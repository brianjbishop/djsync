#!/bin/sh
# Install or remove the djsync LaunchAgent (idempotent).
#
# Usage:
#   ./scripts/install-agent.sh
#   ./scripts/install-agent.sh --uninstall

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.brianjbishop.djsync.agent"
TEMPLATE="$ROOT/scripts/${LABEL}.plist"
DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG="${HOME}/Library/Logs/djsync-agent.log"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}/${LABEL}"

is_loaded() {
  launchctl print "$DOMAIN" >/dev/null 2>&1
}

unload_if_present() {
  if is_loaded; then
    launchctl bootout "gui/${UID_NUM}" "$DEST" 2>/dev/null \
      || launchctl bootout "$DOMAIN" 2>/dev/null \
      || true
    echo "Unloaded LaunchAgent: ${DOMAIN}"
  else
    echo "LaunchAgent not loaded: ${DOMAIN}"
  fi
}

uninstall() {
  unload_if_present
  if [ -f "$DEST" ]; then
    rm -f "$DEST"
    echo "Removed ${DEST}"
  else
    echo "No plist to remove at ${DEST}"
  fi
  echo "Uninstall complete for ${LABEL}."
  echo "Log file left in place: ${LOG}"
}

install() {
  if [ ! -x "$ROOT/djsync-run" ]; then
    echo "error: ${ROOT}/djsync-run is missing or not executable" >&2
    exit 1
  fi
  if [ ! -f "$TEMPLATE" ]; then
    echo "error: missing template ${TEMPLATE}" >&2
    exit 1
  fi

  mkdir -p "${HOME}/Library/LaunchAgents"
  mkdir -p "${HOME}/Library/Logs"
  touch "$LOG"

  tmp="$(mktemp)"
  sed -e "s|__ROOT__|${ROOT}|g" -e "s|__LOG__|${LOG}|g" "$TEMPLATE" > "$tmp"
  mv "$tmp" "$DEST"
  echo "Wrote ${DEST}"

  unload_if_present
  launchctl bootstrap "gui/${UID_NUM}" "$DEST"
  echo "Loaded LaunchAgent: ${DOMAIN}"
  echo "Program: ${ROOT}/djsync-run agent"
  echo "StartInterval: 7200 seconds (2 hours)"
  echo "RunAtLoad: true"
  echo "WatchPaths: /Volumes"
  echo "Logs: ${LOG}"
  echo "Install complete for ${LABEL}."
}

case "${1:-}" in
  --uninstall|-u)
    uninstall
    ;;
  ""|--install)
    install
    ;;
  *)
    echo "Usage: $0 [--install|--uninstall]" >&2
    exit 2
    ;;
esac
