# Live Capture (v0.2.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `capture.py`: capture a workflow live with one screenshot per mouse click plus continuous mic audio, producing the exact folder shape extract.py produces, so the record-to-skill skill consumes it unchanged.

**Architecture:** capture.py lives next to extract.py and imports its helpers (naming, transcription, transcript and manifest writers). Hardware access (screen, mouse, keyboard, mic) is isolated in small functions with lazy imports; all bookkeeping lives in a pure `CaptureSession` class that unit tests cover. The manifest gains optional per-frame notes ("click at (x, y) on monitor 1") that help Claude read the frames.

**Tech Stack:** Python 3.10+, mss (screenshots), Pillow (resize/JPEG), pynput (global click and Esc listeners), sounddevice (mic via bundled PortAudio), faster-whisper (already required), pytest (dev only).

## Global Constraints

- Windows first, cross-platform. No GPU. No admin rights.
- Everything runs locally. Audio never leaves the machine.
- pathlib everywhere, no hardcoded paths.
- Plain English errors, fail loud. No em dashes, no emojis, short sentences in every file.
- Use case: The user wants to capture a workflow while doing it, so that no click falls between frames and no separate recording step is needed.
- Success state: a folder with frames, transcript.txt, transcript.json, MANIFEST.md, identical in shape to extract.py output. MANIFEST.md is written last (completeness marker).
- Error states: no mic -> warn and continue frames-only; missing capture deps -> die with the exact pip command; zero frames on stop -> die plainly.
- Empty state: covered by the zero-frames error.
- Version bump 0.1.1 -> 0.2.0 (feat -> MINOR) in plugin.json and marketplace.json.
- The Codex PostToolUse hook reviews every .py write in-session. Fix any [BLOCKER] it raises before moving to the next task.

---

### Task 1: Shared helpers in extract.py + characterization tests

**Files:**
- Modify: `skills/record-to-skill/scripts/extract.py` (add `frame_filename`, change `write_manifest` signature)
- Test: `skills/record-to-skill/scripts/test_extract.py` (create)

**Interfaces:**
- Produces: `frame_filename(index: int, seconds: float) -> str` returning e.g. `frame_0007_02-15.jpg`.
- Produces: `write_manifest(out_dir: Path, source_name: str, duration: float, frames: list[tuple[str, float]], segments: list[Segment], word_count: int, frame_notes: dict[str, str] | None = None) -> None`. The header line becomes `Source: {source_name}`; when a frame has a note it is printed as the first bullet under that frame's heading.

- [ ] **Step 1: Install pytest (dev only, not in requirements.txt)**

Run: `python -m pip install pytest`

- [ ] **Step 2: Write the failing tests**

Create `skills/record-to-skill/scripts/test_extract.py`:

```python
"""Tests for the pure helpers in extract.py. Run with: python -m pytest -v"""

import pytest

import extract


def test_mmss_formats_minutes_and_seconds():
    assert extract.mmss(135) == "02:15"
    assert extract.mmss(135, "-") == "02-15"


def test_frame_filename():
    assert extract.frame_filename(7, 135) == "frame_0007_02-15.jpg"


def test_thin_evenly_keeps_first_and_last():
    times = [float(i) for i in range(10)]
    out = extract.thin_evenly(times, 4)
    assert out[0] == 0.0
    assert out[-1] == 9.0
    assert len(out) <= 4


def test_thin_evenly_single_frame_does_not_crash():
    assert extract.thin_evenly([1.0, 2.0, 3.0], 1) == [1.0]


def test_interval_times_excludes_the_end():
    assert extract.interval_times(0, 30, 5) == [0.0, 5.0, 10.0, 15.0, 20.0, 25.0]


def test_interval_times_rejects_tiny_intervals():
    with pytest.raises(RuntimeError):
        extract.interval_times(0, 30, 1e-320)


def test_parse_timestamp_mm_ss():
    assert extract.parse_timestamp("02:15", "--start") == 135


def test_parse_timestamp_rejects_bad_minutes():
    with pytest.raises(SystemExit):
        extract.parse_timestamp("01:99:00", "--start")


def test_write_manifest_with_source_name_and_notes(tmp_path):
    extract.write_manifest(
        tmp_path,
        "live capture",
        30.0,
        [("frame_0001_00-00.jpg", 0.0)],
        [],
        0,
        frame_notes={"frame_0001_00-00.jpg": "click at (1, 2) on monitor 1"},
    )
    text = (tmp_path / "MANIFEST.md").read_text(encoding="utf-8")
    assert "Source: live capture" in text
    assert "- click at (1, 2) on monitor 1" in text
```

