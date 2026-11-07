"""Notice when a call app starts or stops using the microphone (PipeWire / PulseAudio source outputs)."""
import json
import subprocess
import threading

from gi.repository import GLib

POLL_SECONDS = 3
START_POLLS = 2         # mic must be in use for ~6 s before we ask (ignores quick mic checks)
END_POLLS = 3           # ...and released for ~9 s before we call the meeting over
IGNORE = {"mintomeet", "parec", "open-whispr", "open-whispr-app", "speech-dispatcher", "gnome-shell",
          "pipewire", "wireplumber", "pavucontrol", "gnome-control-center"}
NAMES = {"zoom": "Zoom", "zoomwebinar": "Zoom", "lark": "Lark", "feishu": "Feishu", "teams": "Microsoft Teams",
         "teams-for-linux": "Microsoft Teams", "slack": "Slack", "skypeforlinux": "Skype", "discord": "Discord",
         "chrome": "Google Chrome", "google-chrome": "Google Chrome", "chromium": "Chromium",
         "firefox": "Firefox", "brave": "Brave", "msedge": "Microsoft Edge", "webex": "Webex"}


def mic_users():
    """Friendly names of apps currently recording from a microphone (not from a speaker monitor)."""
    try:
        outs = json.loads(subprocess.run(["pactl", "-f", "json", "list", "source-outputs"], capture_output=True,
                                         text=True, timeout=3).stdout or "[]")
        sources = json.loads(subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True,
                                            text=True, timeout=3).stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()
    monitors = {s.get("index") for s in sources if str(s.get("name", "")).endswith(".monitor")}
    found = set()
    for o in outs:
        props = o.get("properties", {})
        binary = str(props.get("application.process.binary", "")).lower()
        app = str(props.get("application.name", "")).lower()
        if o.get("source") in monitors or binary in IGNORE or app in IGNORE or o.get("corked"):
            continue
        key = next((k for k in NAMES if k == binary or binary.startswith(k) or k in app), None)
        found.add(NAMES[key] if key else (props.get("application.name") or binary or "An app"))
    return found


class CallWatch:
    """Calls on_start(app_name) when a call seems to begin and on_end(app_name) when it seems over."""

    def __init__(self, on_start, on_end):
        self.on_start, self.on_end = on_start, on_end
        self.active = None          # app we consider "in a call"
        self.seen = 0
        self.gone = 0
        self.enabled = True
        self.busy = False
        GLib.timeout_add_seconds(POLL_SECONDS, self._tick)

    def _tick(self):
        if self.enabled and not self.busy:
            self.busy = True
            threading.Thread(target=self._poll, daemon=True).start()
        return True

    def _poll(self):
        users = mic_users()
        GLib.idle_add(self._update, users)

    def _update(self, users):
        self.busy = False
        if self.active is None:
            self.seen = self.seen + 1 if users else 0
            if self.seen >= START_POLLS:
                self.active = sorted(users)[0]
                self.gone = 0
                self.on_start(self.active)
        else:
            self.gone = 0 if self.active in users else self.gone + 1
            if self.gone >= END_POLLS:
                app, self.active, self.seen = self.active, None, 0
                self.on_end(app)
        return False
