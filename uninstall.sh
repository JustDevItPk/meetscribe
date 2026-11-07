#!/usr/bin/env bash
# Removes MintoMeet: the app, its private Python and its models, the `mintomeet` command, the app-menu
# entry and the log-in autostart entry. Your meetings (~/MintoMeet) and settings, speakers and learned
# voices (~/.config/mintomeet) are kept unless you pass --purge.
set -euo pipefail
dest="$HOME/.local/share/mintomeet"
purge=0
[ "${1:-}" = "--purge" ] && purge=1

if pgrep -f "mintomeet/(gui|worker)\.py" >/dev/null; then
  echo "MintoMeet is running. Quit it from its tray menu (and finish any recording) first."; exit 1
fi
rm -rf "$dest"
rm -f "$HOME/.local/bin/mintomeet" \
      "$HOME/.local/share/applications/dev.mintomeet.app.desktop" \
      "$HOME/.config/autostart/dev.mintomeet.app.desktop"
echo "Removed the MintoMeet app."

if [ "$purge" = 1 ]; then
  read -r -p "Also delete ALL your meetings in ~/MintoMeet and your settings and learned voices? Type yes: " answer
  if [ "$answer" = "yes" ]; then
    rm -rf "$HOME/MintoMeet" "$HOME/.config/mintomeet"
    echo "Deleted your meetings and settings."
  else
    echo "Kept your meetings and settings."
  fi
else
  echo "Kept your meetings (~/MintoMeet) and settings (~/.config/mintomeet). Use --purge to delete them too."
fi
echo "Whisper models downloaded by MintoMeet stay in the shared Hugging Face cache (~/.cache/huggingface/hub)."
