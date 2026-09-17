#!/usr/bin/env python3
"""Extract keyframes and a timestamped transcript from a screen recording.

Claude cannot watch video. This script turns a recording into a folder Claude
can read: JPEG frames named by timestamp, transcript.txt, transcript.json,
and MANIFEST.md that links each frame to the narration around it.

Everything runs locally. Audio only leaves the machine if --cloud is passed.

Usage:
  python extract.py <video> [--out DIR] [--max-frames 60] [--scene 0.3]
                    [--fallback-interval 5] [--whisper-model small] [--cloud]
                    [--start MM:SS] [--end MM:SS]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
MIN_SCENE_FRAMES = 8
FRAME_WIDTH = 1024
JPEG_QSCALE = "5"  # ffmpeg mjpeg qscale 5 is roughly JPEG quality 80
NEARBY_SECONDS = 4.0
OPENAI_TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"


@dataclass
class Segment:
    start: float
    end: float
    text: str


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def check_tools() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if not missing:
        return
    names = " and ".join(missing)
    if sys.platform == "win32":
        hint = "winget install --id Gyan.FFmpeg -e   (then close and reopen the terminal)"
    elif sys.platform == "darwin":
        hint = "brew install ffmpeg"
    else:
        hint = "sudo apt install ffmpeg"
    die(
        f"{names} not found on PATH. This script needs ffmpeg to read the video.\n"
        f"Install it with:  {hint}"
    )


def run(cmd: list[str], why: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Could not start {cmd[0]} while {why}: {exc}") from exc
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        raise RuntimeError(f"{cmd[0]} failed while {why}.\n{tail}")
    return result


def parse_timestamp(value: str, flag: str) -> float:
    parts = value.split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        die(f"{flag} must look like MM:SS (or HH:MM:SS), got '{value}'.")
    parts_int = [int(p) for p in parts]
    if len(parts_int) == 2:
        minutes, seconds = parts_int
        hours = 0
    else:
        hours, minutes, seconds = parts_int
    if seconds > 59:
        die(f"{flag}: seconds must be 00-59, got '{value}'.")
    return hours * 3600 + minutes * 60 + seconds


def mmss(t: float, sep: str = ":") -> str:
    total = int(t)
    return f"{total // 60:02d}{sep}{total % 60:02d}"


def probe_duration(video: Path) -> float:
    result = run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        "reading the video duration",
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise RuntimeError(f"ffprobe returned no duration for {video.name}. Is it a valid video?")


def has_audio_stream(video: Path) -> bool:
    result = run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(video),
        ],
        "checking for an audio track",
    )
    return bool(result.stdout.strip())


def detect_scene_times(video: Path, threshold: float, start: float, end: float) -> list[float]:
    select = f"between(t,{start},{end})*gt(scene,{threshold})"
    result = run(
        [
            "ffmpeg", "-hide_banner",
            "-i", str(video),
            "-vf", f"select='{select}',showinfo",
            "-an", "-f", "null", "-",
        ],
        "detecting scene changes",
    )
    times = [float(m) for m in re.findall(r"pts_time:([0-9]+\.?[0-9]*)", result.stderr)]
    return sorted(set(times))


def interval_times(start: float, end: float, interval: float) -> list[float]:
    times: list[float] = []
    t = start
    while t < end:
        times.append(round(t, 3))
        t += interval
    return times or [start]


def thin_evenly(times: list[float], max_frames: int) -> list[float]:
    # Keep the first and last frame; drop evenly from the middle.
    n = len(times)
    if n <= max_frames:
        return times
    indices = sorted({round(i * (n - 1) / (max_frames - 1)) for i in range(max_frames)})
    return [times[i] for i in indices]


def extract_frames(video: Path, times: list[float], out_dir: Path) -> list[tuple[str, float]]:
    for stale in out_dir.glob("frame_*.jpg"):
        stale.unlink()
    frames: list[tuple[str, float]] = []
    for index, t in enumerate(times, start=1):
        name = f"frame_{index:04d}_{mmss(t, '-')}.jpg"
        target = out_dir / name
        run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-ss", f"{t:.3f}",
                "-i", str(video),
                "-frames:v", "1",
                "-vf", f"scale={FRAME_WIDTH}:-2",
                "-q:v", JPEG_QSCALE,
                "-y", str(target),
            ],
            f"extracting the frame at {mmss(t)}",
        )
        if target.exists() and target.stat().st_size > 0:
            frames.append((name, t))
    if not frames:
        raise RuntimeError("No frames could be extracted. The video may be empty or corrupt.")
    return frames


def extract_audio(video: Path, wav: Path, start: float, end: float) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(video)]
    if end > start:
        cmd += ["-t", f"{end - start:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(wav)]
    run(cmd, "extracting the audio track")


def transcribe_local(wav: Path, model_name: str, offset: float) -> list[Segment]:
    # The default Hugging Face cache layout needs symlinks, which Windows
    # blocks unless Developer Mode is on (WinError 1314). Downloading into a
    # plain folder avoids symlinks entirely.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    try:
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model
    except ImportError:
        die(
            "faster-whisper is not installed, so the audio cannot be transcribed.\n"
            "Install it with:  python -m pip install faster-whisper\n"
            "The first run also downloads the Whisper model (a few hundred MB)."
        )
    print(f"Transcribing locally with faster-whisper '{model_name}' (first run downloads the model)...")
    model_dir = Path.home() / ".cache" / "workflow-to-skill" / "models" / model_name
    model_dir.mkdir(parents=True, exist_ok=True)
    try:
        model_path = download_model(model_name, output_dir=str(model_dir))
    except Exception as exc:
        raise RuntimeError(
            f"Could not download the Whisper model '{model_name}'. "
            f"Check the model name and the internet connection.\n{exc}"
        ) from exc
    model = WhisperModel(str(model_path), device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(wav), vad_filter=True)
    return [
        Segment(seg.start + offset, seg.end + offset, seg.text.strip())
        for seg in segments
        if seg.text.strip()
    ]


def transcribe_cloud(wav: Path, offset: float) -> list[Segment]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        die("--cloud was passed but OPENAI_API_KEY is not set. Set it, or drop --cloud to transcribe locally.")
    print("WARNING: --cloud sends the audio track to OpenAI for transcription. The audio leaves this machine.")
    boundary = f"----workflow-to-skill-{uuid.uuid4().hex}"
    fields = {"model": "whisper-1", "response_format": "verbose_json"}
    body = bytearray()
    for key, value in fields.items():
        body += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n"
        ).encode()
    body += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n"
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()
    body += wav.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        OPENAI_TRANSCRIPTION_URL,
        data=bytes(body),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"OpenAI transcription failed (HTTP {exc.code}). {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach OpenAI: {exc.reason}") from exc
    return [
        Segment(float(seg["start"]) + offset, float(seg["end"]) + offset, str(seg["text"]).strip())
        for seg in payload.get("segments", [])
        if str(seg.get("text", "")).strip()
    ]


def write_transcript(out_dir: Path, segments: list[Segment], source: str) -> int:
    lines = [f"[{mmss(seg.start)}] {seg.text}" for seg in segments]
    (out_dir / "transcript.txt").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    word_count = sum(len(seg.text.split()) for seg in segments)
    payload = {
        "source": source,
        "word_count": word_count,
        "segments": [{"start": seg.start, "end": seg.end, "text": seg.text} for seg in segments],
    }
    (out_dir / "transcript.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return word_count


def write_manifest(
    out_dir: Path,
    video: Path,
    duration: float,
    frames: list[tuple[str, float]],
    segments: list[Segment],
    word_count: int,
) -> None:
    lines = [
        "# Extract manifest",
        "",
        f"Video: {video.name}",
        f"Duration: {mmss(duration)}",
        f"Frames: {len(frames)}",
        f"Transcript words: {word_count}",
        "",
        "Read this file first. Then read transcript.txt in full. Then view the frames in order.",
        f"Narration shown under each frame is every transcript line within {int(NEARBY_SECONDS)} seconds of it.",
        "",
    ]
    for name, t in frames:
        lines.append(f"## {name} ({mmss(t)})")
        nearby = [
            seg for seg in segments
            if seg.start <= t + NEARBY_SECONDS and seg.end >= t - NEARBY_SECONDS
        ]
        if nearby:
            lines.extend(f"- [{mmss(seg.start)}] {seg.text}" for seg in nearby)
        else:
            lines.append(f"(no narration within {int(NEARBY_SECONDS)} seconds)")
        lines.append("")
    (out_dir / "MANIFEST.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract frames and a transcript from a screen recording.")
    parser.add_argument("video", help="Path to the recording (.mp4, .mov, .mkv, .webm)")
    parser.add_argument("--out", help="Output folder (default: <video-folder>/<video-name>_extract)")
    parser.add_argument("--max-frames", type=int, default=60, help="Frame cap, thinned evenly (default 60)")
    parser.add_argument("--scene", type=float, default=0.3, help="Scene-change threshold 0-1 (default 0.3)")
    parser.add_argument("--fallback-interval", type=float, default=5,
                        help="Seconds between frames when scene detection finds too few (default 5)")
    parser.add_argument("--whisper-model", default="small",
                        help="faster-whisper model: tiny, base, small, medium (default small)")
    parser.add_argument("--cloud", action="store_true",
                        help="Use OpenAI whisper-1 instead of local transcription. Audio leaves the machine.")
    parser.add_argument("--start", help="Only process from this point, MM:SS")
    parser.add_argument("--end", help="Only process up to this point, MM:SS")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        die(f"Video not found: {video}")
    if video.suffix.lower() not in VIDEO_EXTENSIONS:
        allowed = " ".join(sorted(VIDEO_EXTENSIONS))
        die(f"'{video.suffix}' is not a supported video type. Use one of: {allowed}")
    if args.max_frames < 1:
        die("--max-frames must be at least 1.")
    if args.fallback_interval <= 0:
        die("--fallback-interval must be greater than 0.")

    check_tools()
    duration = probe_duration(video)
    start = parse_timestamp(args.start, "--start") if args.start else 0.0
    end = min(parse_timestamp(args.end, "--end"), duration) if args.end else duration
    if start >= end:
        die(f"--start ({mmss(start)}) must be before --end ({mmss(end)}).")

    out_dir = Path(args.out).expanduser().resolve() if args.out else video.parent / f"{video.stem}_extract"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Detecting scene changes (threshold {args.scene})...")
    times = detect_scene_times(video, args.scene, start, end)
    if len(times) < MIN_SCENE_FRAMES:
        print(
            f"Scene detection found {len(times)} frames (fewer than {MIN_SCENE_FRAMES}). "
            f"Falling back to one frame every {args.fallback_interval:g} seconds."
        )
        times = interval_times(start, end, args.fallback_interval)
    times = thin_evenly(times, args.max_frames)

    print(f"Extracting {len(times)} frames...")
    frames = extract_frames(video, times, out_dir)

    segments: list[Segment] = []
    source = "none (no audio stream)"
    if has_audio_stream(video):
        wav = out_dir / "audio_tmp.wav"
        extract_audio(video, wav, start, end)
        if args.cloud:
            segments = transcribe_cloud(wav, offset=start)
            source = "openai whisper-1 (cloud)"
        else:
            segments = transcribe_local(wav, args.whisper_model, offset=start)
            source = f"faster-whisper {args.whisper_model} (local)"
        wav.unlink(missing_ok=True)
    else:
        print("No audio stream found. The transcript will be empty.")

    word_count = write_transcript(out_dir, segments, source)
    write_manifest(out_dir, video, duration, frames, segments, word_count)

    print("")
    print("Done.")
    print(f"  Duration:   {mmss(duration)}")
    print(f"  Frames:     {len(frames)}")
    print(f"  Transcript: {word_count} words ({source})")
    print(f"  Output:     {out_dir}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("Cancelled.")
    except RuntimeError as exc:
        die(str(exc))
