# prn: PR Review Notifier

A local GitHub PR inbox for macOS. It has a menu bar icon with a dropdown, desktop notifications, and an interactive terminal inbox. Sections and filters live in a YAML file you can edit by hand. It's modeled on Graphite's inbox.

## Install

You need macOS. Everything else is handled by the setup script.

```bash
git clone <this repo> && cd pr-review-notifier
./setup.sh
```

`setup.sh` is interactive and safe to re-run. It:

1. Checks for Homebrew, [uv](https://docs.astral.sh/uv/), the GitHub CLI (`gh`), and `terminal-notifier`, and offers to install anything missing. It asks before installing each one.
2. Makes sure `gh` is logged in with the `read:org` scope. prn reuses the `gh` token, so it never stores a token of its own.
3. Installs the `prn` command with `uv tool install`, and offers to add uv's bin directory to your PATH.
4. Runs the setup wizard (`prn setup`), which creates `./config.yaml` from `config.example.yaml`. The wizard asks for:
   - **Repos to watch.** It checks that you have access to each one.
   - **Teammates.** It suggests the people whose PRs you've reviewed most in the last 90 days.
   - **Which starter sections to keep.**
   - **Poll interval and notification types.**
5. Runs a first poll, which records the current state silently so you don't get a burst of notifications.
6. Sends a test notification, installs the menu bar app as a login item, and runs `prn doctor`.

If `./config.yaml` already exists, re-running setup lets you **keep** it (reinstall and restart only), **reconfigure** it with your current values as defaults, or start **fresh** from the example. Before overwriting, it backs up the old file to `config.yaml.bak-<timestamp>`.

To upgrade after pulling changes, re-run `./setup.sh` and choose **keep**.

### Uninstall

```bash
./uninstall.sh
```

This stops the menu bar app, removes the login item, and runs `uv tool uninstall prn`. It then asks whether to delete `config.yaml` and its backups (default: keep), whether to delete cached state and logs (default: delete), and whether to uninstall `terminal-notifier` (default: keep). It never removes `gh`, `uv`, or Homebrew.

### Your config stays private

`config.yaml`, which holds your repos and teammates, is gitignored. Only the generic `config.example.yaml` is committed. Setup stores the location of your config in `~/.config/prn/config-path`, so the installed CLI and the menu bar daemon always find it. To point prn at a different file, run `prn config use <path>`.

## Everyday use

| Command | What it does |
|---|---|
| `prn` | Interactive inbox. `↑/↓` move, `Enter` opens on GitHub, `y` copies the URL, `r` refreshes, `/` filters, `q` quits |
| `prn list [-s NAME] [--json] [-r]` | Print the inbox (optionally refresh first) |
| `prn refresh` | Poll now. Use it after finishing reviews so the inbox updates right away |
| `prn sections list` | Sections in evaluation order |
| `prn sections add NAME [flags] / -i` | Add a section with flags, or with prompts (`-i`) |
| `prn sections remove NAME` / `move NAME --to N` | Remove or reorder sections |
| `prn setup` | Re-run the configuration wizard on its own |
| `prn config edit` / `validate` / `path` / `use PATH` | Edit in `$EDITOR`, then validate. Show or change the active config file |
| `prn daemon install / uninstall / restart / status / logs -f` | Manage the login item |
| `prn doctor` | Health check |

**How the inbox stays current.** The menu bar app polls GitHub every `poll_interval_seconds` (default 300). Each poll rewrites `~/.local/state/prn/inbox.json`, and both the TUI and the menu bar redraw when that file changes. When you finish a review, the PR moves or disappears on the next poll. To skip the wait, press `r` in the TUI, choose **Refresh now** in the menu bar, or run `prn refresh`.

## Config reference

Your config is `./config.yaml` (run `prn config path` to confirm). The daemon reloads it on every poll, so no restart is needed. See `config.example.yaml` for a fully commented example.

```yaml
settings:
  poll_interval_seconds: 300   # >= 60
  notifier: terminal-notifier  # or osascript (notifications won't be clickable)
  hide_empty_sections: true    # menu bar only
  max_title_length: 50
  ignore_bot_reviews: false
  ignored_reviewers: [coderabbitai]   # never count toward approvals / has_reviews_from_others
  notifications: { new_in_section: true, re_review_requested: true, my_pr_status: true, ci_status: true }

defaults:
  repos: [your-org/repo-one, ...]              # used by any section without its own repos

groups:
  teammates: [teammate-one, ...]               # reference as group:teammates

sections:                                      # evaluated top-down; first match wins
  - name: Teammates Need Review
    notify: true      # notify when a PR newly enters this section
    badge: true       # count toward the menu bar number
    display: true     # false: still claims matching PRs, but hides the section from the menu bar, `prn`, and `prn list` (and never sends "New in section" notifications)
    filters: { authors: [group:teammates], draft: false, approvals: { max: 0 } }
```

### Filters

All filters in a section must match (AND). List values match if any item matches (OR).

| Key | Values | Meaning |
|---|---|---|
| `repos` / `exclude_repos` | `owner/name` list | Overrides or subtracts from `defaults.repos` |
| `authors` / `exclude_authors` | usernames, `@me`, `group:<name>` | PR author |
| `review_requested` | `me` · `team` · `any` · `none` | `me` means you're requested directly. `team` means a team you're on is requested, but not you directly |
| `my_review` | `none` · `commented` · `changes_requested` · `approved` · `dismissed`, or `{not: <state>}` | Your latest *opinionated* review. A comment after an approval still counts as approved |
| `review_decision` | `approved` · `changes_requested` · `review_required` · `none` | GitHub's overall decision |
| `approvals` | `{min: N, max: N}` | Count of current approving reviews |
| `has_reviews_from_others` | `true` / `false` | Someone other than you and the author has reviewed |
| `has_reviews` | `true` / `false` | Anyone other than the author has reviewed, including you |
| `awaiting_my_review` | `true` / `false` | You haven't submitted any review yet, or your review was re-requested *directly* (pending team requests don't count) |
| `draft` | `true` / `false` | |
| `labels` / `exclude_labels` | names | Has any / has none (case-insensitive) |
| `ci` | `success` · `failure` · `pending` · `none` | Rollup status of the latest commit |
| `conflicts` | `true` / `false` | GitHub reports merge conflicts with the base branch. While GitHub is still computing, it counts as no conflicts |
| `awaiting_approval_from` | usernames | Any of these reviewers hasn't approved the PR's latest commit (e.g. `[coderabbitai]` while it re-reviews a new push). Not affected by `ignored_reviewers` |
| `updated_within_days` | int | |

