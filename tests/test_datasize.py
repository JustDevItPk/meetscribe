import os

import datasize
import procstats


def test_fmt():
    assert datasize.fmt(0) == "0 B"
    assert datasize.fmt(512) == "512 B"
    assert datasize.fmt(2048) == "2 KB"
    assert datasize.fmt(5 * 2**20) == "5.0 MB"
    assert datasize.fmt(150 * 2**20) == "150 MB"
    assert datasize.fmt(3 * 2**30) == "3.0 GB"
    assert procstats.size(3 * 2**30) == "3.0 GB"


def test_meeting_parts(tmp_path):
    files = {"audio-meeting.ogg": 1000, "audio-mic.ogg": 500, "transcript.txt": 30, "segments.json": 70,
             "transcript.before-whisper-20261102-160041.txt": 20, "chat.json": 5, "meeting.json": 3}
    for name, size in files.items():
        (tmp_path / name).write_bytes(b"x" * size)
    (tmp_path / "versions" / "20261103-120000").mkdir(parents=True)
    (tmp_path / "versions" / "20261103-120000" / "transcript.txt").write_bytes(b"x" * 40)
    sizes = datasize.meeting(tmp_path)
    assert sizes == {"audio": 1500, "transcript": 100, "versions": 60, "chat": 5, "other": 3, "total": 1668}
    assert datasize.meeting_line(sizes).startswith("2 KB on disk · audio recordings 1 KB")


def test_hf_repo_counts_shared_blobs_once(tmp_path):
    repo = tmp_path / "models--Systran--faster-whisper-small"
    (repo / "blobs").mkdir(parents=True)
    (repo / "blobs" / "abc").write_bytes(b"x" * 1000)
    snap = repo / "snapshots" / "rev1"
    snap.mkdir(parents=True)
    os.symlink(repo / "blobs" / "abc", snap / "model.bin")
    os.symlink(repo / "blobs" / "abc", snap / "copy.bin")
    assert datasize.hf_repo(repo) == 1000
