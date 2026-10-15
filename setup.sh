#!/usr/bin/env bash
# PR Review Notifier (prn) setup: installs dependencies, configures prn, builds the
# "PR Review Notifier" macOS app, and starts the menu bar app.
# Safe to re-run: it reinstalls the latest code and lets you keep or reconfigure your config.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$REPO_DIR/config.yaml"
ORIGINAL_PATH="$PATH"   # the PATH your shell gives new terminals (before we add anything)

# ------------------------------------------------------------------ helpers
bold=$'\033[1m'; dim=$'\033[2m'; red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; reset=$'\033[0m'
step()  { printf '\n%s==>%s %s%s%s\n' "$green" "$reset" "$bold" "$*" "$reset"; }
info()  { printf '    %s\n' "$*"; }
warn()  { printf '    %s⚠ %s%s\n' "$yellow" "$*" "$reset"; }
die()   { printf '%s✗ %s%s\n' "$red" "$*" "$reset" >&2; exit 1; }
have()  { command -v "$1" >/dev/null 2>&1; }

# confirm "Question" [Y|N]: default Y
confirm() {
  local prompt="$1" default="${2:-Y}" reply hint="[Y/n]"
  [[ "$default" == "N" ]] && hint="[y/N]"
  read -r -p "    $prompt $hint " reply </dev/tty || reply=""
  reply="${reply:-$default}"
  [[ "$reply" =~ ^[Yy] ]]
}

load_brew_env() {
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [[ -x "$b" ]]; then eval "$("$b" shellenv)"; return 0; fi
  done
  return 1
}

brew_install() {  # brew_install <formula> <why> <required:yes|no>
  local formula="$1" why="$2" required="$3"
  if confirm "$formula is not installed ($why). Install it with Homebrew?"; then
    brew install "$formula"
  elif [[ "$required" == "yes" ]]; then
    die "$formula is required. Install it and re-run ./setup.sh"
  else
    warn "Skipping $formula."
    return 1
  fi
}

# ------------------------------------------------------------------ preflight
[[ "$(uname -s)" == "Darwin" ]] || die "prn's menu bar app and notifications need macOS."
[[ -t 0 || -e /dev/tty ]] || die "setup.sh is interactive; run it from a terminal."

printf '%sPR Review Notifier (prn) setup%s  %s(%s)%s\n' "$bold" "$reset" "$dim" "$REPO_DIR" "$reset"

# ------------------------------------------------------------------ 1. dependencies
step "Checking dependencies"
have brew || load_brew_env || true

need_brew=false
for tool in uv gh terminal-notifier; do have "$tool" || need_brew=true; done

if $need_brew && ! have brew; then
  if confirm "Homebrew is not installed (needed to install missing tools). Install it now?"; then
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    load_brew_env || die "Homebrew installed but not found on PATH. Open a new terminal and re-run."
  else
    warn "Continuing without Homebrew; missing tools must be installed manually."
  fi
fi

if ! have uv; then
  if have brew; then brew_install uv "installs and runs prn" yes
  elif confirm "uv is not installed. Install it with the official installer (astral.sh)?"; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  else die "uv is required. See https://docs.astral.sh/uv/"; fi
fi
have gh || { have brew && brew_install gh "used for GitHub auth" yes; } || die "GitHub CLI (gh) is required."
have terminal-notifier || { have brew && brew_install terminal-notifier "sends PR Review Notifier's notifications" yes; } \
  || die "terminal-notifier is required (brew install terminal-notifier)."
info "uv $(uv --version | awk '{print $2}') · gh $(gh --version | head -1 | awk '{print $3}')$(have terminal-notifier && echo ' · terminal-notifier ✓')"

# ------------------------------------------------------------------ 2. GitHub auth
step "Checking GitHub authentication"
if ! gh auth status --hostname github.com >/dev/null 2>&1; then
  info "You're not logged in to the GitHub CLI. Starting \`gh auth login\`…"
  gh auth login --hostname github.com --scopes read:org
fi
status_out="$(gh auth status --hostname github.com 2>&1 || true)"
if grep -q "Token scopes" <<<"$status_out" && ! grep -q "read:org" <<<"$status_out"; then
  warn "Your gh token is missing the read:org scope (used for team review requests)."
  if confirm "Add it now with \`gh auth refresh -s read:org\`?"; then
    gh auth refresh --hostname github.com --scopes read:org
  fi
fi
info "Logged in as $(gh api user --jq .login 2>/dev/null || echo '?')"

# ------------------------------------------------------------------ 3. install prn
step "Installing the prn command (also available as pr-review-notifier)"
# Editable install: prn runs straight from ./src, so `git pull` or local edits take effect
# without reinstalling, and the menu bar app restarts itself when the code changes.
uv tool install --force --reinstall --quiet --editable "$REPO_DIR"
BIN_DIR="$(uv tool dir --bin)"
PRN="$BIN_DIR/prn"
[[ -x "$PRN" ]] || die "prn was not installed to $BIN_DIR"
export PATH="$BIN_DIR:$PATH"
info "Installed $PRN"
case ":$ORIGINAL_PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    warn "$BIN_DIR is not on your shell PATH, so \`prn\` won't be found in new terminals."
    if confirm "Add it with \`uv tool update-shell\`?"; then uv tool update-shell; fi
    ;;
esac

# ------------------------------------------------------------------ 4. configure
step "Configuring"
"$PRN" setup --config "$CONFIG"

# ------------------------------------------------------------------ 5. first poll
step "Fetching your PRs (first run seeds state silently, no notification burst)"
if ! "$PRN" refresh; then
  die "First poll failed. Fix the error above, then re-run ./setup.sh (or run \`prn refresh\`)."
fi

# ------------------------------------------------------------------ 6. macOS app + notifications
step "Building the PR Review Notifier app (so macOS shows our name instead of \"Python\")"
if ! "$PRN" app install; then
  warn "Couldn't build the app; notifications will come from terminal-notifier instead. Details above."
fi
if confirm "Send a test notification? (macOS may ask you to allow notifications from PR Review Notifier)"; then
  "$PRN" test-notification || true
  info "If nothing appeared: System Settings → Notifications → PR Review Notifier → Allow."
fi

# ------------------------------------------------------------------ 7. menu bar daemon
step "Starting the menu bar app (and enabling it at login)"
"$PRN" daemon install

# ------------------------------------------------------------------ 8. health check
step "Health check"
"$PRN" doctor || warn "Some checks failed. See above."

printf '\n%s✓ PR Review Notifier is ready.%s\n' "$green" "$reset"
info "• Look for the inbox icon in your menu bar."
info "• Run ${bold}prn${reset} (or ${bold}pr-review-notifier${reset}) for the interactive inbox, ${bold}prn list${reset} for a quick view."
info "• Edit ${bold}$CONFIG${reset} (or run ${bold}prn config edit${reset}); re-run ./setup.sh anytime."
info "• Remove everything with ./uninstall.sh"
