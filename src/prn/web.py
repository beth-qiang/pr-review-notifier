"""Local web dashboard. Reads inbox.json and seen.json, so the page never waits on GitHub;
only Refresh polls, and it does so through the same lock as the menu bar app."""

from __future__ import annotations

import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from . import config as config_mod
from . import fmt, poller, store
from .models import PR, Mention

log = logging.getLogger(__name__)

DEFAULT_PORT = 8787


_REVIEW_ORDER = {"approved": 0, "changes_requested": 1, "commented": 2}


def reviewers(pr: PR, me: str) -> list[dict]:
    """Everyone but the author who has reviewed: approved, changes_requested, or commented.
    Humans come first. A dismissed review shows as commented. I show up as "you"."""
    me = me.lower()
    states: dict[str, tuple[str, bool]] = {}
    for r in pr.reviews + pr.latest_reviews:   # latest_reviews last, so an opinion overrides an older comment
        if r.author == pr.author:
            continue
        state = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes_requested"}.get(r.state, "commented")
        if r in pr.latest_reviews or r.author not in states:
            states[r.author] = (state, r.is_bot)
    out = [{"login": "you" if a == me else a, "state": s, "bot": b} for a, (s, b) in states.items()]
    return sorted(out, key=lambda x: (x["bot"], _REVIEW_ORDER[x["state"]], x["login"]))


def status(pr: PR) -> str:
    """approved / draft / open, in the order the starter sections claim PRs."""
    if pr.review_decision == "approved":
        return "approved"
    return "draft" if pr.is_draft else "open"


def mention_section(mentions: list[Mention], seen: Optional[dict[str, dict]]) -> dict:
    return {
        "name": "Mentions", "kind": "mentions", "badge": True, "grouped": False, "collapsed": False,
        "prs": [
            {
                "key": store.mention_key(m.id),
                "label": f"{m.short_repo}#{m.number}",
                "repo": m.repo,
                "title": m.title,
                "url": m.url,
                "reason": m.reason,
                "updated_at": m.updated_at,
                "unread": seen is None or store.is_mention_unread(seen, m),
                "done": seen is not None and store.is_mention_done(seen, m),
            }
            for m in mentions
        ],
    }


def payload(inbox: Optional[store.Inbox], seen: Optional[dict[str, dict]],
            groups: Optional[dict[str, str]] = None, collapsed: Optional[set[str]] = None,
            mentions: Optional[list[Mention]] = None, mentions_position: int = 1) -> dict:
    """The JSON the page renders. `seen` is None when read tracking is off (everything counts as unread).
    `groups` maps a section name to a group name; sections sharing a group become one table, in the
    position of the first, and each of its rows shows a status. `collapsed` is the set of section names
    that start collapsed; a group starts collapsed if its first section does. `mentions` (None: feature off)
    becomes a "Mentions" section at `mentions_position` (1 = first)."""
    if inbox is None:
        return {"empty": True, "sections": []}
    groups = groups or {}
    collapsed = collapsed or set()
    me = inbox.me.lower()
    sections: list[dict] = []
    by_group: dict[str, dict] = {}
    for s in inbox.sections:
        rows = [
            {
                "key": p.key,
                "label": fmt.pr_label(p),
                "title": p.title,
                "url": p.url,
                "author": p.author,
                "draft": p.is_draft,
                "status": status(p),
                "mine": p.author == me,
                "ci": p.ci if p.author == me else "none",   # CI only matters on my own PRs
                "reviewers": reviewers(p, inbox.me),
                "unread": seen is None or store.is_unread(seen, p, s.name, inbox.me),
                "done": seen is not None and store.is_done(seen, p, s.name, inbox.me),
            }
            for p in s.prs
        ]
        group = groups.get(s.name)
        if group is None:
            sections.append({"name": s.name, "badge": s.badge, "grouped": False,
                             "collapsed": s.name in collapsed, "prs": rows})
        elif group in by_group:
            by_group[group]["prs"].extend(rows)
            by_group[group]["badge"] = by_group[group]["badge"] or s.badge
        else:
            by_group[group] = {"name": group, "badge": s.badge, "grouped": True,
                               "collapsed": s.name in collapsed, "prs": rows}
            sections.append(by_group[group])
    if mentions is not None:
        sections.insert(min(mentions_position - 1, len(sections)), mention_section(mentions, seen))
    return {
        "empty": False,
        "me": inbox.me,
        "generated_at": inbox.generated_at,
        "next_poll_at": inbox.next_poll_at,
        "error": inbox.error,
        "badge_count": inbox.badge_count,
        "tracking": seen is not None,   # read/done only exist when read_tracking is on
        "sections": sections,
    }


