"""Tests for the pure helpers in extract.py. Run with: python -m pytest -v"""

import pytest

import extract


def test_mmss_formats_minutes_and_seconds():
    assert extract.mmss(135) == "02:15"
    assert extract.mmss(135, "-") == "02-15"


def test_frame_filename():
    assert extract.frame_filename(7, 135) == "frame_0007_02-15.jpg"


def test_thin_evenly_samples_evenly_keeping_first_and_last():
    times = [float(i) for i in range(10)]
    assert extract.thin_evenly(times, 4) == [0.0, 3.0, 6.0, 9.0]


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


def test_clear_previous_outputs_refuses_foreign_files(tmp_path):
    (tmp_path / "MANIFEST.md").write_text("my own notes", encoding="utf-8")
    with pytest.raises(RuntimeError):
        extract.clear_previous_outputs(tmp_path)
    assert (tmp_path / "MANIFEST.md").read_text(encoding="utf-8") == "my own notes"


def test_clear_previous_outputs_removes_own_files_only(tmp_path):
    (tmp_path / "MANIFEST.md").write_text("# Extract manifest\n", encoding="utf-8")
    (tmp_path / "transcript.txt").write_text("", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("unrelated", encoding="utf-8")
    extract.clear_previous_outputs(tmp_path)
    assert not (tmp_path / "MANIFEST.md").exists()
    assert not (tmp_path / "transcript.txt").exists()
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "unrelated"


def test_clear_previous_outputs_accepts_ownership_marker(tmp_path):
    extract.mark_ownership(tmp_path)
    (tmp_path / "transcript.txt").write_text("old", encoding="utf-8")
    extract.clear_previous_outputs(tmp_path)
    assert not (tmp_path / "transcript.txt").exists()


def test_clear_previous_outputs_refuses_foreign_frames(tmp_path):
    (tmp_path / "frame_0001_00-00.jpg").write_bytes(b"jpg")
    with pytest.raises(RuntimeError):
        extract.clear_previous_outputs(tmp_path)
    assert (tmp_path / "frame_0001_00-00.jpg").exists()


def test_clear_previous_outputs_removes_own_stale_frames(tmp_path):
    extract.mark_ownership(tmp_path)
    (tmp_path / "frame_0001_00-00.jpg").write_bytes(b"jpg")
    extract.clear_previous_outputs(tmp_path)
    assert not (tmp_path / "frame_0001_00-00.jpg").exists()


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
