#!/usr/bin/env python3
"""Capture a workflow live: one screenshot per mouse click, plus mic audio.

Companion to extract.py. Produces the same folder shape (frames named by
timestamp, transcript.txt, transcript.json, MANIFEST.md), so the
record-to-skill skill consumes it unchanged. Everything runs locally.

Usage:
  python capture.py [--out DIR] [--no-audio] [--whisper-model small]
                    [--max-frames 200] [--selftest]

While capturing: every mouse click saves a screenshot of the monitor the
click happened on. Narrate out loud. Press Esc to finish.
"""

from __future__ import annotations

import argparse
import queue
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path

from extract import (
    Segment,
    clear_previous_outputs,
    die,
    frame_filename,
    mark_ownership,
    mmss,
    transcribe_local,
    write_manifest,
    write_transcript,
)

FRAME_WIDTH = 1024
JPEG_QUALITY = 80
SAMPLE_RATE = 16000
CLICK_QUEUE_SIZE = 1000
WORKER_FLUSH_SECONDS = 30
PIP_HINT = "python -m pip install -r requirements.txt"


def click_note(x: int, y: int, monitor_index: int) -> str:
    return f"click at ({x}, {y}) on monitor {monitor_index}"


class CaptureSession:
    """Frame bookkeeping only. No hardware access, so tests can cover it.

    A frame is reserved with add(), then confirmed with confirm() once its
    bytes are fully on disk. Only confirmed frames are published, so a write
    still in flight during finalization can never reach the manifest.
    """

    def __init__(self, out_dir: Path, max_frames: int) -> None:
        self.out_dir = out_dir
        self.max_frames = max_frames
        self.t0 = time.monotonic()
        self.frames: list[tuple[str, float]] = []
        self.notes: dict[str, str] = {}
        self.dropped = 0
        self._confirmed: set[str] = set()
        self._closed = False
        self._lock = threading.Lock()

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    @property
    def full(self) -> bool:
        with self._lock:
            return self._closed or len(self.frames) >= self.max_frames

    def add(self, seconds: float, note: str) -> str | None:
        """Reserve the next frame name, or None once capped or closed."""
        with self._lock:
            if self._closed or len(self.frames) >= self.max_frames:
                self.dropped += 1
                return None
            name = frame_filename(len(self.frames) + 1, seconds)
            self.frames.append((name, seconds))
            self.notes[name] = note
            return name

    def confirm(self, name: str) -> None:
        """Mark a reserved frame as fully written to disk."""
        with self._lock:
            self._confirmed.add(name)

    def remove(self, name: str) -> None:
        """Roll back a reservation whose screenshot could not be saved."""
        with self._lock:
            self.frames = [(n, t) for (n, t) in self.frames if n != name]
            self.notes.pop(name, None)
            self._confirmed.discard(name)

    def close(self) -> None:
        """Refuse all further frames. A late worker cannot corrupt output."""
        with self._lock:
            self._closed = True

    def confirmed_frames(self) -> list[tuple[str, float]]:
        with self._lock:
            return [(n, t) for (n, t) in self.frames if n in self._confirmed]


def capture_frame(
    session: CaptureSession,
    pos: tuple[int, int] | None,
    seconds: float | None = None,
    note_override: str | None = None,
) -> None:
    """Screenshot the monitor under pos (primary monitor when pos is None).

    seconds is the click time; it defaults to now for direct calls.
    """
    try:
        import mss
        from PIL import Image
    except ImportError:
        die(f"mss and Pillow are required for capture. Install them with:\n  {PIP_HINT}")
    if seconds is None:
        seconds = session.elapsed()
    name: str | None = None
    target: Path | None = None
    try:
        with mss.mss() as sct:
            monitor_index = 1
            if pos is not None:
                for i, mon in enumerate(sct.monitors[1:], start=1):
                    inside_x = mon["left"] <= pos[0] < mon["left"] + mon["width"]
                    inside_y = mon["top"] <= pos[1] < mon["top"] + mon["height"]
                    if inside_x and inside_y:
                        monitor_index = i
                        break
            shot = sct.grab(sct.monitors[monitor_index])
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if img.width > FRAME_WIDTH:
            img = img.resize((FRAME_WIDTH, round(img.height * FRAME_WIDTH / img.width)))
        if note_override is not None:
            note = note_override
        elif pos is not None:
            note = click_note(pos[0], pos[1], monitor_index)
        else:
            note = "screen capture"
        name = session.add(seconds, note)
        if name is None:
            return
        target = session.out_dir / name
        img.save(target, "JPEG", quality=JPEG_QUALITY)
        session.confirm(name)
    except Exception as exc:
        # A single failed frame must not kill the session, and a failed save
        # must not leave a reserved slot or a partial file behind.
        if name is not None:
            session.remove(name)
        if target is not None:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        print(f"WARNING: could not capture a frame at {mmss(seconds)}: {exc}")


class AudioRecorder:
    """Continuous 16 kHz mono mic recording into a wav file."""

    def __init__(self, wav_path: Path) -> None:
        self.wav_path = wav_path
        self.overflows = 0
        self._wav = None
        self._stream = None
        self._write_lock = threading.Lock()

    def start(self) -> bool:
        try:
            import sounddevice as sd
        except ImportError:
            print(f"WARNING: sounddevice is not installed. Capturing without audio. Install it with:\n  {PIP_HINT}")
            return False
        try:
            self._wav = wave.open(str(self.wav_path), "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(SAMPLE_RATE)

            def callback(indata, frames, time_info, status):
                if status:
                    self.overflows += 1
                try:
                    with self._write_lock:
                        if self._wav is not None:
                            self._wav.writeframes(bytes(indata))
                except Exception:
                    self.overflows += 1

            self._stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=callback
            )
            self._stream.start()
            return True
        except Exception as exc:
            print(f"WARNING: could not start the microphone ({exc}). Capturing without audio.")
            self.stop()
            return False

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        with self._write_lock:
            if self._wav is not None:
                try:
                    self._wav.close()
                except Exception:
                    pass
                self._wav = None
        if self.overflows:
            print(f"WARNING: the audio recording has {self.overflows} gaps. The transcript may miss words.")


