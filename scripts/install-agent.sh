#!/bin/sh
# Install or remove the djsync LaunchAgents (idempotent).
#
# Usage:
#   ./scripts/install-agent.sh
#   ./scripts/install-agent.sh --uninstall

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_LABEL="com.brianjbishop.djsync.agent"
REPORT_LABEL="com.brianjbishop.djsync.report"
AGENT_TEMPLATE="$ROOT/scripts/${AGENT_LABEL}.plist"
REPORT_TEMPLATE="$ROOT/scripts/${REPORT_LABEL}.plist"
AGENT_DEST="${HOME}/Library/LaunchAgents/${AGENT_LABEL}.plist"
REPORT_DEST="${HOME}/Library/LaunchAgents/${REPORT_LABEL}.plist"
AGENT_LOG="${HOME}/Library/Logs/djsync-agent.log"
REPORT_LOG="${HOME}/Library/Logs/djsync-report.log"
UID_NUM="$(id -u)"
AGENT_DOMAIN="gui/${UID_NUM}/${AGENT_LABEL}"
REPORT_DOMAIN="gui/${UID_NUM}/${REPORT_LABEL}"

is_loaded() {
  launchctl print "$1" >/dev/null 2>&1
}

unload_if_present() {
  domain="$1"
  dest="$2"
  if is_loaded "$domain"; then
    launchctl bootout "gui/${UID_NUM}" "$dest" 2>/dev/null \
      || launchctl bootout "$domain" 2>/dev/null \
      || true
    echo "Unloaded LaunchAgent: ${domain}"
  else
    echo "LaunchAgent not loaded: ${domain}"
  fi
}

uninstall_one() {
  label="$1"
  dest="$2"
  domain="$3"
  log="$4"
  unload_if_present "$domain" "$dest"
  if [ -f "$dest" ]; then
    rm -f "$dest"
    echo "Removed ${dest}"
  else
    echo "No plist to remove at ${dest}"
  fi
  echo "Log file left in place: ${log}"
}

install_one() {
  label="$1"
  template="$2"
  dest="$3"
  domain="$4"
  log="$5"
  sed_args="-e s|__ROOT__|${ROOT}|g -e s|__LOG__|${AGENT_LOG}|g -e s|__REPORT_LOG__|${REPORT_LOG}|g"
  tmp="$(mktemp)"
  # shellcheck disable=SC2086
  sed $sed_args "$template" > "$tmp"
  mv "$tmp" "$dest"
  echo "Wrote ${dest}"
  unload_if_present "$domain" "$dest"
  launchctl bootstrap "gui/${UID_NUM}" "$dest"
  echo "Loaded LaunchAgent: ${domain}"
}

uninstall() {
  uninstall_one "$AGENT_LABEL" "$AGENT_DEST" "$AGENT_DOMAIN" "$AGENT_LOG"
  uninstall_one "$REPORT_LABEL" "$REPORT_DEST" "$REPORT_DOMAIN" "$REPORT_LOG"
  echo "Uninstall complete for ${AGENT_LABEL} and ${REPORT_LABEL}."
}

install() {
  if [ ! -x "$ROOT/djsync-run" ]; then
    echo "error: ${ROOT}/djsync-run is missing or not executable" >&2
    exit 1
  fi
  if [ ! -f "$AGENT_TEMPLATE" ]; then
    echo "error: missing template ${AGENT_TEMPLATE}" >&2
    exit 1
  fi
  if [ ! -f "$REPORT_TEMPLATE" ]; then
    echo "error: missing template ${REPORT_TEMPLATE}" >&2
    exit 1
  fi

  mkdir -p "${HOME}/Library/LaunchAgents"
  mkdir -p "${HOME}/Library/Logs"
  touch "$AGENT_LOG" "$REPORT_LOG"

  install_one "$AGENT_LABEL" "$AGENT_TEMPLATE" "$AGENT_DEST" "$AGENT_DOMAIN" "$AGENT_LOG"
  echo "Program: ${ROOT}/djsync-run agent"
  echo "StartInterval: 21600 seconds (6 hours)"
  echo "RunAtLoad: true"
  echo "WatchPaths: /Volumes"
  echo "Logs: ${AGENT_LOG}"
  echo "Install complete for ${AGENT_LABEL}."
  echo ""

  install_one "$REPORT_LABEL" "$REPORT_TEMPLATE" "$REPORT_DEST" "$REPORT_DOMAIN" "$REPORT_LOG"
  echo "Program: ${ROOT}/djsync-run report --beeper"
  echo "StartCalendarInterval: daily at 08:00 local"
  echo "Logs: ${REPORT_LOG}"
  echo "Install complete for ${REPORT_LABEL}."
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