def current_payload() -> dict:
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError:   # keep showing the inbox; just no grouping
        cfg = None
    seen = store.read_seen() if cfg is None or cfg.settings.read_tracking else None
    groups = {s.name: s.group for s in cfg.sections if s.group} if cfg else {}
    collapsed = {s.name for s in cfg.sections if s.collapsed} if cfg else set()
    ms = cfg.settings.mentions if cfg else config_mod.MentionSettings()
    mentions = (store.read_mentions() or []) if ms.enabled else None
    return payload(store.read_inbox(), seen, groups, collapsed, mentions, ms.position)


def _items_for(inbox: store.Inbox, keys: set[str]) -> list[tuple]:
    return [(p, s.name) for s in inbox.sections for p in s.prs if p.key in keys]


def _split_keys(keys: list[str]) -> tuple[set[str], list[str]]:
    """Mention rows use `mention:<thread id>` keys; everything else is a PR key."""
    prefix = store.mention_key("")
    return ({k[len(prefix):] for k in keys if k.startswith(prefix)}, [k for k in keys if not k.startswith(prefix)])


def _mark(keys: list[str], done: bool) -> None:
    mention_ids, keys = _split_keys(keys)
    if mention_ids:
        store.mark_mentions([m for m in store.read_mentions() or [] if m.id in mention_ids], done=done)
    inbox = store.read_inbox()
    if inbox is None or not keys:
        return
    live = {p.key for s in inbox.sections for p in s.prs}
    store.mark_read(_items_for(inbox, set(keys)), inbox.me, live_keys=live, done=done)


def mark_read(keys: list[str]) -> None:
    _mark(keys, done=False)


def mark_done(keys: list[str]) -> None:
    _mark(keys, done=True)


def mark_unread(keys: list[str]) -> None:
    store.mark_unread(keys)


def refresh() -> Optional[str]:
    """Poll GitHub now. Returns an error message, or None on success."""
    try:
        poller.poll_once(lock_wait_seconds=30)
    except Exception as exc:  # the page shows whatever went wrong
        log.warning("web refresh failed: %s", exc)
        return str(exc)
    return None


def make_handler(token: str, port_getter: Callable[[], int],
                 do_refresh: Callable[[], Optional[str]] = refresh) -> type[BaseHTTPRequestHandler]:
    page = resources.files("prn").joinpath("assets/dashboard.html").read_text(encoding="utf-8")
    actions = {"/read": mark_read, "/unread": mark_unread, "/done": mark_done}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the daemon log quiet
            pass

        def _send(self, body: str, ctype: str = "application/json", code: int = 200) -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self) -> bool:
            # The Host check stops DNS-rebinding pages from reading the token; the token stops other
            # sites from POSTing to localhost.
            port = port_getter()
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _authed(self) -> bool:
            return self._allowed() and parse_qs(urlparse(self.path).query).get("token") == [token]

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/" and self._allowed():
                self._send(page.replace("__TOKEN__", token), "text/html")
            elif path == "/data" and self._authed():
                self._send(json.dumps(current_payload()))
            else:
                self._send("{}", code=403)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if not self._authed():
                self._send("{}", code=403)
            elif path == "/refresh":
                self._send(json.dumps({"error": do_refresh()}))
            elif path in actions:
                try:
                    keys = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"[]")
                except json.JSONDecodeError:
                    return self._send("{}", code=400)
                if not isinstance(keys, list):
                    return self._send("{}", code=400)
                actions[path]([str(k) for k in keys])
                self._send("{}")
            else:
                self._send("{}", code=404)

    return Handler


class Dashboard:
    """The dashboard server. `start()` runs it on a background thread (the menu bar app) and
    `serve_forever()` blocks (`prn web`)."""

    def __init__(self, port: int = DEFAULT_PORT, do_refresh: Callable[[], Optional[str]] = refresh):
        self.token = secrets.token_urlsafe(16)
        self._server = ThreadingHTTPServer(
            ("127.0.0.1", port), make_handler(self.token, lambda: self.port, do_refresh))
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="prn-web")
        self._thread.start()

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