- [ ] **Step 3: Run tests to verify the new-interface ones fail**

Run: `cd skills/record-to-skill/scripts && python -m pytest test_extract.py -v`
Expected: `test_frame_filename` FAILS (no attribute `frame_filename`) and `test_write_manifest_with_source_name_and_notes` FAILS (unexpected argument). The characterization tests pass.

- [ ] **Step 4: Implement in extract.py**

Add below `mmss`:

```python
def frame_filename(index: int, seconds: float) -> str:
    return f"frame_{index:04d}_{mmss(seconds, '-')}.jpg"
```

In `extract_frames`, replace `name = f"frame_{index:04d}_{mmss(t, '-')}.jpg"` with `name = frame_filename(index, t)`.

Change `write_manifest` to:

```python
def write_manifest(
    out_dir: Path,
    source_name: str,
    duration: float,
    frames: list[tuple[str, float]],
    segments: list[Segment],
    word_count: int,
    frame_notes: dict[str, str] | None = None,
) -> None:
```

Inside it, replace `f"Video: {video.name}",` with `f"Source: {source_name}",` and, in the per-frame loop, insert directly after `lines.append(f"## {name} ({mmss(t)})")`:

```python
        if frame_notes and name in frame_notes:
            lines.append(f"- {frame_notes[name]}")
```

Update the caller in `main`: `write_manifest(out_dir, video.name, duration, frames, segments, word_count)`.

- [ ] **Step 5: Run all tests, verify green**

Run: `python -m pytest test_extract.py -v`
Expected: all 9 PASS.

- [ ] **Step 6: Commit**

```bash
git add skills/record-to-skill/scripts/extract.py skills/record-to-skill/scripts/test_extract.py
git commit -m "refactor(extract): share frame naming and manifest writer with capture"
```

---

### Task 2: capture.py bookkeeping core

**Files:**
- Create: `skills/record-to-skill/scripts/capture.py` (module skeleton plus pure parts)
- Test: `skills/record-to-skill/scripts/test_capture.py` (create)

**Interfaces:**
- Consumes: `extract.frame_filename`, `extract.mmss`.
- Produces: `click_note(x: int, y: int, monitor_index: int) -> str`; class `CaptureSession(out_dir: Path, max_frames: int)` with `elapsed() -> float`, `add(seconds: float, note: str) -> str | None` (reserves the next frame name, records the note, returns None past the cap and counts `dropped`), attributes `frames: list[tuple[str, float]]`, `notes: dict[str, str]`, `dropped: int`.

- [ ] **Step 1: Write the failing tests**

Create `skills/record-to-skill/scripts/test_capture.py`:

```python
"""Tests for the pure bookkeeping in capture.py. Run with: python -m pytest -v"""

from pathlib import Path

import capture


def test_click_note():
    assert capture.click_note(100, 200, 2) == "click at (100, 200) on monitor 2"


def test_session_names_frames_sequentially(tmp_path):
    s = capture.CaptureSession(tmp_path, max_frames=10)
    assert s.add(0.0, "initial screen") == "frame_0001_00-00.jpg"
    assert s.add(65.0, "click") == "frame_0002_01-05.jpg"
    assert s.notes["frame_0001_00-00.jpg"] == "initial screen"


def test_session_caps_frames_and_counts_drops(tmp_path):
    s = capture.CaptureSession(tmp_path, max_frames=2)
    assert s.add(0.0, "a") is not None
    assert s.add(1.0, "b") is not None
    assert s.add(2.0, "c") is None
    assert s.dropped == 1
    assert len(s.frames) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest test_capture.py -v`
Expected: FAIL with "No module named 'capture'".

- [ ] **Step 3: Create capture.py with the pure parts**

```python
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
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path

from extract import (
    Segment,
    die,
    frame_filename,
    mmss,
    transcribe_local,
    write_manifest,
    write_transcript,
)

FRAME_WIDTH = 1024
JPEG_QUALITY = 80
SAMPLE_RATE = 16000
PIP_HINT = "python -m pip install -r requirements.txt"


def click_note(x: int, y: int, monitor_index: int) -> str:
    return f"click at ({x}, {y}) on monitor {monitor_index}"


class CaptureSession:
    """Frame bookkeeping only. No hardware access, so tests can cover it."""

    def __init__(self, out_dir: Path, max_frames: int) -> None:
        self.out_dir = out_dir
        self.max_frames = max_frames
        self.t0 = time.monotonic()
        self.frames: list[tuple[str, float]] = []
        self.notes: dict[str, str] = {}
        self.dropped = 0
        self._lock = threading.Lock()

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def add(self, seconds: float, note: str) -> str | None:
        with self._lock:
            if len(self.frames) >= self.max_frames:
                self.dropped += 1
                return None
            name = frame_filename(len(self.frames) + 1, seconds)
            self.frames.append((name, seconds))
            self.notes[name] = note
            return name
```

