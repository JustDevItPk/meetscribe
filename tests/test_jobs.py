import pathlib
import queue
import sys
import time

import pytest

import jobs
import meetings

FAKE = pathlib.Path(__file__).with_name("fakeworker.py")


class Loop:
    """Runs the Jobs callbacks on the test's thread, like GLib.idle_add does in the app."""

    def __init__(self):
        self.q = queue.Queue()
        self.finished = []

    def post(self, fn, *a):
        self.q.put((fn, a))

    def until(self, cond, timeout=15):
        end = time.monotonic() + timeout
        while not cond():
            try:
                fn, a = self.q.get(timeout=max(0.01, end - time.monotonic()))
            except queue.Empty:
                break
            fn(*a)
        assert cond(), "timed out"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "available_gb", lambda: 64.0)
    loop = Loop()
    js = jobs.Jobs(sys.executable, FAKE, post=loop.post, on_finished=loop.finished.append, limit=2)
    folders = []
    for i in range(3):
        f = tmp_path / f"2026-11-0{i + 1}_100000"
        f.mkdir()
        (f / "audio-meeting.ogg").write_bytes(b"x")
        folders.append(f)
    return js, loop, folders


def test_runs_up_to_the_limit_and_the_rest_wait(setup):
    js, loop, folders = setup
    a, b, c = (js.add(f, "parakeet") for f in folders)
    assert [a.state, b.state, c.state] == ["running", "running", "queued"]
    loop.until(lambda: len(loop.finished) == 3)
    assert all(j.state == "done" for j in (a, b, c))
    for f in folders:
        assert (f / "transcript.txt").exists()
        assert not (f / meetings.JOB_FILE).exists() and not (f / meetings.PARTIAL_FILE).exists()
    assert js.all() == []


def test_pause_and_resume_carries_on(setup, monkeypatch):
    js, loop, folders = setup
    monkeypatch.setenv("FAKE_STEP", "0.3")
    job = js.add(folders[0], "parakeet")
    loop.until(lambda: job.done >= 2)
    js.pause(job)
    loop.until(lambda: job.state == "paused")
    done = job.done
    assert meetings.load_job(folders[0])["paused"] is True
    assert len(meetings.read_partial(folders[0])[1]) == done
    js.resume(job)
    loop.until(lambda: job.state == "done")
    lines = (folders[0] / "transcript.txt").read_text().splitlines()
    assert len(lines) == 6 and lines[0].endswith("word 0")


def test_waiting_job_can_be_paused_without_starting(setup):
    js, loop, folders = setup
    js.limit_setting = 1
    a, b = js.add(folders[0], "parakeet"), js.add(folders[1], "parakeet")
    js.pause(b)
    assert b.state == "paused" and b.proc is None
    loop.until(lambda: a.state == "done")
    assert b.state == "paused"      # stays paused: it was your choice


def test_restore_after_quitting(setup, monkeypatch):
    js, loop, folders = setup
    monkeypatch.setenv("FAKE_STEP", "0.3")
    job = js.add(folders[0], "parakeet")
    loop.until(lambda: job.done >= 1)
    quit_done = []
    js.suspend_all(lambda: quit_done.append(True))
    loop.until(lambda: quit_done)
    info = meetings.load_job(folders[0])
    assert info is not None and info["paused"] is False     # quitting isn't a pause: it carries on next time
    again = jobs.Jobs(sys.executable, FAKE, post=loop.post, on_finished=loop.finished.append, limit=2)
    restored = again.restore(folders[0].parent)
    assert len(restored) == 1 and restored[0].state == "running"
    loop.until(lambda: restored[0].state == "done")
    assert len((folders[0] / "transcript.txt").read_text().splitlines()) == 6


def test_cancel_forgets_progress(setup):
    js, loop, folders = setup
    job = js.add(folders[0], "parakeet")
    loop.until(lambda: job.done >= 1)
    js.cancel(job)
    loop.until(lambda: job in loop.finished)
    assert job.state == "cancelled" and js.get(folders[0]) is None
    assert not (folders[0] / meetings.JOB_FILE).exists()
    assert not (folders[0] / meetings.PARTIAL_FILE).exists()


def test_failure_waits_paused_for_a_retry(setup, monkeypatch):
    js, loop, folders = setup
    monkeypatch.setattr(js, "worker", str(FAKE.with_name("does-not-exist.py")))
    job = js.add(folders[0], "parakeet")
    loop.until(lambda: job in loop.finished)
    assert job.state == "paused" and job.error and js.get(folders[0]) is job
    assert "Resume" in job.status()


def test_command_uses_the_model_and_threads(setup):
    js, _loop, folders = setup
    job = jobs.Job(folders[0], "again", "whisper-small", "en")
    cmd = js._command(job)
    assert cmd[cmd.index("--engine") + 1] == "whisper" and cmd[cmd.index("--model") + 1] == "small"
    assert "--resume" in cmd and int(cmd[cmd.index("--threads") + 1]) >= 2


def test_limits():
    assert jobs.auto_limit(4) == 1 and jobs.auto_limit(8) == 2 and jobs.auto_limit(16) == 3
    assert jobs.threads_for(1, 16) == 8 and jobs.threads_for(3, 16) == 4 and jobs.threads_for(4, 4) == 2


def test_partial_lines(tmp_path):
    (tmp_path / meetings.PARTIAL_FILE).write_text(
        '{"engine": "parakeet"}\n{"start": 5, "end": 6, "stream": "Them", "text": "later", "label": "Speaker 1"}\n'
        '{"start": 1, "end": 2, "stream": "You", "text": ""}\n{"start": 0, "end": 1, "stream": "You", "text": "hi"}\n'
        '{"start": 9, "end"')     # cut short by a crash
    header, chunks = meetings.read_partial(tmp_path)
    assert header["engine"] == "parakeet" and len(chunks) == 3
    assert meetings.partial_lines(tmp_path) == ["[00:00:00] You: hi", "[00:00:05] Speaker 1: later"]
