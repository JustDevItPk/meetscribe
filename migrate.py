"""Moves data left by MeetScribe (this app's former name) to MintoMeet's places, once.

  ~/MeetScribe                  -> ~/MintoMeet                 meetings
  ~/.config/meetscribe          -> ~/.config/mintomeet         settings, speakers, learned voices, API-key file
  ~/.claude/projects/<…MeetScribe-…> -> <…MintoMeet-…>         Ask Agent's Claude Code sessions (kept per folder)

A folder is only moved when the new one doesn't exist yet, and never while the old app is running. The app
itself (~/.local/share/meetscribe) is handled by install.sh. Pure Python, no third-party imports.
"""
import os
import pathlib
import re
import subprocess

HOME = pathlib.Path.home()
MOVES = [(HOME / "MeetScribe", HOME / "MintoMeet"),
         (HOME / ".config/meetscribe", HOME / ".config/mintomeet")]
CLAUDE_PROJECTS = HOME / ".claude/projects"


def old_app_running():
    """True when MeetScribe's window or recorder is still running."""
    try:
        return subprocess.run(["pgrep", "-f", r"meetscribe/(gui|worker)\.py"], capture_output=True).returncode == 0
    except OSError:
        return False


def claude_dir(path):
    """The folder name Claude Code keeps a working directory's sessions under."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def run(moves=None, projects=None, check_running=True):
    """Move what's there; returns a list of (old, new) that moved. Safe to call on every start."""
    moves = MOVES if moves is None else moves
    projects = CLAUDE_PROJECTS if projects is None else projects
    todo = [(old, new) for old, new in moves if old.is_dir() and not old.is_symlink() and not new.exists()]
    if not todo or (check_running and old_app_running()):
        return []
    moved = []
    for old, new in todo:
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            os.rename(old, new)
            moved.append((old, new))
        except OSError:
            continue
        # Ask Agent runs Claude Code inside each meeting folder, so its sessions follow the folder's name.
        old_prefix, new_prefix = claude_dir(old) + "-", claude_dir(new) + "-"
        try:
            entries = list(projects.iterdir()) if projects.is_dir() else []
        except OSError:
            entries = []
        for e in entries:
            if e.name.startswith(old_prefix):
                target = projects / (new_prefix + e.name[len(old_prefix):])
                if not target.exists():
                    try:
                        os.rename(e, target)
                    except OSError:
                        pass
    return moved


if __name__ == "__main__":
    for old, new in run():
        print(f"Moved {old} to {new}")
