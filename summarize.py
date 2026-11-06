#!/usr/bin/env python3
"""Summarize a meeting folder from the terminal: meetscribe summarize FOLDER (uses the AI agent chosen in the app)."""
import sys
import threading

import agents
import chat

PROMPT = ("Summarize this meeting with these sections: Summary, Decisions, Action items "
          "(with owner and deadline if mentioned), Open questions.")


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: summarize.py <meeting-folder>")
    cfg = agents.load_config()
    issue = chat.agent_problem(cfg)
    if issue:
        sys.exit(f"{issue} Choose an AI agent in MeetScribe (Preferences → AI agent).")
    print(f"Asking {agents.describe(cfg)}…\n", file=sys.stderr)
    done = threading.Event()
    result = {}

    def finished(text, error):
        result.update(text=text, error=error)
        done.set()

    chat.Chat(sys.argv[1]).send(PROMPT, lambda chunk: print(chunk, end="", flush=True), finished, agent=cfg)
    done.wait()
    print()
    if result["error"]:
        sys.exit(result["error"])


if __name__ == "__main__":
    main()