- [ ] **Step 4: Run tests, verify green**

Run: `python -m pytest test_capture.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/record-to-skill/scripts/capture.py skills/record-to-skill/scripts/test_capture.py
git commit -m "feat(capture): add capture session bookkeeping"
```

---

### Task 3: capture.py hardware layer and main flow

**Files:**
- Modify: `skills/record-to-skill/scripts/capture.py` (append everything below the CaptureSession class)

**Interfaces:**
- Consumes: `CaptureSession`, `click_note` from Task 2; `transcribe_local`, `write_transcript`, `write_manifest`, `die`, `Segment` from extract.
- Produces: a runnable CLI. `--selftest` captures 3 synthetic frames over 6 seconds with no OS-level clicking, used by Task 5 acceptance.

- [ ] **Step 1: Append the hardware and flow code**

```python
def capture_frame(session: CaptureSession, pos: tuple[int, int] | None, note_override: str | None = None) -> None:
    """Screenshot the monitor under pos (primary when pos is None)."""
    try:
        import mss
        from PIL import Image
    except ImportError:
        die(f"mss and Pillow are required for capture. Install them with:\n  {PIP_HINT}")
    seconds = session.elapsed()
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
            else:
                note = click_note(pos[0], pos[1], monitor_index)
            name = session.add(seconds, note)
            if name is not None:
                img.save(session.out_dir / name, "JPEG", quality=JPEG_QUALITY)
    except Exception as exc:
        print(f"WARNING: could not capture a frame at {mmss(seconds)}: {exc}")


class AudioRecorder:
    """Continuous 16 kHz mono mic recording into a wav file."""

    def __init__(self, wav_path: Path) -> None:
        self.wav_path = wav_path
        self._wav = None
        self._stream = None

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
                if self._wav is not None:
                    self._wav.writeframes(bytes(indata))

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
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._wav is not None:
            try:
                self._wav.close()
            except Exception:
                pass
            self._wav = None


def run_listeners(session: CaptureSession) -> None:
    """Block until Esc. Every mouse press captures a frame."""
    try:
        from pynput import keyboard, mouse
    except ImportError:
        die(f"pynput is required for capture. Install it with:\n  {PIP_HINT}")
    stop = threading.Event()

    def on_click(x, y, button, pressed):
        if pressed:
            capture_frame(session, (int(x), int(y)))

    def on_press(key):
        if key == keyboard.Key.esc:
            stop.set()
            return False

    print("Capturing. Every click saves a screenshot. Narrate out loud. Press Esc to finish.")
    with mouse.Listener(on_click=on_click), keyboard.Listener(on_press=on_press):
        stop.wait()


def run_selftest(session: CaptureSession) -> None:
    """Deterministic test mode: no OS clicks, no listeners."""
    print("Self-test: capturing 3 synthetic click frames over 6 seconds.")
    for _ in range(3):
        time.sleep(2)
        capture_frame(session, (10, 10), "selftest click")


def finalize(session: CaptureSession, whisper_model: str, wav: Path, audio_on: bool) -> None:
    frames = [(n, t) for (n, t) in session.frames if (session.out_dir / n).is_file()]
    if not frames:
        die("No frames were captured. Nothing to build a skill from.")
    segments: list[Segment] = []
    source = "none (no audio captured)"
    try:
        if audio_on and wav.is_file() and wav.stat().st_size > 44:
            segments = transcribe_local(wav, whisper_model, offset=0.0)
            source = f"faster-whisper {whisper_model} (local)"
    finally:
        wav.unlink(missing_ok=True)
    duration = session.elapsed()
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
    parser.add_argument("--max-frames", type=int, default=200, help="Stop saving frames past this cap (default 200)")
    parser.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.max_frames < 1:
        die("--max-frames must be at least 1.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out).expanduser().resolve() if args.out else Path.cwd() / f"capture_{stamp}_extract"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Same invariant as extract.py: MANIFEST.md is written last, so remove
    # stale outputs first. A failed run must not leave a poisoned folder.
    for stale_name in ("MANIFEST.md", "transcript.txt", "transcript.json"):
        (out_dir / stale_name).unlink(missing_ok=True)

    session = CaptureSession(out_dir, args.max_frames)
    wav = out_dir / f"audio_tmp_{uuid.uuid4().hex}.wav"
    recorder = AudioRecorder(wav)
    audio_on = False if args.no_audio else recorder.start()
    try:
        capture_frame(session, None, "initial screen")
        if args.selftest:
            run_selftest(session)
        else:
            run_listeners(session)
    finally:
        recorder.stop()
    finalize(session, args.whisper_model, wav, audio_on)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("Cancelled.")
    except RuntimeError as exc:
        die(str(exc))
    except OSError as exc:
        die(f"File error: {exc}")
```

