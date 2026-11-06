# Contributing to MeetScribe

Thanks for helping! Bug reports, ideas, documentation fixes and code are all welcome.

## Reporting bugs and asking for features

- Use the [issue templates](../../issues/new/choose). For bugs, include `meetscribe --version`, your distribution
  and desktop, the speech model you used, and any errors from `journalctl --user -t gui.py --since today`.
- **Never attach real meeting recordings, transcripts or `~/.config/meetscribe` files** to an issue: they contain
  other people's voices and words. If a bug needs audio to reproduce, describe it or record a short sample of
  your own voice.
- Security problems go through [SECURITY.md](SECURITY.md), not public issues.

## Setting up

1. Install the system packages and run `./install.sh` once, as in the [README](README.md). This creates the private
   Python with the speech engines in `~/.local/share/meetscribe/venv` and downloads the models.
2. To run the window **straight from your clone**, point it at that Python and start it with the system Python:

   ```bash
   ln -s ~/.local/share/meetscribe/venv venv     # gui.py looks for venv/ next to itself
   python3 gui.py
   ```

   Quit any installed MeetScribe first (from its tray menu): only one copy runs at a time.
3. To try your changes as an installed app, run `./install.sh` again; it only copies files and updates packages.

Use test folders so you don't touch your real meetings:

```bash
MEETSCRIBE_MEETINGS_DIR=/tmp/ms-meetings MEETSCRIBE_CONFIG_DIR=/tmp/ms-config python3 gui.py
```

The recorder also runs on its own, e.g. to transcribe a file:
`venv/bin/python worker.py --file sample.ogg --out /tmp/ms-out`.

## Tests and checks

```bash
python3 -m pytest              # or: uvx pytest
uvx ruff check .
shellcheck install.sh uninstall.sh meetscribe
```

CI runs the same checks on every pull request. The tests cover the pure-Python modules; for changes to the window
or the recorder, please also describe how you tried them (see *Testing* in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for ways to do that without a real call).

## Code guidelines

- Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first. In short: `gui.py` and its dialogs run on the system
  Python with GTK; `worker.py` and `export.py` run in MeetScribe's own Python; `meetings.py` and `voices.py` are
  shared and must stay pure Python.
- **New module?** Add it to `FILES` in `install.sh`.
- Python 3.10+, standard library first. New dependencies for the recorder go in `requirements.txt`; the window
  can't use pip packages at all.
- Keep the style of the surrounding code: small functions, a docstring that says *why*, lines up to 120
  characters (`ruff` allows 150).
- **UI text** is written for people who aren't technical: plain words, say what will happen, no jargon. Errors
  should say what to do next.
- Anything that changes recordings, transcripts or learned voices must not lose user data, and should be
  undoable where it's practical.
- Add or update tests for pure-Python logic, and add a line to [CHANGELOG.md](CHANGELOG.md) under *Unreleased*.

## Pull requests

1. Fork, create a branch from `main`, make focused commits with clear messages.
2. Run the tests and checks above.
3. Open the pull request and fill in the template. Screenshots help for UI changes; please use made-up meetings,
   not real ones.

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE), and to follow
our [Code of Conduct](CODE_OF_CONDUCT.md).
