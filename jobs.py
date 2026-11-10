"""Transcriptions running in the background, so the window stays free while meetings are transcribed.

Each meeting being transcribed is a Job. A job is either a recording (the recorder captures the call, and once you
stop it carries on transcribing what is left) or a "transcribe again" from a meeting's saved audio. Several can
run at once (up to `limit()`, also kept within free memory); the rest wait in line. Any job can be paused and
resumed: the recorder keeps every finished chunk in the meeting folder (meetings.PARTIAL_FILE), so it carries
on from where it stopped, also after quitting MintoMeet or a crash. What each job is transcribed with is kept in
meetings.JOB_FILE, and unfinished jobs come back when the app starts (restore()).

Pure Python (no GTK): the window passes `post`, which runs callbacks on its main loop.
"""
import fcntl
import os
import pathlib
import signal
import subprocess
import threading
import time

import meetings
import modelcatalog

STATES = ("capturing", "queued", "running", "pausing", "paused", "done", "cancelled")
ACTIVE = ("capturing", "queued", "running", "pausing", "paused")
HEADROOM_GB = 1.5       # memory kept free when starting one more transcription next to others


def cpu_count():
    return os.cpu_count() or 4


def auto_limit(cores=None):
    """How many meetings to transcribe at once by default: one on small computers, more on bigger ones. Each
    gets its own share of the processor, so running more at once doesn't make any single one quicker."""
    cores = cores or cpu_count()
    return 1 if cores < 8 else 2 if cores < 16 else 3