def run_listeners(session: CaptureSession) -> None:
    """Block until Esc. Every mouse press queues a frame capture."""
    try:
        from pynput import keyboard, mouse
    except ImportError:
        die(f"pynput is required for capture. Install it with:\n  {PIP_HINT}")
    clicks: queue.Queue[tuple[tuple[int, int], float] | None] = queue.Queue(maxsize=CLICK_QUEUE_SIZE)
    stop = threading.Event()

    def worker() -> None:
        while True:
            item = clicks.get()
            if item is None:
                return
            pos, seconds = item
            capture_frame(session, pos, seconds=seconds)

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    def on_click(x, y, button, pressed):
        if not pressed or session.full:
            return
        # Only enqueue. Heavy work inside the low-level mouse hook lags the
        # pointer system-wide on Windows.
        try:
            clicks.put_nowait(((int(x), int(y)), session.elapsed()))
        except queue.Full:
            pass

    def on_press(key):
        if key == keyboard.Key.esc:
            stop.set()
            return False

    print("Capturing. Every click saves a screenshot. Narrate out loud. Press Esc to finish.")
    with mouse.Listener(on_click=on_click), keyboard.Listener(on_press=on_press) as key_listener:
        # A crashed keyboard listener would make Esc dead. End the run
        # instead of hanging forever.
        while not stop.is_set() and key_listener.running:
            stop.wait(0.5)
    try:
        clicks.put_nowait(None)
    except queue.Full:
        pass
    worker_thread.join(timeout=WORKER_FLUSH_SECONDS)
    if worker_thread.is_alive():
        print("WARNING: some queued clicks were still being saved and may be missing from the output.")
    session.close()


def run_selftest(session: CaptureSession) -> None:
    """Deterministic test mode: no OS clicks, no global listeners."""
    print("Self-test: capturing 3 synthetic click frames over 6 seconds.")
    for _ in range(3):
        time.sleep(2)
        capture_frame(session, (10, 10), note_override="selftest click")


def finalize(session: CaptureSession, whisper_model: str, wav: Path, audio_on: bool) -> None:
    # Capture ends here; measure before transcription so duration is honest.
    duration = session.elapsed()
    # Only frames whose write completed are published. This is computed from
    # the save outcome, never inferred from the filesystem.
    frames = [(n, t) for (n, t) in session.confirmed_frames() if (session.out_dir / n).is_file()]
    if not frames:
        die("No frames were captured. Nothing to build a skill from.")
    segments: list[Segment] = []
    source = "none (no audio captured)"
    try:
        if audio_on and wav.is_file() and wav.stat().st_size > 44:
            segments = transcribe_local(wav, whisper_model, offset=0.0)
            source = f"faster-whisper {whisper_model} (local)"
    finally:
        # Raw microphone audio never stays behind, even on failure.
        wav.unlink(missing_ok=True)
    word_count = write_transcript(session.out_dir, segments, source)
    write_manifest(session.out_dir, "live capture", duration, frames, segments, word_count,
                   frame_notes=session.notes)
    print("")
    print("Done.")
    print(f"  Duration:   {mmss(duration)}")
    print(f"  Frames:     {len(frames)}")
    if session.dropped:
        print(f"  Dropped:    {session.dropped} clicks past the --max-frames cap")
    print(f"  Transcript: {word_count} words ({source})")
    print(f"  Output:     {session.out_dir}")
    print("")
    print(f'Next: tell Claude "turn this recording into a skill: {session.out_dir}"')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture a workflow live: screenshot per click plus mic.")
    parser.add_argument("--out", help="Output folder (default: ./capture_<timestamp>_extract)")
    parser.add_argument("--no-audio", action="store_true", help="Skip microphone recording")
    parser.add_argument("--whisper-model", default="small",
                        help="faster-whisper model: tiny, base, small, medium (default small)")
    parser.add_argument("--max-frames", type=int, default=200,
                        help="Stop saving frames past this cap (default 200)")
    parser.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.max_frames < 1:
        die("--max-frames must be at least 1.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out).expanduser().resolve() if args.out else Path.cwd() / f"capture_{stamp}_extract"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Same invariants as extract.py: only delete outputs in a folder we own,
    # and MANIFEST.md is written last, so its presence marks a complete run.
    clear_previous_outputs(out_dir)
    mark_ownership(out_dir)

    session = CaptureSession(out_dir, args.max_frames)
    wav = out_dir / f"audio_tmp_{uuid.uuid4().hex}.wav"
    recorder = AudioRecorder(wav)
    try:
        audio_on = False if args.no_audio else recorder.start()
        try:
            capture_frame(session, None, note_override="initial screen")
            if args.selftest:
                run_selftest(session)
            else:
                run_listeners(session)
        finally:
            session.close()
            recorder.stop()
        finalize(session, args.whisper_model, wav, audio_on)
    finally:
        wav.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("Cancelled.")
    except RuntimeError as exc:
        die(str(exc))
    except OSError as exc:
        die(f"File error: {exc}")
