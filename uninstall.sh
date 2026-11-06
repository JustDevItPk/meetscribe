#!/usr/bin/env bash
# Removes MeetScribe: the app, its private Python and its models, the `meetscribe` command, the app-menu
# entry and the log-in autostart entry. Your meetings (~/MeetScribe) and settings, speakers and learned
# voices (~/.config/meetscribe) are kept unless you pass --purge.
set -euo pipefail
dest="$HOME/.local/share/meetscribe"
purge=0
[ "${1:-}" = "--purge" ] && purge=1

if pgrep -f "meetscribe/(gui|worker)\.py" >/dev/null; then
  echo "MeetScribe is running. Quit it from its tray menu (and finish any recording) first."; exit 1
fi
rm -rf "$dest"
rm -f "$HOME/.local/bin/meetscribe" \
      "$HOME/.local/share/applications/dev.meetscribe.app.desktop" \
      "$HOME/.local/share/applications/meetscribe.desktop" \
      "$HOME/.config/autostart/dev.meetscribe.app.desktop"
echo "Removed the MeetScribe app."

if [ "$purge" = 1 ]; then
  read -r -p "Also delete ALL your meetings in ~/MeetScribe and your settings and learned voices? Type yes: " answer
  if [ "$answer" = "yes" ]; then
    rm -rf "$HOME/MeetScribe" "$HOME/.config/meetscribe"
    echo "Deleted your meetings and settings."
  else
    echo "Kept your meetings and settings."
  fi
else
  echo "Kept your meetings (~/MeetScribe) and settings (~/.config/meetscribe). Use --purge to delete them too."
fi
echo "Whisper models downloaded by MeetScribe stay in the shared Hugging Face cache (~/.cache/huggingface/hub)."
