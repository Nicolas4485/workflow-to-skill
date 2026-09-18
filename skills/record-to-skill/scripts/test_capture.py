"""Tests for the pure bookkeeping in capture.py. Run with: python -m pytest -v"""

import capture


def test_click_note():
    assert capture.click_note(100, 200, 2) == "click at (100, 200) on monitor 2"


def test_session_names_frames_sequentially(tmp_path):
    s = capture.CaptureSession(tmp_path, max_frames=10)
    assert s.add(0.0, "initial screen") == "frame_0001_00-00.jpg"
    assert s.add(65.0, "click") == "frame_0002_01-05.jpg"
    assert s.notes["frame_0001_00-00.jpg"] == "initial screen"


def test_only_confirmed_frames_are_published(tmp_path):
    s = capture.CaptureSession(tmp_path, max_frames=5)
    first = s.add(0.0, "written fully")
    s.add(1.0, "still being written")
    s.confirm(first)
    assert s.confirmed_frames() == [(first, 0.0)]


def test_session_caps_frames_and_counts_drops(tmp_path):
    s = capture.CaptureSession(tmp_path, max_frames=2)
    assert s.add(0.0, "a") is not None
    assert s.add(1.0, "b") is not None
    assert s.add(2.0, "c") is None
    assert s.add(3.0, "d") is None
    assert s.dropped == 2
    assert len(s.frames) == 2
    assert "c" not in s.notes.values()
