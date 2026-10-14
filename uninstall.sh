#!/usr/bin/env bash
# prn uninstall: stops the menu bar app, removes the login item and the prn command,
# and optionally deletes your config, cached state, and terminal-notifier.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.prn.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
POINTER="$HOME/.config/prn/config-path"
STATE_DIR="$HOME/.local/state/prn"

bold=$'\033[1m'; red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; reset=$'\033[0m'
step() { printf '\n%s==>%s %s%s%s\n' "$green" "$reset" "$bold" "$*" "$reset"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    %s⚠ %s%s\n' "$yellow" "$*" "$reset"; }
have() { command -v "$1" >/dev/null 2>&1; }
confirm() {
  local prompt="$1" default="${2:-N}" reply hint="[y/N]"
  [[ "$default" == "Y" ]] && hint="[Y/n]"
  read -r -p "    $prompt $hint " reply </dev/tty || reply=""
  reply="${reply:-$default}"
  [[ "$reply" =~ ^[Yy] ]]
}

printf '%sprn uninstall%s\n' "$bold" "$reset"

# Work out where the config lives before removing the pointer to it.
CONFIG="$REPO_DIR/config.yaml"
if [[ -f "$POINTER" ]]; then
  CONFIG="$(head -n1 "$POINTER")"
fi

# ------------------------------------------------------------------ 1. daemon
step "Stopping the menu bar app"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload -w "$PLIST" 2>/dev/null || true
if [[ -f "$PLIST" ]]; then rm -f "$PLIST"; info "Removed $PLIST"; else info "No login item installed."; fi
pkill -f "prn menubar" 2>/dev/null || true

# ------------------------------------------------------------------ 2. the prn command
step "Removing the prn command"
if have uv && uv tool list 2>/dev/null | grep -q '^prn '; then
  uv tool uninstall prn
else
  info "prn is not installed via uv."
fi

# ------------------------------------------------------------------ 3. data
step "Your data"
shopt -s nullglob
backups=("$CONFIG".bak-*)
if [[ -f "$CONFIG" ]]; then
  if confirm "Delete your config ($CONFIG${backups:+ and ${#backups[@]} backup(s)})?" N; then
    rm -f "$CONFIG"
    for b in ${backups[@]+"${backups[@]}"}; do rm -f "$b"; done   # bash 3.2-safe with set -u
    info "Deleted config."
  else
    info "Kept $CONFIG (re-run ./setup.sh and choose 'keep' to reuse it)."
  fi
else
  info "No config file found."
fi

if [[ -d "$STATE_DIR" ]]; then
  if confirm "Delete cached inbox, notification state, and logs ($STATE_DIR)?" Y; then
    rm -rf "$STATE_DIR"
    info "Deleted $STATE_DIR"
  fi
fi

# The pointer only tells prn where the config is; it's useless without prn.
rm -f "$POINTER"
rmdir "$HOME/.config/prn" 2>/dev/null || true

# ------------------------------------------------------------------ 4. optional brew dependency
if have brew && brew list --formula terminal-notifier >/dev/null 2>&1; then
  step "Optional dependencies"
  info "gh, uv, and Homebrew are left alone (other tools rely on them)."
  if confirm "Uninstall terminal-notifier (installed for prn's clickable notifications)?" N; then
    brew uninstall terminal-notifier
  fi
fi

printf '\n%s✓ prn has been uninstalled.%s\n' "$green" "$reset"
[[ -f "$CONFIG" ]] && info "Your config is still at $CONFIG"
info "You can delete this folder ($REPO_DIR) whenever you like."