- [ ] **Step 2: Compile check**

Run: `python -m py_compile capture.py`
Expected: no output, exit 0.

- [ ] **Step 3: Re-run both test files, verify green**

Run: `python -m pytest test_extract.py test_capture.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add skills/record-to-skill/scripts/capture.py
git commit -m "feat(capture): screenshots on click, mic recording, selftest mode"
```

---

### Task 4: Dependencies, docs, version 0.2.0

**Files:**
- Modify: `skills/record-to-skill/scripts/requirements.txt`
- Modify: `skills/record-to-skill/SKILL.md`
- Modify: `README.md`
- Modify: `.claude-plugin/plugin.json` (version 0.2.0)
- Modify: `.claude-plugin/marketplace.json` (version 0.2.0)

- [ ] **Step 1: requirements.txt** (append)

```
# Live capture (capture.py)
mss>=9
Pillow>=10
pynput>=1.7
sounddevice>=0.4
```

- [ ] **Step 2: SKILL.md** (insert at the end of step 2's section, before "## 3. Read the evidence")

```
Live capture variant: if the user wants to capture while they work instead of recording a video, give them this command to run in their own terminal. Tell them to narrate out loud and press Esc to finish, then treat the folder it prints as the extract folder and continue at step 3.

    python "${CLAUDE_PLUGIN_ROOT}/skills/record-to-skill/scripts/capture.py"
```

- [ ] **Step 3: README.md** (add after the Usage section, and update Roadmap)

New section:

```
## Live capture (no video file)

Instead of recording a video first, capture while you work. Run this in a terminal:

    python <plugin-folder>/skills/record-to-skill/scripts/capture.py

Every mouse click saves a screenshot of the monitor you clicked on, and the mic records continuously. Press Esc to finish. It prints a folder; tell Claude "turn this recording into a skill" with that folder. Needs the capture dependencies: python -m pip install -r skills/record-to-skill/scripts/requirements.txt
```

Roadmap section becomes:

```
## Roadmap

- Live capture shipped in v0.2.0. Nothing else planned yet.
```

- [ ] **Step 4: Version bumps**

In `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`, change `"version": "0.1.1"` to `"version": "0.2.0"`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(capture): document live capture, bump to 0.2.0"
```

---

### Task 5: Acceptance, package, ship

**Files:**
- Modify: `workflow-to-skill.plugin` (rebuild, stays gitignored)

- [ ] **Step 1: Install capture dependencies**

Run: `python -m pip install mss Pillow pynput sounddevice`

- [ ] **Step 2: Full test suite**

Run: `cd skills/record-to-skill/scripts && python -m pytest -v`
Expected: all PASS.

- [ ] **Step 3: Selftest acceptance run**

Run: `python capture.py --selftest --whisper-model tiny --out <scratchpad>/selftest_extract`
Expected: prints Done with 4 frames (initial + 3 selftest clicks). Verify: `selftest_extract/` contains 4 frame jpgs, `MANIFEST.md` containing "Source: live capture" and "selftest click", `transcript.txt`, `transcript.json`. Mic silence gives an empty transcript; that is fine. Delete `selftest_extract/` afterwards.

- [ ] **Step 4: Validate and rebuild the plugin zip**

Run: `claude plugin validate . --strict`
Expected: Validation passed.
Rebuild `workflow-to-skill.plugin` with the same Python zipfile snippet used for 0.1.x (exclude `.git`, `__pycache__`, the `.plugin` itself).

- [ ] **Step 5: Push and update the installed plugin**

```bash
git push
claude plugin marketplace update workflow-to-skill
claude plugin update workflow-to-skill@workflow-to-skill
```

Expected: plugin reports 0.2.0.

- [ ] **Step 6: Report**

Tell the user: what shipped, the exact capture command to try, and that a restart applies the update.
