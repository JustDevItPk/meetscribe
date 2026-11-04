#!/usr/bin/env bash
# Installs MeetScribe for the current user (no sudo): a private Python 3.12 + faster-whisper
# under ~/.local/share/meetscribe, a `meetscribe` command and an app-menu entry.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dest="$HOME/.local/share/meetscribe"

for c in parec pactl ffmpeg curl; do
  command -v "$c" >/dev/null || { echo "Missing '$c'. Install it with: sudo apt install pulseaudio-utils ffmpeg curl"; exit 1; }
done
/usr/bin/python3 -c 'import gi; gi.require_version("Gtk","4.0")' 2>/dev/null \
  || { echo "GTK 4 for Python is missing. Install: sudo apt install python3-gi gir1.2-gtk-4.0"; exit 1; }

mkdir -p "$dest/uv" "$HOME/.local/bin" "$HOME/.local/share/applications"

if [ ! -x "$dest/uv/uv" ]; then
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  base=https://github.com/astral-sh/uv/releases/latest/download
  name=uv-x86_64-unknown-linux-gnu.tar.gz
  curl -fsSL -o "$tmp/$name" "$base/$name"
  curl -fsSL -o "$tmp/$name.sha256" "$base/$name.sha256"
  [ "$(cut -d' ' -f1 "$tmp/$name.sha256")" = "$(sha256sum "$tmp/$name" | cut -d' ' -f1)" ] \
    || { echo "uv download failed its checksum"; exit 1; }
  mkdir "$tmp/x" && tar -xzf "$tmp/$name" -C "$tmp/x"
  cp "$tmp"/x/*/uv "$dest/uv/uv"
fi

export UV_PYTHON_INSTALL_DIR="$dest/python"
[ -x "$dest/venv/bin/python" ] || "$dest/uv/uv" venv --python 3.12 "$dest/venv"
"$dest/uv/uv" pip install --python "$dest/venv/bin/python" faster-whisper sherpa-onnx python-docx fpdf2

cp "$here"/{worker,gui,chat,speakers,voices,meetings,procstats,tray,callwatch,export,summarize}.py "$here/meetscribe" "$dest/"
"$dest/venv/bin/python" "$dest/worker.py" --prepare
chmod +x "$dest/meetscribe"
ln -sf "$dest/meetscribe" "$HOME/.local/bin/meetscribe"
rm -f "$HOME/.local/share/applications/meetscribe.desktop"    # renamed: notifications need the app id as the file name
cat > "$HOME/.local/share/applications/dev.meetscribe.app.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=MeetScribe
Comment=Record and transcribe meeting audio locally
Exec=$dest/meetscribe
Icon=audio-input-microphone
Terminal=false
Categories=AudioVideo;Audio;
EOF
echo "Installed. Open MeetScribe from the app menu, or run: meetscribe"
