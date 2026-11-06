#!/usr/bin/env bash
# Installs (or updates) MeetScribe for the current user, no sudo needed:
#   - a private Python 3.12 with the speech engines under ~/.local/share/meetscribe
#   - the `meetscribe` command in ~/.local/bin and an entry in your app menu
#   - the Parakeet and speaker-recognition models (about 680 MB, downloaded once)
# Run it again after pulling new code to update. Remove everything with ./uninstall.sh.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dest="$HOME/.local/share/meetscribe"
FILES=(worker gui chat speakers voices voicereview datasize storage meetings procstats tray callwatch export summarize version)

hint() {
  if command -v apt >/dev/null; then echo "  sudo apt install $1"
  elif command -v dnf >/dev/null; then echo "  sudo dnf install $2"
  elif command -v pacman >/dev/null; then echo "  sudo pacman -S $3"
  else echo "  (install: $1)"; fi
}

missing=0
for c in parec pactl ffmpeg curl; do
  command -v "$c" >/dev/null || { echo "Missing '$c'."; missing=1; }
done
if [ "$missing" = 1 ]; then
  echo "Install the audio tools with:"; hint "pulseaudio-utils ffmpeg curl" "pulseaudio-utils ffmpeg curl" "libpulse ffmpeg curl"
  exit 1
fi
if ! /usr/bin/python3 - <<'PY' 2>/dev/null
import sys, gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw
sys.exit(0 if (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 6) else 1)
PY
then
  echo "MeetScribe needs GTK 4 and libadwaita 1.6 or newer for Python (Ubuntu 25.04+, Fedora 41+, Debian 13+)."
  hint "python3-gi gir1.2-gtk-4.0 gir1.2-adw-1" "python3-gobject gtk4 libadwaita" "python-gobject gtk4 libadwaita"
  exit 1
fi

case "$(uname -m)" in
  x86_64) arch=x86_64 ;;
  aarch64|arm64) arch=aarch64 ;;
  *) echo "Unsupported processor: $(uname -m). MeetScribe runs on x86_64 and ARM64 Linux."; exit 1 ;;
esac

mkdir -p "$dest/uv" "$HOME/.local/bin" "$HOME/.local/share/applications"

if [ ! -x "$dest/uv/uv" ]; then
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  base=https://github.com/astral-sh/uv/releases/latest/download
  name="uv-$arch-unknown-linux-gnu.tar.gz"
  curl -fsSL -o "$tmp/$name" "$base/$name"
  curl -fsSL -o "$tmp/$name.sha256" "$base/$name.sha256"
  [ "$(cut -d' ' -f1 "$tmp/$name.sha256")" = "$(sha256sum "$tmp/$name" | cut -d' ' -f1)" ] \
    || { echo "uv download failed its checksum"; exit 1; }
  mkdir "$tmp/x" && tar -xzf "$tmp/$name" -C "$tmp/x"
  cp "$tmp"/x/*/uv "$dest/uv/uv"
fi

export UV_PYTHON_INSTALL_DIR="$dest/python"
[ -x "$dest/venv/bin/python" ] || "$dest/uv/uv" venv --python 3.12 "$dest/venv"
"$dest/uv/uv" pip install --python "$dest/venv/bin/python" -r "$here/requirements.txt"

for f in "${FILES[@]}"; do cp "$here/$f.py" "$dest/"; done
cp "$here/meetscribe" "$dest/"
"$dest/venv/bin/python" "$dest/worker.py" --prepare
chmod +x "$dest/meetscribe"
ln -sf "$dest/meetscribe" "$HOME/.local/bin/meetscribe"
rm -f "$HOME/.local/share/applications/meetscribe.desktop"    # renamed: notifications need the app id as the file name
cat > "$HOME/.local/share/applications/dev.meetscribe.app.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=MeetScribe
Comment=Record and transcribe meeting audio locally
Exec=$dest/meetscribe
Icon=audio-input-microphone
Terminal=false
Categories=AudioVideo;Audio;
DESKTOP
echo "Installed MeetScribe $("$dest/meetscribe" --version | cut -d' ' -f2). Open it from the app menu, or run: meetscribe"
