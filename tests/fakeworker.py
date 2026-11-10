"""Stands in for worker.py in tests: "transcribes" CHUNKS chunks of a folder, keeping progress like the real one
(meetings.PARTIAL_FILE), pausing on SIGUSR1 and carrying on with --resume. No audio, no models."""
import json
import os
import pathlib
import signal
import sys
import time

CHUNKS = int(os.environ.get("FAKE_CHUNKS", "6"))
STEP = float(os.environ.get("FAKE_STEP", "0.15"))


def say(msg):
    print(msg, flush=True)


def main():
    args = sys.argv[1:]
    folder = pathlib.Path(args[args.index("--retranscribe") + 1])
    partial = folder / "transcript.partial.jsonl"
    done = 0
    if "--resume" in args and partial.exists():
        done = sum(1 for line in partial.read_text().splitlines()[1:] if line.strip())
    else:
        partial.write_text(json.dumps({"engine": "parakeet", "model": None}) + "\n")
    (folder / "fake-args.json").write_text(json.dumps(args))

    def pause(*_):
        say("STATE: paused")
        os._exit(0)
    signal.signal(signal.SIGUSR1, pause)
    say("STATE: loading")
    for i in range(done):
        say(f"[00:00:{i:02d}] You: word {i}")
    say(f"STATE: finishing done={done} total={CHUNKS}")
    with open(partial, "a") as f:
        for i in range(done, CHUNKS):
            time.sleep(STEP)
            f.write(json.dumps({"start": i, "end": i + 1, "stream": "You", "text": f"word {i}", "label": "You"}) + "\n")
            f.flush()
            say(f"[00:00:{i:02d}] You: word {i}")
            say(f"STATE: finishing done={i + 1} total={CHUNKS}")
    (folder / "transcript.txt").write_text("".join(f"[00:00:{i:02d}] You: word {i}\n" for i in range(CHUNKS)))
    partial.unlink()
    say(f"DONE: {folder / 'transcript.txt'}")


if __name__ == "__main__":
    main()
