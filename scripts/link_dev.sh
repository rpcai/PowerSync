#!/usr/bin/env bash
# Re-create the dev symlink after a HACS update wipes it.
# Run this when you want local changes reflected in Home Assistant.

set -euo pipefail

SRC="/home/engineer/dev/PowerSync/custom_components/power_sync"
DST="/home/engineer/docker/home-assistant/data/config/custom_components/power_sync"

if [ -L "$DST" ]; then
  echo "Symlink already in place: $DST -> $(readlink "$DST")"
  exit 0
fi

if [ -e "$DST" ]; then
  echo "Removing HACS-installed directory..."
  sudo rm -rf "$DST"
fi

sudo ln -s "$SRC" "$DST"
echo "Done: $DST -> $SRC"
echo "Restart Home Assistant to pick up any changes."
