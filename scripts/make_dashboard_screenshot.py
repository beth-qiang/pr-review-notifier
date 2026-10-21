#!/usr/bin/env python3
"""Regenerate docs/dashboard.png: the web dashboard rendered from made-up data.

Nothing here touches your real prn state: the config and state live in a temp directory, and the
dashboard is served from this process on a free port. Needs Google Chrome (set CHROME to use another
binary). From the repo root:

    uv run python scripts/make_dashboard_screenshot.py [output.png]
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
ROOT = Path(__file__).resolve().parents[1]
ME = "jordan"

CONFIG = """\
settings:
  ignored_reviewers: [coderabbitai]
  mentions: {position: 2}
defaults:
  repos: [acme/web, acme/api, acme/infra]
groups:
  teammates: [alice, bob, carol]
  bots: [dependabot, renovate]
sections:
  - name: My Approved PRs
    group: My PRs
    filters: {authors: ["@me"], review_decision: approved}
  - name: My Open PRs
    group: My PRs
    filters: {authors: ["@me"], draft: false}
  - name: My Draft PRs
    group: My PRs
    filters: {authors: ["@me"], draft: true}
  - name: Teammates Need Review
    badge: true
    filters: {authors: [group:teammates], draft: false, approvals: {max: 0}, awaiting_my_review: true, has_reviews_from_others: false}
  - name: Teammates Reviewed
    badge: true
    filters: {authors: [group:teammates], draft: false, has_reviews: true, my_review: {not: approved}}
  - name: Direct Review Request
    badge: true
    filters: {review_requested: me}
  - name: Dependency Bumps
    collapsed: true
    filters: {authors: [group:bots], review_requested: team}
  - name: Team Review Requested
    badge: true
    filters: {review_requested: team, exclude_authors: ["@me"]}
"""


def sample_prs(now: datetime) -> list:
    from prn.models import PR, Review

    def iso(mins: int) -> str:
        return (now - timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def rv(author: str, state: str, bot: bool = False) -> Review:
        return Review(author=author, state=state, submitted_at=iso(30), is_bot=bot, commit="c1")

    def pr(number, repo, title, author, ci="success", draft=False, decision="none", reviews=(), team=False):
        revs = list(reviews)
        return PR(id=f"PR_{number}", repo=f"acme/{repo}", number=number, title=title,
                  url=f"https://github.com/acme/{repo}/pull/{number}", author=author, is_draft=draft,
                  created_at=iso(560), updated_at=iso(60), review_decision=decision, ci=ci, head_oid="c1",
                  reviews=revs, latest_reviews=[r for r in revs if r.state in ("APPROVED", "CHANGES_REQUESTED")],
                  review_count=len(revs), requested_teams=["acme/core"] if team else [], review_requested_from_me=team)

    bumps = [("web", "axios", "1.6.2", "1.7.4", "dependabot"), ("web", "vite", "5.0.8", "5.4.2", "dependabot"),
             ("api", "rails", "7.1.2", "7.1.3", "dependabot"), ("api", "puma", "6.4.0", "6.4.2", "renovate"),
             ("infra", "terraform", "1.6.4", "1.9.5", "renovate")]
    return [
        pr(412, "web", "Add CSV export to the orders page", ME, decision="approved",
           reviews=[rv("alice", "APPROVED"), rv("coderabbitai", "APPROVED", True)]),
        pr(198, "api", "Retry failed webhook deliveries", ME, "failure", reviews=[rv("bob", "CHANGES_REQUESTED")]),
        pr(415, "web", "Refactor the invoice table", ME, "pending", reviews=[rv("coderabbitai", "COMMENTED", True)]),
        pr(77, "infra", "WIP: move the job queue to the new cluster", ME, "pending", draft=True),
        pr(430, "web", "Cache product lookups", "carol", reviews=[rv("coderabbitai", "APPROVED", True)]),
        pr(203, "api", "Validate the address on checkout", "bob", reviews=[rv("coderabbitai", "CHANGES_REQUESTED", True)]),
        pr(428, "web", "Update the onboarding copy", "alice"),
        pr(199, "api", "Paginate the audit log", "bob", reviews=[rv("carol", "APPROVED"), rv("coderabbitai", "APPROVED", True)]),
        pr(425, "web", "Dark mode for the settings page", "alice", reviews=[rv("dave", "COMMENTED"), rv("coderabbitai", "APPROVED", True)]),
        pr(80, "infra", "Add alerting for queue depth", "dave", team=True, reviews=[rv("erin", "APPROVED")]),
        pr(436, "web", "Handle expired sessions gracefully", "erin", team=True),
        pr(207, "api", "Document the search filters", "dave", team=True, reviews=[rv("coderabbitai", "APPROVED", True)]),
    ] + [pr(500 + i, repo, f"Bump {lib} from {old} to {new}", bot, team=True)
         for i, (repo, lib, old, new, bot) in enumerate(bumps)]


def main() -> int:
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "docs" / "dashboard.png"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "config.yaml").write_text(CONFIG, encoding="utf-8")
        # Point prn at the temp directory before anything reads its paths.
        os.environ["PRN_STATE_DIR"] = str(tmp_dir / "state")
        os.environ["PRN_CONFIG"] = str(tmp_dir / "config.yaml")
        os.environ.pop("PRN_CONFIG_DIR", None)

        from prn import config as config_mod
        from prn import store, web
        from prn.engine import evaluate
        from prn.models import Mention

        now = datetime.now(timezone.utc)
        prs = sample_prs(now)
        store.write_inbox(evaluate(prs, config_mod.load(), ME), ME, next_poll_at=time.time() + 240)

        def iso(mins: int) -> str:
            return (now - timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")

        mentions = [
            Mention(id="m1", repo="acme/web", number=431, title="Fix the timezone bug in the scheduler",
                    url="https://github.com/acme/web/pull/431", reason="mention", updated_at=iso(25)),
            Mention(id="m2", repo="acme/api", number=205, title="Add rate limiting to search",
                    url="https://github.com/acme/api/pull/205", reason="team_mention", updated_at=iso(190)),
        ]
        store.write_mentions(mentions)
        store.mark_mentions([mentions[1]])                                    # one mention already read: faded
        seen = next(p for p in prs if p.number == 425)
        store.mark_read([(seen, "Teammates Reviewed")], ME)                  # one PR already read: faded

        dash = web.Dashboard(port=0)
        dash.start()
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                 "--window-size=1240,1050", "--virtual-time-budget=5000", f"--screenshot={out}", dash.url],
                capture_output=True, text=True)
        finally:
            dash.close()
    if not out.exists():
        print(f"Chrome didn't write {out}. Is it installed at {CHROME}? Set CHROME to its path.", file=sys.stderr)
        return 1
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