Unknown keys or bad values are rejected. `prn config validate` tells you exactly where.

### Notifications

| Event | When |
|---|---|
| New in section | A PR enters a section with `notify: true` (including moving between sections). Moves caused by your own review are silent |
| Re-review requested | You'd reviewed, and the author requests your review again |
| My PR status | Your PR's review decision changes, or someone else leaves a review or comment |
| CI | Your PR's checks finish as passed or failed |

Multiple events for one PR are combined into a single notification. If more than 5 PRs change in one poll, you get one summary notification instead.

## How it works

- **Auth:** the app uses `gh auth token` (or `GITHUB_TOKEN`/`GH_TOKEN` if set). The GitHub MCP is only used while developing. The running app talks to GitHub's GraphQL API directly.
- **Fetching:** prn builds the fewest search queries that cover every section (currently 3 per poll for the default config), staying under GitHub's 256-character query limit. Filtering then happens locally. That's how it can tell team requests from direct ones, count approvals, and check "others reviewed but I haven't approved".
- **State:** files live in `~/.local/state/prn/`. `inbox.json` is the rendered inbox, `snapshot.json` is the last-seen PR state used to detect changes, and `prn.log` is the log.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
uv run pytest
```

## Troubleshooting

- **No menu bar icon:** run `prn daemon status` and `prn daemon logs`, or run `prn menubar` in a terminal to see errors directly.
- **"gh not found" from the daemon:** launchd uses a minimal PATH. prn also checks `/opt/homebrew/bin` and `/usr/local/bin`. If `gh` is installed somewhere else, set `GITHUB_TOKEN` in the plist's `EnvironmentVariables`.
- **Notifications don't appear:** in System Settings → Notifications, allow *terminal-notifier* (or *Script Editor* when using osascript).
- **Team Review Requested is empty:** your `gh` token needs `read:org`. Check with `gh auth status`, and fix with `gh auth refresh -s read:org`.

## Smoke test (first run on your Mac)

1. Run `./setup.sh`. Every step should finish with ✓, and the test notification should appear.
2. Check that the inbox icon appears in the menu bar with your badge count, and that clicking a PR opens it.
3. Run `prn list` and spot-check two or three PRs per section against GitHub.
4. Run `prn` and check that arrows move the selection, Enter opens the browser, `/` filters, and `r` refreshes.
5. Run `git status` and confirm that `config.yaml` does **not** show up.
6. Approve a PR from **Direct Review Request**, then choose **Refresh now**. It should leave the section, with no notification.
7. Ask a teammate to re-request your review (or open a test PR yourself). Within one poll you should get a clickable notification.
8. Run `prn sections add "Test" --labels urgent && prn refresh && prn sections remove Test`. Comments in your YAML should survive.
