import json

import meetings


def test_model_names():
    assert meetings.model_id("parakeet") == "parakeet"
    assert meetings.model_id("whisper", "medium") == "whisper-medium"
    assert meetings.model_name("whisper-small") == "Whisper small"
    assert meetings.model_name("parakeet+whisper-medium") == "Parakeet TDT 0.6B v3 + Whisper medium"
    assert meetings.model_name(None) == "an unknown model"
    assert meetings.model_name("whisper-tiny") == "Whisper tiny"


def test_fmt_time():
    assert meetings.fmt_time(0) == "00:00:00"
    assert meetings.fmt_time(3725.9) == "01:02:05"


def test_title_falls_back_to_folder_date(tmp_path):
    folder = tmp_path / "2026-11-02_131011"
    folder.mkdir()
    assert meetings.title(folder) == "Meeting on Mon 2 Nov, 13:10"
    meetings.save_meta(folder, title="Weekly sync")
    assert meetings.title(folder) == "Weekly sync"


def test_archive_and_restore_round_trip(tmp_path):
    folder = tmp_path / "2026-11-02_131011"
    folder.mkdir()
    (folder / "transcript.txt").write_text("[00:00:01] You: first version\n")
    meetings.save_meta(folder, model="parakeet", transcribed="2026-11-02T14:00:00")

    vid = meetings.archive_current(folder)
    assert not (folder / "transcript.txt").exists()
    (folder / "transcript.txt").write_text("[00:00:01] You: second version\n")
    meetings.save_meta(folder, model="whisper-medium", transcribed="2026-11-02T15:00:00")

    versions = meetings.list_versions(folder)
    assert [v["id"] for v in versions] == [vid]
    assert versions[0]["model"] == "parakeet"

    meetings.restore_version(folder, vid)
    assert "first version" in (folder / "transcript.txt").read_text()
    assert meetings.current_model(folder)[0] == "parakeet"
    assert [v["model"] for v in meetings.list_versions(folder)] == ["whisper-medium"]


def test_legacy_backups_are_named_after_the_engine_that_replaced_them(tmp_path):
    folder = tmp_path / "m"
    folder.mkdir()
    meetings.save_meta(folder, engine="parakeet", started="2026-11-02T13:10:00", duration=600)
    (folder / "transcript.before-whisper-20261102-160041.txt").write_text("old")
    (folder / "transcript.txt").write_text("new")
    [old] = meetings.list_versions(folder)
    assert old["model"] == "parakeet"                   # made by the recording
    assert meetings.current_model(folder)[0] == "whisper"  # made by the engine named on the backup


def test_mixed_audio_command(tmp_path):
    assert meetings.mixed_audio_command(tmp_path, tmp_path / "out.mp3") is None
    (tmp_path / "audio-mic.ogg").write_bytes(b"x")
    one = meetings.mixed_audio_command(tmp_path, tmp_path / "out.mp3")
    assert "amix" not in " ".join(one) and "afftdn" in " ".join(one)
    (tmp_path / "audio-meeting.ogg").write_bytes(b"x")
    both = meetings.mixed_audio_command(tmp_path, tmp_path / "out.mp3")
    graph = both[both.index("-filter_complex") + 1]
    assert "amix=inputs=2" in graph and "alimiter" in graph
    assert both[-1].endswith("out.mp3")


def test_bookmarks_round_trip(tmp_path):
    marks = [{"t": 12.5, "note": "decision"}]
    meetings.save_bookmarks(tmp_path, marks)
    assert meetings.load_bookmarks(tmp_path) == marks
    assert json.loads((tmp_path / "bookmarks.json").read_text()) == marks