def threads_for(limit, cores=None):
    """CPU threads for each background transcription, leaving room for your other apps."""
    cores = cores or cpu_count()
    return max(2, min(8, cores // (limit + 1)))


def available_gb():
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2**20
    except (OSError, ValueError, IndexError):
        pass
    return None


def locked(folder):
    """True when a recorder (maybe from an earlier run of the app) is working on this folder right now."""
    path = pathlib.Path(folder) / meetings.PARTIAL_FILE
    try:
        with open(path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except FileNotFoundError:
        return False
    except OSError:
        return True


class Job:
    def __init__(self, folder, kind, model_id, language=None, speakers=True, added=None):
        self.folder = pathlib.Path(folder)
        self.kind = kind                # "recording" or "again"
        self.model_id = model_id
        self.language = language
        self.speakers = speakers
        self.added = added or time.time()
        self.state = "queued"
        self.done = 0                   # speech chunks transcribed (this run, counting ones done before)
        self.total = 0
        self.phase = ""                 # what it's doing besides transcribing ("Loading the speech model…")
        self.lines = []                 # transcript lines so far, "[HH:MM:SS] Label: text"
        self.error = None
        self.proc = None
        self.live_names = {}            # "Speaker 2" -> "Priya": names you gave while it was transcribing
        self.started_at = None
        self._ended = None              # "done", "paused" or "cancelled" as the recorder reported it
        self._user_pause = False        # paused by you (stays paused) rather than for quitting (resumes)

    @property
    def active(self):
        return self.state in ACTIVE

    @property
    def fraction(self):
        return self.done / self.total if self.total else 0.0

    def save(self, paused=False):
        try:
            meetings.save_job(self.folder, kind=self.kind, model=self.model_id, language=self.language,
                              speakers=self.speakers, paused=paused, added=self.added, done=self.done,
                              total=self.total)
        except OSError:
            pass

    def status(self):
        """Short text for lists: "Transcribing · 45%", "Paused at 45%", "Waiting"…"""
        pct = f"{self.fraction:.0%}" if self.total else ""
        if self.state == "capturing":
            return "Recording now…"
        if self.error and self.state == "paused":
            return "Transcription stopped · press Resume to try again"
        if self.state == "queued":
            return "Waiting to transcribe" + (f" (paused at {pct})" if self.done else "")
        if self.state in ("running", "pausing"):
            if self.state == "pausing":
                return "Pausing…"
            if self.phase:
                return self.phase
            return f"Transcribing · {pct}" if pct else "Transcribing…"
        if self.state == "paused":
            return f"Paused at {pct}" if pct else "Paused"
        return ""


class Jobs:
    """All transcriptions. Callbacks (run through `post` on the window's main loop):
    on_change(job), on_output(job, line) for every line the recorder prints, on_finished(job)."""

    def __init__(self, python, worker, on_change=None, on_output=None, on_finished=None,
                 post=lambda fn, *a: fn(*a), limit=0, extra_args=None):
        self.python, self.worker = str(python), str(worker)
        self.on_change = on_change or (lambda job: None)
        self.on_output = on_output or (lambda job, line: None)
        self.on_finished = on_finished or (lambda job: None)
        self.post = post
        self.limit_setting = limit          # 0 = automatic
        self.extra_args = extra_args or (lambda: [])     # e.g. ["--no-speakers"], asked each time a job starts
        self.jobs = {}                      # folder -> Job (active ones)
        self._then = None                   # called once nothing runs any more (quitting)

    # --- what's there ---------------------------------------------------------------------------------
    def get(self, folder):
        return self.jobs.get(pathlib.Path(folder)) if folder is not None else None

    def all(self):
        """Active jobs: the one recording first, then running, waiting and paused ones, oldest first."""
        order = {"capturing": 0, "running": 1, "pausing": 1, "queued": 2, "paused": 3}
        return sorted(self.jobs.values(), key=lambda j: (order.get(j.state, 4), j.added))

    def running(self):
        return [j for j in self.jobs.values() if j.state in ("running", "pausing")]

    def capturing(self):
        return next((j for j in self.jobs.values() if j.state == "capturing"), None)

    def limit(self):
        return self.limit_setting or auto_limit()

    def models_in_use(self):
        return {j.model_id for j in self.jobs.values() if j.proc is not None}

    # --- starting --------------------------------------------------------------------------------------
    def record(self, folder, cmd, model_id, language=None, speakers=True):
        """Start a recording now (never waits in line). `cmd` is the recorder's full command."""
        job = Job(folder, "recording", model_id, language, speakers)
        job.save()      # if the app stops mid-call, the audio is transcribed when it starts again
        job.state = "capturing"
        self.jobs[job.folder] = job
        if not self._launch(job, cmd):
            del self.jobs[job.folder]
            meetings.clear_job(job.folder)
            return None
        return job

    def stop_capture(self, job):
        """Stop recording; the recorder then transcribes what's left in the background."""
        if job.proc is not None and job.state == "capturing":
            job.proc.send_signal(signal.SIGTERM)
            job.state, job.phase = "running", "Finishing the recording…"
            self.on_change(job)

    def add(self, folder, model_id, language=None, speakers=True):
        """Transcribe a meeting again from its saved audio, as soon as there is room."""
        folder = pathlib.Path(folder)
        if folder in self.jobs:
            return self.jobs[folder]
        meetings.clear_job(folder)      # a fresh start, not the rest of an earlier, unfinished attempt
        job = Job(folder, "again", model_id, language, speakers)
        job.save()
        self.jobs[folder] = job
        self.on_change(job)
        self.schedule()
        return job

    def restore(self, meetings_dir):
        """Unfinished transcriptions from before (paused, waiting, or interrupted by quitting or a crash)."""
        root = pathlib.Path(meetings_dir)
        if not root.is_dir():
            return []
        found = []
        for folder in sorted(root.iterdir()):
            info = meetings.load_job(folder) if folder.is_dir() else None
            if info is None or folder in self.jobs or locked(folder):
                continue
            job = Job(folder, info.get("kind", "again"), info["model"], info.get("language"),
                      info.get("speakers", True), info.get("added"))
            job.lines = meetings.partial_lines(folder)
            job.done, job.total = info.get("done", 0), info.get("total", 0)
            job.state = "paused" if info.get("paused") else "queued"
            job._user_pause = bool(info.get("paused"))
            self.jobs[folder] = job
            found.append(job)
        self.schedule()
        return found

    def schedule(self):
        """Start waiting jobs while there's room (by the limit and by free memory)."""
        if self._then is not None:
            return
        waiting = sorted((j for j in self.jobs.values() if j.state == "queued"), key=lambda j: j.added)
        busy = len(self.running()) + (1 if self.capturing() else 0)
        for job in waiting:
            if busy >= self.limit():
                break
            m = modelcatalog.get(job.model_id)
            free = available_gb()
            if busy and m is not None and free is not None and free < m.ram_gb + HEADROOM_GB:
                break       # not enough memory for one more next to the others: it starts when one finishes
            if self._launch(job, self._command(job)):
                busy += 1

    def _command(self, job):
        m = modelcatalog.get(job.model_id)
        cmd = [self.python, self.worker, "--retranscribe", str(job.folder), "--resume",
               "--threads", str(threads_for(self.limit()))]
        if m is None or m.engine == "parakeet":
            cmd += ["--engine", "parakeet"]
        else:
            cmd += ["--engine", "whisper", "--model", m.model, "--language", job.language or "auto"]
        if not job.speakers:
            cmd.append("--no-speakers")
        return cmd + [a for a in self.extra_args() if a not in cmd]

    def _launch(self, job, cmd):
        job.error, job._ended, job.phase = None, None, ""
        job.lines = []          # the recorder repeats what was done before, then carries on
        try:
            job.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as e:
            job.state, job.error = "error", f"Could not start the recorder: {e}"
            self.on_change(job)
            return False
        if job.state != "capturing":
            job.state = "running"
            job.phase = "Starting…"
        job.started_at = time.monotonic()
        threading.Thread(target=self._read, args=(job, job.proc), daemon=True).start()
        self.on_change(job)
        return True

    def _read(self, job, proc):
        for line in proc.stdout:
            self.post(self._line, job, proc, line.rstrip("\n"))
        proc.wait()
        self.post(self._exited, job, proc, proc.returncode)

    # --- what the recorder says ------------------------------------------------------------------------
    def _line(self, job, proc, line):
        if proc is not job.proc:
            return False
        changed = False
        if line.startswith("STATE: "):
            words = line[7:].split()
            word, opts = words[0], dict(w.split("=", 1) for w in words[1:] if "=" in w)
            if word == "downloading" and job.state != "capturing":
                job.phase = f"Downloading the speech model{': ' + opts['pct'] + '%' if 'pct' in opts else '…'}"
            elif word == "loading" and job.state != "capturing":
                job.phase = "Loading the speech model…"
            elif word == "finishing" and "speakers" in words:
                job.phase = "Telling speakers apart…"
            elif word == "finishing":
                if job.state == "capturing":
                    job.state = "running"       # recording stopped: the rest is transcribed in the background
                job.done, job.total = int(opts.get("done", 0)), int(opts.get("total", 0))
                job.phase = ""
            elif word == "paused":
                job._ended = "paused"
            elif word == "cancelled":
                job._ended = "cancelled"
            changed = True
        elif line.startswith("ERROR: "):
            job.error = line[7:]
        elif line.startswith("DONE: "):
            job._ended = "done"
        elif meetings.LINE_RE.match(line):
            job.lines.append(line)
        self.on_output(job, line)
        if changed:
            self.on_change(job)
        return False

    def _exited(self, job, proc, code):
        if proc is not job.proc:
            return False
        job.proc = None
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        if job.state == "cancelled":
            meetings.clear_job(job.folder)
        elif job._ended == "paused" or job.state == "pausing":
            job.state = "paused" if job._user_pause else "queued"
            job.save(paused=job._user_pause)
            job.phase = ""
        elif job._ended == "done" and code == 0:
            job.state = "done"
            meetings.clear_job(job.folder)
        elif job._ended == "cancelled" and code == 0:
            # stopped before the speech model was ready: the audio is kept, to be transcribed later
            job.state, job._user_pause = "paused", True
            job.error = "Stopped before the speech model was ready. The audio is saved."
            job.save(paused=True)
        else:       # failed: it waits, paused, with what went wrong, until you press Resume to try again
            job.state, job._user_pause = "paused", True
            job.error = job.error or f"The recorder stopped unexpectedly (exit code {code})."
            job.save(paused=True)
        if not job.active:
            self.jobs.pop(job.folder, None)
        self.on_change(job)
        self.on_finished(job)
        if self._then is not None and not any(j.proc is not None for j in self.jobs.values()):
            then, self._then = self._then, None
            then()
        else:
            self.schedule()
        return False

    # --- your controls ---------------------------------------------------------------------------------
    def can_pause(self, job):
        return job.state in ("queued", "running")

    def pause(self, job):
        """Pause (a running one stops after saving its progress; it carries on from there when resumed)."""
        if job.state == "queued":
            job.state, job._user_pause = "paused", True
            job.save(paused=True)
            self.on_change(job)
        elif job.state == "running" and job.proc is not None:
            job._user_pause = True
            job.state = "pausing"
            job.proc.send_signal(signal.SIGUSR1)
            self.on_change(job)

    def resume(self, job):
        if job.state == "paused":
            job.state, job._user_pause, job.error = "queued", False, None
            job.save(paused=False)
            self.on_change(job)
            self.schedule()

    def cancel(self, job):
        """Stop and forget this transcription (the meeting keeps the transcript it had)."""
        if job.state == "capturing":
            return
        job.state = "cancelled"
        if job.proc is not None:
            job.proc.kill()         # its progress is thrown away, so no need to stop cleanly
        else:
            meetings.clear_job(job.folder)
            self.jobs.pop(job.folder, None)
            self.on_change(job)
            self.on_finished(job)
            self.schedule()

    def suspend_all(self, then):
        """For quitting: stop every transcription after saving its progress, then call `then`. They carry on
        the next time the app starts. A recording stops too: its audio is closed and saved first."""
        running = [j for j in self.jobs.values() if j.proc is not None]
        if not running:
            then()
            return
        self._then = then
        for job in running:
            if job.state in ("running", "capturing"):
                job.state = "pausing"
                job.proc.send_signal(signal.SIGUSR1)
