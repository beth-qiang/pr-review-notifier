# PR Review Notifier (`prn`)

A local GitHub PR inbox for macOS. The command is `prn`, short for **PR Review Notifier**; `pr-review-notifier` works too.

- **Menu bar app:** an inbox icon with a badge count. Its dropdown lists your PRs grouped into sections, and clicking one opens it on GitHub.
- **Desktop notifications:** you get one when a PR needs you, when your review is re-requested, or when your own PR is approved, reviewed, or finishes CI.
- **Interactive terminal inbox:** run `prn` to browse, filter, and open PRs from the keyboard.
- **Configurable sections:** each section is a named set of filters (repos, authors, review state, CI, and more) in a YAML file you can edit by hand.

It's modeled on Graphite's inbox, and everything runs locally on your Mac.

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Upgrading](#upgrading)
- [Uninstall](#uninstall)
- [Using it](#using-it)
- [Read and unread](#read-and-unread)
- [The starter sections](#the-starter-sections)
- [Config reference](#config-reference)
- [Notifications](#notifications)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Requirements

- **macOS:** version 13 (Ventura) or later is recommended. Login Items only shows the app's name and icon on 13+.
- **Xcode Command Line Tools:** Homebrew installs these. They provide `otool` and `install_name_tool`, which are used to build the app. If you don't have them, run `xcode-select --install`.
- **A GitHub account with access to the repos you want to watch.** For the **Team Review Requested** section, you also need to be a member of the GitHub teams that get requested.

`setup.sh` installs everything else for you, and asks first: Homebrew, [uv](https://docs.astral.sh/uv/), the GitHub CLI (`gh`), and `terminal-notifier`.

## Install

```bash
git clone https://github.com/sepiette/pr-review-notifier.git
cd pr-review-notifier
./setup.sh
```

`setup.sh` is interactive. It explains each step, asks before installing anything, and is safe to re-run. In order, it:

1. **Checks dependencies.** It looks for Homebrew, uv, `gh`, and `terminal-notifier`, and offers to install any that are missing. All four are required.
2. **Checks GitHub auth.** If `gh` isn't logged in, it runs `gh auth login`. It also makes sure the token has the `read:org` scope, which team review requests need. prn reuses the `gh` token and never stores a token of its own.
3. **Installs the command.** It installs `prn` (and `pr-review-notifier`) with `uv tool install --editable`, so prn runs straight from this folder. If uv's bin directory (`~/.local/bin`) isn't on your PATH, it offers to add it.
4. **Runs the setup wizard** (`prn setup`), which creates `./config.yaml` from `config.example.yaml`. It asks for:
   - **Repos to watch**, as `owner/name` separated by commas. It checks that you can access each one.
   - **Teammates.** It shows the people whose PRs you've reviewed most in those repos over the last 90 days. Pick them by number, type usernames, or both.
   - **Which of the 7 starter sections to keep.** See [The starter sections](#the-starter-sections).
   - **Polling strategy** (conservative or aggressive, see [Polling strategies](#polling-strategies)), the **full poll interval** (default 300 seconds), and which notification types you want.
5. **Fetches your PRs.** The first poll records the current state silently, so you don't get a burst of notifications about PRs that already exist.
6. **Builds the PR Review Notifier app** with `prn app install`, so notifications say "PR Review Notifier" instead of "Python". See [Why there's an app](#why-theres-a-pr-review-notifierapp).
7. **Sends a test notification.** macOS may ask you to allow notifications from PR Review Notifier. Click **Allow**.
8. **Starts the menu bar app** and adds it as a login item, so it launches at login.
9. **Runs a health check** (`prn doctor`). Every line should show ✓.

When it finishes, the inbox icon appears in your menu bar.

### Re-running setup

If `./config.yaml` already exists, setup asks what to do with it:

| Choice | What happens |
|---|---|
| **k**eep | Leave your config alone. Reinstall prn, rebuild the app, and restart the menu bar app |
| **r**econfigure | Re-run the wizard with your current repos, teammates, and settings as the defaults |
| **f**resh | Start over from `config.example.yaml` |

Before it overwrites anything, it backs up the old file to `config.yaml.bak-<timestamp>`.

### Your config stays private

`config.yaml` holds your repos and teammates, and it's gitignored. Only the generic `config.example.yaml` is committed. Setup records where your config lives in `~/.config/prn/config-path`, so the installed command and the menu bar app always find it. To use a different file, run `prn config use <path>`.

## Upgrading

```bash
git pull
```

prn is installed in editable mode, so pulling is usually all you need. The menu bar app notices the code change, checks that the new code imports, and restarts itself within a few seconds. If the new code fails to import, it keeps running the previous version and sends you a notification.

**Re-run `./setup.sh` and choose keep when:**

- `pyproject.toml` dependencies changed in the pull.
- You installed prn before the PR Review Notifier app existed, and notifications or Login Items still say "Python". This also switches older installs to editable mode.
- You upgraded Python through uv. You can also just run `prn app install`.

## Uninstall

```bash
./uninstall.sh
```

This stops the menu bar app and removes the login item, the PR Review Notifier app, and the `prn` command. It then asks whether to delete:

- **`config.yaml` and its backups.** Default: keep.
- **Cached state and logs** in `~/.local/state/prn`. Default: delete.
- **`terminal-notifier`.** Default: keep.

It never removes `gh`, `uv`, or Homebrew.

## Using it

### The menu bar

The icon shows a number: the count of PRs in sections that have `badge: true`. The default config counts the four review sections. When the last poll failed, a ⚠ appears next to it.

The dropdown contains:

```
PR Review Notifier
───────────────────────────────
Teammates Need Review (2)
   ✓   app-workflow#123  Add order export…   · teammate-one
   ● ◆ infra-gitops#45   Bump chart version… · teammate-two
───────────────────────────────
My Draft PRs (1)
   ✗   api-llm#9  WIP: retry logic  (draft)  · you
───────────────────────────────
✓ ✗ ● CI   ◆ approved / changes requested
───────────────────────────────
Open dashboard             ⌘D
Refresh now                ⌘R
Open inbox (Terminal)
Edit config…
Last updated 10:42 AM · next in 4m
───────────────────────────────
Quit PR Review Notifier    ⌘Q
```

- **CI status:** green ✓ passed, red ✗ failed, orange ● running. A blank means no checks.
- **Review status:** green ◆ approved, red ◆ changes requested.
- **Drafts:** shown dimmed and labelled "(draft)".
- **Read PRs:** muted until there's new activity. See [Read and unread](#read-and-unread).
- **Click a PR** to open it on GitHub and mark it read.
- **Refresh now** runs a full poll immediately. **Open inbox (Terminal)** opens the interactive inbox in Terminal. **Edit config…** opens `config.yaml` in your default text editor.
- **The status line** shows when the inbox was last updated and when the next poll is. With `poll_strategy: aggressive` it reads `Updated 10:42 AM · live, checked 6s ago` instead.

Empty sections are hidden (`hide_empty_sections`). Sections with `display: false` are never shown.

**Keep the dropdown short.** Set `menu: count` on a section to show one `Name (N)` line instead of every PR (clicking it opens the dashboard), or `menu: hidden` to leave it out of the dropdown. The default is `menu: rows`. This only affects the dropdown: badges, notifications, the terminal inbox and the dashboard still show everything.

### The web dashboard

**Open dashboard** in the menu bar (or `prn web`) opens `http://127.0.0.1:8787/`. It reads the same inbox the menu bar app writes, so it loads instantly; only **Refresh** polls GitHub.

- Sections are collapsible, and the collapsed state is remembered.
- Each row shows the repo and number, the title, the author, and a chip for every reviewer: green ✓ approved, red ✗ requested changes, outlined for a comment. Bots are dimmed, and a bot's approval is slate blue instead of green. Your own review shows as "you".
- CI status is shown only on your own PRs.
- **Read / Unread** and **Mark all read** use the same read state as the menu bar and terminal inbox. Clicking a title marks it read.
- A section with `collapsed: true` starts collapsed. Once you expand or collapse a section yourself, the dashboard remembers your choice instead.
- Sections that share a `group:` are shown as one table with a status column (approved, open, draft). For example, put `group: My PRs` on the three "My ..." sections.

The server listens on localhost only and needs a per-run token, so other web pages can't read or change anything. If port 8787 is taken, the menu bar app still runs and "Open dashboard" opens whatever is on that port.

### Mentions

Unread @mentions on pull requests come from GitHub's notifications feed, so they cover **every repo you're subscribed to**, not just `defaults.repos`. Issues are ignored.

- **Where they show:** a **Mentions** section on the dashboard (at `mentions.position`), a block at the top of the menu bar dropdown that counts toward the badge, and `prn list` / `prn list --json`.
- **What a row has:** the repo and PR number, the title, whether it was you or your team, and how long ago. The notifications feed has no author, CI or reviewer data, so rows have none.
- **GitHub decides what's unread.** Opening the PR on GitHub marks the thread read there, and the mention disappears from prn within a poll.
- **Read and Done** work like they do for PRs, and are local to prn. A mention you marked done comes back if someone mentions you again.
- **Cost:** one request per poll (at most once a minute, as GitHub asks), sent with `If-Modified-Since`, so an unchanged feed is a 304 and costs no rate limit.
- **The first fetch is silent.** After that, a new mention sends a notification.
- **Turning it off:** `mentions.enabled: false` removes it entirely. `mentions.team_mentions: false` keeps only personal mentions. `notifications.mention: false` keeps the section but sends no notification.

### The terminal inbox

Run `prn` (or `pr-review-notifier`). Sections and PRs are on the left, with PRs you've already read muted. The selected PR's details are on the right: reviews, requested reviewers, approvals, CI, labels, and your own review state. The view updates by itself whenever the menu bar app polls.

| Key | Action |
|---|---|
| `↑` / `↓` | Move |
| `Enter` or `o` | Open the PR on GitHub |
| `y` | Copy the PR URL |
| `m` | Toggle the selected PR between read and unread |
| `M` | Mark every PR shown as read |
| `r` | Refresh now |
| `/` | Filter by title, repo, number, author, or label (`Esc` clears it) |
| `q` | Quit |

### Read and unread

Once you've looked at a PR, it fades so new activity stands out. This works in both the TUI and the menu bar dropdown.

- **A PR becomes read** when you open it from the TUI (`Enter` or `o`) or click it in the menu bar. In the TUI, `m` toggles the selected PR between read and unread, and `M` marks everything shown as read. The status bar counts the PRs that are new.
- **A read PR is muted.** Its row keeps the CI and review colours, but everything is drawn fainter.
- **It comes back to full brightness only when something new happens on it.** That means one of these:
  - someone else reviews or comments
  - new commits on someone else's PR
  - CI passes or fails on your PR
  - your review is requested directly
  - the PR moves to a different section
- **Your own activity doesn't count.** Your comments and reviews, your pushes to your own PRs, and a review request disappearing because you reviewed all leave it dimmed.
- **Every PR starts unread.** Press `M` once to start from a clean slate.
- **Clicking a desktop notification doesn't mark the PR read**, because macOS doesn't report the click to prn. Use `m` instead.

**Don't want it?** Set `read_tracking: false` under `settings`. Every row then stays at full brightness, opening a PR doesn't mark it read, and `m`/`M` and the "new" count go away. Both views pick up the change within a few seconds. Turning it back on keeps your earlier read state.

Read state lives in `~/.local/state/prn/seen.json`. Entries are cleared at the end of the week: when Monday starts, any entry from an earlier week is removed if its PR is no longer in the inbox (merged, closed, or no longer matching a section). A PR still in the inbox keeps its read or done state however old it is. If a cleared PR comes back, it shows as unread.

### Commands

| Command | What it does |
|---|---|
| `prn` | Interactive inbox |
| `prn list [-s NAME] [--json] [-r] [-a]` | Print the inbox: one section only (`-s`), as JSON, refresh first (`-r`), include empty sections (`-a`) |
| `prn refresh` | Poll GitHub now. Notifications go out for anything new |
| `prn sections list` | Sections in evaluation order, with their filters |
| `prn sections add NAME [flags]` / `-i` | Add a section with flags, or answer prompts (`-i`). See [Adding sections](#adding-sections-from-the-command-line) |
| `prn sections remove NAME` | Remove a section |
| `prn sections move NAME --to N` | Move a section to position N. Order matters: first match wins |
| `prn setup` | Run the configuration wizard on its own |
| `prn config edit` | Open the config in `$EDITOR`, then validate it |
| `prn config validate` | Check the config and show exactly what's wrong |
| `prn config path` / `use PATH` | Show the active config file, or switch to another one |
| `prn config init [--path P]` | Copy `config.example.yaml` into place. Setup normally does this |
| `prn app install` / `uninstall` / `status` | Build, remove, or inspect the PR Review Notifier app |
| `prn test-notification` | Send a test notification |
| `prn daemon install` / `uninstall` / `restart` / `status` | Manage the login item that runs the menu bar app |
| `prn daemon logs [-n N] [-f]` | Show (or follow) the log |
| `prn web [--port N] [--no-open]` | Serve the [web dashboard](#the-web-dashboard) on localhost (the menu bar app already does this) |
| `prn menubar` | Run the menu bar app in the foreground, which is useful for debugging |
| `prn doctor` | Health check: gh, auth, terminal-notifier, config, app, login item, last poll |

## The starter sections

Every PR is checked against the sections from top to bottom, and it appears only in the **first** section it matches. So order matters. Use `prn sections move` or edit the YAML to change it.

| # | Section | What it catches | Notify | Badge |
|---|---|---|---|---|
| 1 | **Teammates Need Review** | Teammates' non-draft PRs with no approvals that are waiting on you: you haven't reviewed yet, or the author re-requested your review | ✓ | ✓ |
| 2 | **Teammates Reviewed Not Approved by Me** | Teammates' non-draft PRs that someone (you included) has reviewed, but that you haven't approved | ✓ | ✓ |
| 3 | **Team Review Requested** | PRs where a team you're on was requested, but you weren't requested directly. Your own PRs are excluded | ✓ | ✓ |
| 4 | **Direct Review Request** | PRs where you were requested directly as a reviewer | ✓ | ✓ |
| 5 | **My Approved PRs** | Your PRs that GitHub considers approved | ✓ | |
| 6 | **My Open PRs** | Your other non-draft PRs | | |
| 7 | **My Draft PRs** | Your drafts | | |

How a teammate's PR moves through the sections:

- When it opens, it lands in **Teammates Need Review**.
- Once you leave any review (a comment, changes requested, or a reply in a review thread), it moves to **Teammates Reviewed Not Approved by Me**. Moves caused by your own review don't send a notification.
- It goes back to **Teammates Need Review** only if the author re-requests your review directly, and only while it still has no approvals. If someone else has already approved it, it stays in Reviewed Not Approved by Me.
- Once you approve it, it leaves both teammate sections.

## Config reference

Your config is `./config.yaml`; run `prn config path` to check. **When you save it, the menu bar app validates it and re-polls within about 3 seconds**, so you don't need to restart anything. If the file is invalid, you get one notification describing the error, the menu bar shows ⚠, and the last good inbox stays visible until you fix it. `config.example.yaml` has every option with comments.

```yaml
settings:
  poll_strategy: conservative      # conservative | aggressive (see "Polling strategies" below)
  poll_interval_seconds: 300       # how often to run a full poll (minimum 60)
  fast_poll_interval_seconds: 15   # aggressive only: how often to check for changes (minimum 10)
  open_with: github                # where PR links open
  notifier: terminal-notifier      # terminal-notifier (branded + clickable) | osascript
  hide_empty_sections: true        # in the menu bar dropdown
  max_title_length: 50             # PR title truncation in the dropdown
  read_tracking: true              # fade PRs you've opened until there's new activity; false turns it off
  ignore_bot_reviews: false        # true: bot reviews don't count for has_reviews / has_reviews_from_others
  ignored_reviewers: [coderabbitai]  # these reviewers never count toward approvals or "has reviews"
  mentions:                        # unread @mentions on PRs (see "Mentions")
    enabled: true
    team_mentions: true            # false: only count @you, not @your-team
    position: 1                    # where the web dashboard puts the Mentions section (1 = first)
  notifications:                   # turn each notification type on or off
    new_in_section: true
    re_review_requested: true
    mention: true
    my_pr_status: true
    ci_status: true

defaults:
  repos: [your-org/repo-one, your-org/repo-two]   # used by every section without its own `repos:`

groups:
  teammates: [teammate-one, teammate-two]          # use as `group:teammates` in authors / exclude_authors

sections:                          # checked top to bottom; first match wins
  - name: Teammates Need Review
    notify: true                   # notify when a PR newly enters this section (default true)
    badge: true                    # count toward the menu bar number (default false)
    display: true                  # false: still claims matching PRs, but hides the section everywhere
                                   #        and never sends "New in section" notifications
    menu: rows                     # menu bar dropdown: rows (every PR) | count (one line) | hidden
    group: My PRs                  # web dashboard: sections with the same group are one table (optional)
    collapsed: true                # web dashboard: start this section collapsed (optional; your own clicks win)
    filters:
      authors: [group:teammates]
      draft: false
      approvals: { max: 0 }
      awaiting_my_review: true
```

`ignored_reviewers` accepts logins with or without the `[bot]` suffix. `coderabbitai` and `coderabbitai[bot]` both work.

**Dependency bumps** make a good quiet section: define a group of bot logins, then put a section for them just before your catch-all review section.

```yaml
groups:
  bots: [dependabot, renovate]

sections:
  # ...
  - name: Dependency Bumps
    notify: false
    badge: false
    menu: hidden                   # keep it out of the menu bar dropdown
    collapsed: true                # start collapsed on the dashboard
    filters:
      authors: [group:bots]
      review_requested: team       # reuses the review-requested search, so no extra GitHub queries
  - name: Team Review Requested
    # ...
```

A section with `display: false` is useful for removing PRs you don't care about from later sections. For example, a hidden "Has conflicts" section near the top keeps conflicted PRs out of your review sections.

### Polling strategies

`poll_strategy` decides how the menu bar app keeps the inbox current. **Refresh now**, `r` in the terminal inbox, `prn refresh`, and saving `config.yaml` always run a full poll.

| | `conservative` (default) | `aggressive` |
|---|---|---|
| What runs | A full poll every `poll_interval_seconds` | A change check every `fast_poll_interval_seconds`, plus a full poll every `poll_interval_seconds` |
| Typical delay before a change shows up | About half of `poll_interval_seconds` | About 10–20 seconds |
| GitHub API use at the default intervals | About 110 points an hour | About 400 points an hour |

GitHub allows 5,000 GraphQL points an hour. prn uses your `gh` login, so it shares that budget with the `gh` CLI and anything else using the same login.

How aggressive mode works:

- **Change check.** All of prn's searches go in one request that asks only for each PR's last-updated time, latest commit, and CI status. It costs 1 point and takes about 3 seconds.
- **Refetch.** When a PR's values differ from the last check, or a PR appears, disappears, or starts or stops requesting your review, prn fetches full details for just those PRs. Then it updates the inbox and sends notifications as usual.
- **Full poll.** GitHub doesn't update a PR when merge conflicts appear, so conflicts can take up to `poll_interval_seconds` to show up. The full poll catches those.
- **Fallbacks.** If a search returns more than 100 PRs, or fewer than 500 of your hourly points are left, prn skips change checks until the next full poll.
- **Brand-new PRs and review requests** reach GitHub's search a few seconds after they happen, so they can take slightly longer to appear than changes to PRs already in your inbox.

### Filters

All filters in a section must match (AND). Inside a list, any one item can match (OR).

| Key | Values | Meaning |
|---|---|---|
| `repos` / `exclude_repos` | `owner/name` list | Replaces, or removes repos from, `defaults.repos` for this section |
| `authors` / `exclude_authors` | usernames, `@me`, `group:<name>` | Who opened the PR |
| `review_requested` | `me` · `team` · `any` · `none` | `me`: you're requested directly. `team`: a team you're on is requested, but not you directly |
| `my_review` | `none` · `commented` · `changes_requested` · `approved` · `dismissed`, or `{not: <state>}` | Your latest review that approved or requested changes. A comment after an approval still counts as approved |
| `review_decision` | `approved` · `changes_requested` · `review_required` · `none` | GitHub's overall review decision |
| `approvals` | `{min: N, max: N}` | Number of current approvals, not counting `ignored_reviewers` |
| `has_reviews_from_others` | `true` / `false` | Someone other than you and the author has reviewed |
| `has_reviews` | `true` / `false` | Anyone other than the author has reviewed, including you |
| `awaiting_my_review` | `true` / `false` | You haven't reviewed yet, or your review was re-requested **directly**. Pending team requests don't count |
| `draft` | `true` / `false` | |
| `labels` / `exclude_labels` | names | Has any of these labels / has none of them (case-insensitive) |
| `ci` | `success` · `failure` · `pending` · `none` | Combined check status of the latest commit |
| `conflicts` | `true` / `false` | GitHub reports merge conflicts with the base branch. While GitHub is still computing, this counts as no conflicts |
| `ready_to_merge` | `true` / `false` | GitHub's merge state is clean: branch protection is satisfied (required reviews and required checks such as policy-bot), every other check passed, and there are no conflicts. While GitHub is still computing, this counts as not ready |
| `awaiting_approval_from` | usernames | Any of these reviewers hasn't approved the PR's **latest commit**, for example `[coderabbitai]` while it re-reviews a new push. Not affected by `ignored_reviewers` |
| `approved_by` | usernames | Any of these reviewers has approved the PR's **latest commit**. An approval on an older commit doesn't count. Not affected by `ignored_reviewers` |
| `updated_within_days` | number | Updated within the last N days |

Unknown keys and bad values are rejected with the exact location of the problem. If an unknown key is a newer setting, the error message tells you to reinstall.

### Adding sections from the command line

`prn sections add` writes to `config.yaml` and keeps your comments. Each flag maps to a filter:

| Flag | Filter |
|---|---|
| `--authors a,b` / `--exclude-authors a,b` | `authors` / `exclude_authors` |
| `--repos o/r,o/r` / `--exclude-repos …` | `repos` / `exclude_repos` |
| `--review-requested me\|team\|any\|none` | `review_requested` |
| `--my-review approved` or `--my-review not:approved` | `my_review` |
| `--review-decision approved` | `review_decision` |
| `--min-approvals N` / `--max-approvals N` | `approvals` |
| `--has-reviews` / `--no-reviews` | `has_reviews` |
| `--has-reviews-from-others` / `--no-reviews-from-others` | `has_reviews_from_others` |
| `--awaiting-my-review` / `--not-awaiting-my-review` | `awaiting_my_review` |
| `--draft` / `--not-draft` | `draft` |
| `--labels a,b` / `--exclude-labels a,b` | `labels` / `exclude_labels` |
| `--ci failure` | `ci` |
| `--updated-within-days N` | `updated_within_days` |
| `--notify/--no-notify`, `--badge/--no-badge`, `--position N` | section options |

There are no flags yet for `conflicts`, `ready_to_merge`, `awaiting_approval_from`, `approved_by`, or `display`; set those in the YAML.

Example:

```bash
prn sections add "Failing CI on my PRs" --authors @me --ci failure --badge --position 5
```

## Notifications

Notifications come from **PR Review Notifier**, with its own icon. Clicking one opens the PR.

| Event | When it fires |
|---|---|
| New in section | A PR enters a section that has `notify: true`, including when it moves between sections. Silent when the move was caused by your own review, or when the section has `display: false` |
| Re-review requested | You'd already reviewed, and you've been requested again |
| Mention | Someone @mentions you (or a team you're on) on a pull request, or mentions you again on one you'd already seen. See [Mentions](#mentions) |
| My PR status | Your PR's review decision changes, or someone else leaves a review or comment. Your own replies don't count |
| CI | Checks on your PR finish as passed or failed |

- **One notification per PR:** if several events hit the same PR in one poll, they're combined.
- **Busy polls:** if more than 5 PRs change at once, you get a single summary instead.
- **First run:** setup's first poll is silent, so PRs that were already open don't flood you.
- **App alerts:** you also get a notification when you save an invalid `config.yaml`, or when a code update fails to load.
- **Turning things off:** each event type has its own setting under `settings.notifications`, and each section has its own `notify` option.

To check notifications are working, run `prn test-notification`. If nothing appears, allow **PR Review Notifier** under System Settings → Notifications.

### Why there's a "PR Review Notifier.app"

macOS labels notifications, Login Items, and Activity Monitor entries with the app a process runs from. A plain Python process shows up as **Python**. To show **PR Review Notifier** instead, `prn app install` (run by setup) builds `~/Library/Application Support/PR Review Notifier/PR Review Notifier.app`. It contains:

- **The menu bar app.** The login item runs it from inside the app, using a copy of prn's Python interpreter. If that copy can't start (for example after a Python upgrade), the login item falls back to plain `prn menubar` until you run `prn app install` again.
- **A branded notifier.** A renamed copy of `terminal-notifier` that sends every notification.

## How it works

- **Auth:** prn uses `gh auth token`, or `GITHUB_TOKEN` / `GH_TOKEN` if one is set, and talks to GitHub's GraphQL API directly.
- **Fetching:** each poll runs the smallest set of GitHub searches that covers every section (3 for the default config), keeping each under GitHub's 256-character limit. Filtering then happens locally. That's how prn can tell team requests from direct ones, count approvals, ignore reviewers, and check "reviewed but not approved by me".
- **Polling and live updates:** the menu bar app polls on a timer and writes the results to `inbox.json`. The terminal inbox and the dropdown redraw whenever that file changes. Saving `config.yaml` triggers a full poll, and code changes trigger a restart. The searches in a poll run in parallel.
- **Files:**

  | Path | What |
  |---|---|
  | `./config.yaml` | Your config (gitignored) |
  | `~/.config/prn/config-path` | Points the installed command at your config |
  | `~/.local/state/prn/inbox.json` | The rendered inbox |
  | `~/.local/state/prn/snapshot.json` | Last-seen PR state, used to detect changes |
| `~/.local/state/prn/seen.json` | Read/unread state: what each PR looked like when you last opened it |
  | `~/.local/state/prn/prn.log` | App log (`prn daemon logs`) |
  | `~/.local/state/prn/menubar.out.log` | Output from the menu bar process (startup errors land here) |
  | `~/Library/LaunchAgents/com.prn.menubar.plist` | The login item |
  | `~/Library/Application Support/PR Review Notifier/` | The PR Review Notifier app |

## Troubleshooting

Start with `prn doctor`. It checks everything below and tells you what to run.

- **No menu bar icon:** run `prn daemon status` and `prn daemon logs`, and check `~/.local/state/prn/menubar.out.log`. Running `prn menubar` in a terminal shows errors directly.
- **⚠ in the menu bar:** the last poll failed. The line under "PR Review Notifier" at the top of the dropdown says why. Common causes are an invalid config (`prn config validate`) and expired GitHub auth (`gh auth login`).
- **Notifications don't appear:** allow **PR Review Notifier** under System Settings → Notifications, then run `prn test-notification`. If `prn doctor` says the app isn't built, run `prn app install`.
- **Notifications or Login Items still say "Python":** the app isn't being used. Run `prn app install`; it also points the login item at the app.
- **`prn app install` fails:** make sure the Command Line Tools are installed (`xcode-select --install`) and that `terminal-notifier` is installed (`brew install terminal-notifier`). Then run it again. Until it succeeds, notifications still work through plain terminal-notifier.
- **"Unknown key" config error after `git pull`:** your config uses a newer setting than the installed code supports. Re-run `./setup.sh` and choose keep.
- **"gh not found" from the menu bar app:** the login item uses a minimal PATH that includes `~/.local/bin`, `/opt/homebrew/bin`, and `/usr/local/bin`. If `gh` is installed somewhere else, set `GITHUB_TOKEN` in the login item's `EnvironmentVariables` (`~/Library/LaunchAgents/com.prn.menubar.plist`).
- **Team Review Requested is empty:** your `gh` token needs `read:org`. Check with `gh auth status`, and fix it with `gh auth refresh -s read:org`.
- **A PR is in the "wrong" section:** remember that the first matching section wins. Run `prn sections list` to see the order and filters.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
uv run pytest
```

- **Tests:** they don't need GitHub or macOS. GitHub responses and macOS tools are faked.
- **Running from your checkout:** setup's editable install runs your local changes directly, and the menu bar app restarts itself when you save a file under `src/`.
- **Pull requests:** open them from a fork against `main`, and leave **Allow edits from maintainers** ticked so conflicts can be resolved on your branch. Don't commit `config.yaml` or anything with personal usernames or repos; tests use `alice`, `bob`, and `acme/*`.

### Smoke test on a Mac

1. Run `./setup.sh`. Every step should show ✓, and the test notification should say "PR Review Notifier".
2. Check that the inbox icon shows your badge count, and that clicking a PR opens it.
3. Run `prn list` and spot-check two or three PRs per section against GitHub.
4. Run `prn` and check that the arrow keys, `Enter`, `m`, `/`, and `r` work. Opened PRs should fade.
5. Run `git status` and confirm `config.yaml` doesn't appear.
6. Review a teammate's PR without approving it, then choose **Refresh now**. It should move to Reviewed Not Approved by Me, with no notification.
7. Have a teammate re-request your review. Within one poll, you should get a clickable notification, and the PR should move back to Teammates Need Review.
8. Change `poll_interval_seconds` in `config.yaml` and save. Within about 3 seconds the dropdown should show "Config changed, refreshing…" or a new "Last updated" time.
