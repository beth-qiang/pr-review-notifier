"""One poll cycle: fetch → evaluate → diff → notify → persist."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from . import config as config_mod
from . import events as events_mod
from . import notify, store
from .config import Config
from .engine import SectionResult, evaluate
from .github import GitHubClient, fetch_prs

log = logging.getLogger(__name__)


@dataclass
class PollResult:
    inbox: store.Inbox
    notifications: list[events_mod.Notification] = field(default_factory=list)
    seeded: bool = False
    duration_s: float = 0.0


_me_cache: dict[str, str] = {}


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
        try:
            me = _me_cache.get("login") or client.viewer_login()
            _me_cache["login"] = me
            prs = fetch_prs(client, cfg, me)
        except Exception as exc:
            store.write_inbox_error(str(exc))
            raise
        finally:
            if own_client:
                client.close()

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
        inbox = store.write_inbox(results, me, next_poll_at=time.time() + cfg.settings.poll_interval_seconds)

    dur = time.time() - start
    log.info("poll ok: %d PRs, %d in inbox, %d notifications, %.1fs%s",
             len(prs), inbox.total, len(notifications), dur, " (seeded)" if seeded else "")
    return PollResult(inbox=inbox, notifications=notifications, seeded=seeded, duration_s=dur)
