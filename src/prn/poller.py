"""Polling: fetch → evaluate → diff → notify → persist.

`poll_once` is a single full poll (used by `prn refresh` and the TUI). `Poller` drives the menu bar
app's loop and implements both `settings.poll_strategy` values.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import config as config_mod
from . import events as events_mod
from . import notify, store
from .config import Config
from .engine import SectionResult, evaluate
from .github import (AuthError, Fingerprint, Fetched, GitHubClient, allowed_repos, build_prs, check_all,
                     fetch_all, parse_mentions, plan_queries)

log = logging.getLogger(__name__)

# Below this many GraphQL points left in the hour, aggressive mode pauses change checks until the next full poll.
LOW_RATE_REMAINING = 500


@dataclass
class PollResult:
    inbox: Optional[store.Inbox]   # None when a change check found nothing new (inbox.json left as is)
    notifications: list[events_mod.Notification] = field(default_factory=list)
    seeded: bool = False
    duration_s: float = 0.0
    kind: str = "full"             # full | check
    changed: int = 0               # check only: PRs refetched or dropped
    points: int = 0                # GraphQL points this poll cost


_me_cache: dict[str, str] = {}


def _viewer(client: GitHubClient) -> str:
    me = _me_cache.get("login") or client.viewer_login()
    _me_cache["login"] = me
    return me


def _publish(cfg: Config, me: str, prs, send_notifications: bool,
             next_poll_at: float) -> tuple[store.Inbox, list[events_mod.Notification], bool]:
    results: list[SectionResult] = evaluate(prs, cfg, me)
    prev = store.read_snapshot()
    seeded = prev is None
    notifications: list[events_mod.Notification] = []
    if not seeded:
        evs = events_mod.diff(prev, results, prs, cfg, me)
        notifications = events_mod.to_notifications(evs)
        if send_notifications:
            notify.deliver(notifications, cfg.settings.notifier)
    store.write_snapshot(events_mod.build_snapshot(results, prs, me))
    inbox = store.write_inbox(results, me, next_poll_at=next_poll_at, strategy=cfg.settings.poll_strategy)
    return inbox, notifications, seeded


def sync_mentions(client: GitHubClient, cfg: Config, send_notifications: bool = True) -> None:
    """Fetch unread @mentions on PRs, store them, and notify about new or renewed ones. The first fetch is
    silent. Never raises: a mentions problem must not stop PR polling. Callers hold the poll lock."""
    ms = cfg.settings.mentions
    if not ms.enabled:
        return
    try:
        mentions = parse_mentions(client.notifications(), include_team=ms.team_mentions)
        prev = store.read_mentions()
        if prev is None or [(m.id, m.updated_at) for m in prev] != [(m.id, m.updated_at) for m in mentions]:
            store.write_mentions(mentions)
        new = events_mod.new_mentions(prev, mentions)
        toggles = cfg.settings.notifications
        if not toggles.team_mention:   # team mentions stay in the list, they just don't pop up
            new = [m for m in new if m.reason != "team_mention"]
        if new and send_notifications and toggles.mention:
            notify.deliver_mentions(new, cfg.settings.notifier)
    except Exception as exc:
        log.warning("mentions sync failed: %s", exc)


def _next_poll_at(cfg: Config) -> float:
    s = cfg.settings
    secs = s.fast_poll_interval_seconds if s.poll_strategy == "aggressive" else s.poll_interval_seconds
    return time.time() + secs


def poll_once(
    cfg: Optional[Config] = None,
    client: Optional[GitHubClient] = None,
    send_notifications: bool = True,
    lock_wait_seconds: float = 0,
) -> PollResult:
    """Run a full poll. Raises config.ConfigError / github.GitHubError / store.LockBusy."""
    start = time.time()
    with store.poll_lock(wait_seconds=lock_wait_seconds):
        cfg = cfg or config_mod.load()
        own_client = client is None
        client = client or GitHubClient()
        points_before = client.points_used
        client.merge_fields = cfg.needs_merge_state()
        try:
            me = _viewer(client)
            prs = build_prs(fetch_all(client, plan_queries(cfg, me)), cfg)
        except Exception as exc:
            store.write_inbox_error(str(exc))
            raise
        finally:
            sync_mentions(client, cfg, send_notifications)   # never raises, and doesn't depend on the PR poll
            points = client.points_used - points_before
            if own_client:
                client.close()
        inbox, notifications, seeded = _publish(cfg, me, prs, send_notifications, _next_poll_at(cfg))

    dur = time.time() - start
    log.info("poll ok: %d PRs, %d in inbox, %d notifications, %.1fs, %d pts%s",
             len(prs), inbox.total, len(notifications), dur, points, " (seeded)" if seeded else "")
    return PollResult(inbox=inbox, notifications=notifications, seeded=seeded, duration_s=dur, points=points)


class Poller:
    """The menu bar app's polling loop state: one long-lived GitHub client and, in aggressive mode,
    the PR nodes from the last fetch plus a fingerprint per PR to compare change checks against."""

    def __init__(self, client_factory: Callable[[], GitHubClient] = GitHubClient,
                 clock: Callable[[], float] = time.time):
        self._client_factory = client_factory
        self._clock = clock
        self._client: Optional[GitHubClient] = None
        self._queries: list[str] = []
        self._fetched: Optional[Fetched] = None
        self._fingerprints: Optional[dict[str, Fingerprint]] = None   # None: checks unavailable until next full poll
        self._unfetchable: set[str] = set()
        self._next_full_at = 0.0
        self._warned_low_rate = False
        self._next_mentions_at = 0.0
        self.last_check_at: Optional[float] = None    # last successful GitHub round trip of any kind

    @property
    def client(self) -> GitHubClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def tick(self, cfg: Config, force_full: bool = False) -> tuple[PollResult, float]:
        """Poll once according to the configured strategy. Returns the result and seconds until the next tick.

        Raises like `poll_once`. A change check raises store.LockBusy right away if another poll holds the lock.
        """
        s = cfg.settings
        aggressive = s.poll_strategy == "aggressive"
        started = self._clock()
        try:
            self.client.merge_fields = cfg.needs_merge_state()
            me = _viewer(self.client)
            queries = plan_queries(cfg, me)
            if aggressive and not force_full and self._clock() < self._next_full_at and self._can_check(queries):
                result = self._check(cfg, me, queries)
            else:
                result = self._full(cfg, me, queries, seed_checks=aggressive)
                self._next_full_at = self._clock() + s.poll_interval_seconds
        except AuthError:
            self._client = None   # re-read the token next time, in case the user re-ran `gh auth login`
            raise
        finally:
            self._sync_mentions(cfg, force=force_full)   # a failed PR poll must not stop mentions
        self.last_check_at = self._clock()

        if aggressive and self._can_check(queries):
            # Checks start every fast_poll_interval_seconds, however long the last one took.
            next_at = min(started + s.fast_poll_interval_seconds, self._next_full_at)
            wait = max(0.0, next_at - self._clock())
        else:
            wait = float(s.poll_interval_seconds)
        return result, wait

    # ------------------------------------------------------------ internals

    def _sync_mentions(self, cfg: Config, force: bool) -> None:
        """Fetch mentions at most as often as GitHub asks (X-Poll-Interval, 60s), whatever the PR poll rate."""
        now = self._clock()
        if not force and now < self._next_mentions_at:
            return
        try:
            with store.poll_lock(wait_seconds=5):
                sync_mentions(self.client, cfg)
        except store.LockBusy:
            return
        except Exception as exc:   # e.g. no usable token right after an AuthError
            log.warning("mentions sync skipped: %s", exc)
            return
        self._next_mentions_at = now + max(60, self.client.notifications_poll_interval)

    def _rate_low(self) -> bool:
        remaining = self.client.rate_remaining
        low = remaining is not None and remaining < LOW_RATE_REMAINING
        if low and not self._warned_low_rate:
            log.warning("only %d GraphQL points left this hour: pausing change checks, full polls only", remaining)
        self._warned_low_rate = low
        return low

    def _can_check(self, queries: list[str]) -> bool:
        return (self._fetched is not None and self._fingerprints is not None
                and queries == self._queries and not self._rate_low())

    def _full(self, cfg: Config, me: str, queries: list[str], seed_checks: bool) -> PollResult:
        start, points_before = time.time(), self.client.points_used
        allowed = allowed_repos(cfg)
        with store.poll_lock(wait_seconds=30):
            try:
                fingerprints = None
                if seed_checks and not self._rate_low():
                    # Fingerprint before fetching, so anything that changes mid-fetch differs on the next check.
                    checked = check_all(self.client, queries, allowed)
                    if checked is None:
                        log.warning("a search returned more than one page: change checks are off until the next full poll")
                    else:
                        fingerprints = checked[0]
                fetched = fetch_all(self.client, queries)
            except Exception as exc:
                store.write_inbox_error(str(exc))
                raise
            fetched.nodes = {k: n for k, n in fetched.nodes.items()
                             if n["repository"]["nameWithOwner"].lower() in allowed}
            prs = build_prs(fetched, cfg)
            inbox, notifications, seeded = _publish(cfg, me, prs, True, _next_poll_at(cfg))
        self._queries, self._fetched, self._fingerprints = queries, fetched, fingerprints
        self._unfetchable = set()

        dur, points = time.time() - start, self.client.points_used - points_before
        log.info("poll ok: %d PRs, %d in inbox, %d notifications, %.1fs, %d pts%s",
                 len(prs), inbox.total, len(notifications), dur, points, " (seeded)" if seeded else "")
        return PollResult(inbox=inbox, notifications=notifications, seeded=seeded, duration_s=dur, points=points)

    def _check(self, cfg: Config, me: str, queries: list[str]) -> PollResult:
        assert self._fetched is not None and self._fingerprints is not None
        start, points_before = time.time(), self.client.points_used
        with store.poll_lock(wait_seconds=0):
            try:
                checked = check_all(self.client, queries, allowed_repos(cfg))
                if checked is None:
                    log.warning("a search returned more than one page: change checks are off until the next full poll")
                    self._fingerprints = None
                    return PollResult(inbox=None, kind="check", duration_s=time.time() - start,
                                      points=self.client.points_used - points_before)
                fingerprints, requested = checked
                known = self._fetched.nodes
                old = self._fingerprints
                # A PR entering or leaving `review-requested:@me` counts as a change even if its fingerprint didn't move.
                changed = [pid for pid, fp in fingerprints.items()
                           if (old.get(pid) != fp or pid not in known
                               or (pid in requested) != (pid in self._fetched.requested_ids))
                           and pid not in self._unfetchable]
                removed = [pid for pid in known if pid not in fingerprints]
                self._fingerprints = fingerprints
                self._fetched.requested_ids = requested

                if not changed and not removed:
                    store.clear_inbox_error()
                    dur, points = time.time() - start, self.client.points_used - points_before
                    log.debug("check: no changes, %.1fs, %d pts", dur, points)
                    return PollResult(inbox=None, kind="check", duration_s=dur, points=points)

                refetched = {n["id"]: n for n in self.client.nodes_by_id(changed)} if changed else {}
            except Exception as exc:
                store.write_inbox_error(str(exc))
                raise
            for pid in removed:
                known.pop(pid, None)
            known.update(refetched)
            self._unfetchable |= {pid for pid in changed if pid not in refetched}
            prs = build_prs(self._fetched, cfg)
            inbox, notifications, seeded = _publish(cfg, me, prs, True, _next_poll_at(cfg))

        dur, points = time.time() - start, self.client.points_used - points_before
        log.info("check: %d changed, %d gone, %d in inbox, %d notifications, %.1fs, %d pts",
                 len(changed), len(removed), inbox.total, len(notifications), dur, points)
        return PollResult(inbox=inbox, notifications=notifications, seeded=seeded, duration_s=dur,
                          kind="check", changed=len(changed) + len(removed), points=points)
