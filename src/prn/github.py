"""GitHub GraphQL client. Auth comes from `gh auth token` (or GITHUB_TOKEN / GH_TOKEN if set)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, TypeVar

import httpx

from .config import Config, Section
from .models import PR, Review

log = logging.getLogger(__name__)

API_URL = "https://api.github.com/graphql"
SEARCH_BASE = "is:pr is:open archived:false"
MAX_QUERY_LEN = 256          # GitHub search query length limit
PAGE_SIZE = 50
MAX_PAGES = 20               # 1000 results: GitHub's search ceiling
CHECK_PAGE_SIZE = 100        # change checks fetch one page per search; more than this falls back to a full poll
NODES_BATCH = 25             # PRs per refetch request
MAX_PARALLEL = 6             # concurrent GraphQL requests

# launchd starts with a minimal PATH, so check Homebrew locations explicitly.
EXTRA_BIN_DIRS = ["/opt/homebrew/bin", "/usr/local/bin"]


class GitHubError(Exception):
    pass


class AuthError(GitHubError):
    pass


def find_binary(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for d in EXTRA_BIN_DIRS:
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return None


def get_token() -> str:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    gh = find_binary("gh")
    if not gh:
        raise AuthError("GitHub CLI (`gh`) not found. Install it (`brew install gh`) and run `gh auth login`.")
    try:
        out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired as exc:
        raise AuthError("`gh auth token` timed out") from exc
    token = out.stdout.strip()
    if out.returncode != 0 or not token:
        raise AuthError("`gh auth token` failed. Run `gh auth login` first. " + out.stderr.strip())
    return token


# ---------------------------------------------------------------- GraphQL

PR_FIELDS = """
fragment PRFields on PullRequest {
  id number title url isDraft createdAt updatedAt reviewDecision mergeable mergeStateStatus
  repository { nameWithOwner }
  author { __typename login }
  labels(first: 20) { nodes { name } }
  comments(last: 20) { totalCount nodes { author { login } } }
  reviews(last: 60) {
    totalCount
    nodes { author { __typename login } state submittedAt commit { oid } }
  }
  latestOpinionatedReviews(first: 50) {
    nodes { author { __typename login } state submittedAt commit { oid } }
  }
  reviewRequests(first: 50) {
    nodes {
      requestedReviewer {
        __typename
        ... on User { login }
        ... on Bot { login }
        ... on Mannequin { login }
        ... on Team { slug organization { login } }
      }
    }
  }
  commits(last: 1) { nodes { commit { oid statusCheckRollup { state } } } }
}
"""

SEARCH_QUERY = (
    PR_FIELDS
    + """
query($q: String!, $first: Int!, $after: String) {
  rateLimit { remaining resetAt cost }
  search(query: $q, type: ISSUE, first: $first, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ...PRFields }
  }
}
"""
)

NODES_QUERY = (
    PR_FIELDS
    + """
query($ids: [ID!]!) {
  rateLimit { remaining resetAt cost }
  nodes(ids: $ids) { ...PRFields }
}
"""
)

# Only fields GitHub can answer quickly. reviewDecision and mergeable are computed on demand and
# make the check several times slower; reading CI through headRef avoids a connection, so it stays at 1 point.
CHECK_FIELDS = ("id updatedAt headRefOid repository { nameWithOwner } "
                "headRef { target { ... on Commit { statusCheckRollup { state } } } }")


def _check_query(n: int) -> str:
    decl = ", ".join(f"$q{i}: String!" for i in range(n))
    searches = " ".join(
        f"s{i}: search(query: $q{i}, type: ISSUE, first: {CHECK_PAGE_SIZE}) "
        f"{{ pageInfo {{ hasNextPage }} nodes {{ ... on PullRequest {{ {CHECK_FIELDS} }} }} }}"
        for i in range(n)
    )
    return f"query({decl}) {{ rateLimit {{ remaining resetAt cost }} {searches} }}"


VIEWER_QUERY = "query { viewer { login } }"

T = TypeVar("T")
R = TypeVar("R")


def _parallel(fn: Callable[[T], R], items: list[T]) -> list[R]:
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=min(len(items), MAX_PARALLEL), thread_name_prefix="prn-gh") as ex:
        return list(ex.map(fn, items))


class GitHubClient:
    def __init__(self, token: Optional[str] = None, transport: Optional[httpx.BaseTransport] = None):
        self._token = token or get_token()
        self._http = httpx.Client(
            timeout=30,
            transport=transport,
            headers={
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "prn-pr-review-notifier",
                "Accept": "application/vnd.github+json",
            },
        )
        self.rate_remaining: Optional[int] = None
        self.points_used = 0     # sum of GraphQL rateLimit.cost across this client's requests
        self._lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    def graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        payload = {"query": query, "variables": variables or {}}
        for attempt in range(3):
            try:
                resp = self._http.post(API_URL, json=payload)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise GitHubError(f"network error talking to GitHub: {exc}") from exc
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 401:
                raise AuthError("GitHub rejected the token (401). Run `gh auth login` again.")
            if resp.status_code in (502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise GitHubError(f"GitHub API error {resp.status_code}: {resp.text[:300]}")
            body = resp.json()
            if body.get("errors"):
                msgs = "; ".join(e.get("message", str(e)) for e in body["errors"])
                raise GitHubError(f"GraphQL error: {msgs}")
            data = body.get("data") or {}
            rl = data.get("rateLimit")
            if rl:
                with self._lock:
                    self.rate_remaining = rl.get("remaining")
                    self.points_used += rl.get("cost") or 0
            return data
        raise GitHubError("GitHub request failed after retries")

    def viewer_login(self) -> str:
        return self.graphql(VIEWER_QUERY)["viewer"]["login"]

    def repo_exists(self, full_name: str) -> bool:
        owner, name = full_name.split("/", 1)
        try:
            data = self.graphql("query($o:String!,$n:String!){ repository(owner:$o, name:$n){ nameWithOwner } }",
                                {"o": owner, "n": name})
        except GitHubError:
            return False
        return bool(data.get("repository"))

    def frequent_review_partners(self, repos: list[str], me: str, days: int = 90, limit: int = 20) -> list[tuple[str, int]]:
        """Authors of PRs I reviewed in `repos` over the last `days`, most frequent first (bots excluded)."""
        from collections import Counter
        from datetime import date, timedelta

        since = (date.today() - timedelta(days=days)).isoformat()
        query = """query($q:String!,$after:String){ search(query:$q, type:ISSUE, first:100, after:$after){
            pageInfo{ hasNextPage endCursor } nodes{ ... on PullRequest { author { __typename login } } } } }"""
        counts: Counter = Counter()
        for q in _chunk_terms(f"is:pr reviewed-by:{me} created:>{since}", [f"repo:{r}" for r in repos]):
            after = None
            for _ in range(5):
                data = self.graphql(query, {"q": q, "after": after})["search"]
                for n in data["nodes"]:
                    a = (n or {}).get("author") or {}
                    login = (a.get("login") or "").lower()
                    if login and login != me.lower() and a.get("__typename") != "Bot" and not login.endswith("[bot]"):
                        counts[login] += 1
                if not data["pageInfo"]["hasNextPage"]:
                    break
                after = data["pageInfo"]["endCursor"]
        return counts.most_common(limit)

    def search_prs(self, q: str) -> list[dict]:
        nodes: list[dict] = []
        after = None
        for _ in range(MAX_PAGES):
            data = self.graphql(SEARCH_QUERY, {"q": q, "first": PAGE_SIZE, "after": after})
            search = data["search"]
            nodes.extend(n for n in search["nodes"] if n)
            if not search["pageInfo"]["hasNextPage"]:
                break
            after = search["pageInfo"]["endCursor"]
        return nodes

    def check_searches(self, queries: list[str]) -> Optional[list[list[dict]]]:
        """Run every search in one request with only change-detection fields.

        Returns one node list per query, or None if any search has more results than one page holds.
        """
        data = self.graphql(_check_query(len(queries)), {f"q{i}": q for i, q in enumerate(queries)})
        out = []
        for i in range(len(queries)):
            search = data[f"s{i}"]
            if search["pageInfo"]["hasNextPage"]:
                return None
            out.append([n for n in search["nodes"] if n])
        return out

    def nodes_by_id(self, ids: list[str]) -> list[dict]:
        """Full PR nodes for `ids`, fetched in parallel batches. PRs that no longer exist are dropped."""
        batches = [ids[i:i + NODES_BATCH] for i in range(0, len(ids), NODES_BATCH)]
        results = _parallel(lambda b: self.graphql(NODES_QUERY, {"ids": b})["nodes"], batches)
        return [n for batch in results for n in batch if n]


# ---------------------------------------------------------------- parsing


def _login(actor: Optional[dict]) -> str:
    # GraphQL usually reports app logins without the REST-style "[bot]" suffix; normalize either way.
    return ((actor or {}).get("login") or "ghost").lower().removesuffix("[bot]")


def _is_bot(actor: Optional[dict]) -> bool:
    return (actor or {}).get("__typename") == "Bot"


def _reviews(nodes: Iterable[dict]) -> list[Review]:
    out = []
    for n in nodes or []:
        if not n or n.get("state") == "PENDING":
            continue
        out.append(Review(author=_login(n.get("author")), state=n["state"],
                          submitted_at=n.get("submittedAt"), is_bot=_is_bot(n.get("author")),
                          commit=(n.get("commit") or {}).get("oid")))
    return out


def parse_pr(node: dict, requested_from_me: bool = False) -> PR:
    users, teams = [], []
    for rr in (node.get("reviewRequests") or {}).get("nodes", []):
        rev = (rr or {}).get("requestedReviewer") or {}
        if rev.get("__typename") == "Team":
            teams.append(f"{rev['organization']['login']}/{rev['slug']}".lower())
        elif rev.get("login"):
            users.append(rev["login"].lower())

    commits = (node.get("commits") or {}).get("nodes") or []
    head = ((commits[0] or {}).get("commit") or {}) if commits else {}
    rollup = head.get("statusCheckRollup")
    ci_raw = (rollup or {}).get("state")
    ci = {"SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure",
          "PENDING": "pending", "EXPECTED": "pending"}.get(ci_raw or "", "none")

    reviews = node.get("reviews") or {}
    return PR(
        id=node["id"],
        repo=node["repository"]["nameWithOwner"],
        number=node["number"],
        title=node["title"],
        url=node["url"],
        author=_login(node.get("author")),
        is_draft=bool(node.get("isDraft")),
        created_at=node["createdAt"],
        updated_at=node["updatedAt"],
        review_decision=(node.get("reviewDecision") or "none").lower(),
        mergeable=(node.get("mergeable") or "unknown").lower(),
        merge_state=(node.get("mergeStateStatus") or "unknown").lower(),
        head_oid=head.get("oid") or "",
        ci=ci,
        labels=[l["name"] for l in (node.get("labels") or {}).get("nodes", []) if l],
        comment_count=(node.get("comments") or {}).get("totalCount", 0),
        recent_comment_authors=[_login((c or {}).get("author"))
                                for c in (node.get("comments") or {}).get("nodes", [])],
        review_count=reviews.get("totalCount", 0),
        reviews=_reviews(reviews.get("nodes")),
        latest_reviews=_reviews((node.get("latestOpinionatedReviews") or {}).get("nodes")),
        requested_users=users,
        requested_teams=teams,
        review_requested_from_me=requested_from_me,
    )


# ---------------------------------------------------------------- query planning

REVIEW_REQUESTED_ME = "review-requested:@me"


def _chunk_terms(prefix: str, terms: list[str]) -> list[str]:
    """Split OR-able qualifiers (repo:/author:) across queries to respect the 256-char limit."""
    if not terms:
        return [prefix.strip()]
    queries, cur = [], prefix
    for t in terms:
        candidate = f"{cur} {t}"
        if len(candidate) > MAX_QUERY_LEN and cur != prefix:
            queries.append(cur.strip())
            cur = f"{prefix} {t}"
        else:
            cur = candidate
    queries.append(cur.strip())
    return queries


def plan_queries(cfg: Config, me: str) -> list[str]:
    """Build the minimal set of search queries whose union covers every section.

    Results are re-filtered client-side, so queries only need to be supersets.
    When a section narrows by author or review request, we scope by `org:` (short)
    and filter repos client-side; otherwise we list repos explicitly.
    """
    queries: list[str] = []
    all_repos = sorted({r for s in cfg.sections for r in cfg.section_repos(s)})
    orgs = sorted({r.split("/")[0] for r in all_repos})
    org_scope = " ".join(f"org:{o}" for o in orgs)

    # Always fetch: my PRs (status/CI events) and PRs requesting me (team detection, re-review).
    queries.append(f"{SEARCH_BASE} {org_scope} author:{me}")
    queries.append(f"{SEARCH_BASE} {org_scope} {REVIEW_REQUESTED_ME}")

    for s in cfg.sections:
        f = s.filters
        narrowing = []
        if f.draft is True:
            narrowing.append("draft:true")
        elif f.draft is False:
            narrowing.append("draft:false")

        if f.review_requested in ("me", "team", "any"):
            queries.append(f"{SEARCH_BASE} {org_scope} {REVIEW_REQUESTED_ME} {' '.join(narrowing)}".strip())
            continue

        authors = sorted(cfg.resolve_people(f.authors, me)) if f.authors else []
        prefix = f"{SEARCH_BASE} {' '.join(narrowing)}".strip()
        if authors:
            queries.extend(_chunk_terms(f"{prefix} {org_scope}", [f"author:{a}" for a in authors]))
        else:
            repos = cfg.section_repos(s)
            queries.extend(_chunk_terms(prefix, [f"repo:{r}" for r in repos]))

    # De-duplicate, then drop queries that are strictly narrower than another one.
    seen, uniq = set(), []
    for q in queries:
        q = " ".join(q.split())
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return [q for q in uniq if not any(other != q and _subsumes(other, q) for other in uniq)]


_OR_PREFIXES = ("author:", "repo:", "org:")


def _split(q: str) -> tuple[frozenset, frozenset]:
    toks = q.split()
    ors = frozenset(t for t in toks if t.startswith(_OR_PREFIXES))
    ands = frozenset(t for t in toks if not t.startswith(_OR_PREFIXES))
    return ors, ands


def _subsumes(broad: str, narrow: str) -> bool:
    """True if every result of `narrow` is guaranteed to be a result of `broad`."""
    b_or, b_and = _split(broad)
    n_or, n_and = _split(narrow)
    return b_or == n_or and b_and < n_and


@dataclass
class Fetched:
    """Raw PR nodes from a set of searches, keyed by node id."""
    nodes: dict[str, dict] = field(default_factory=dict)
    requested_ids: set[str] = field(default_factory=set)   # PRs returned by a `review-requested:@me` search


def fetch_all(client: GitHubClient, queries: list[str]) -> Fetched:
    """Run every search in parallel and union the results by PR id."""
    for q in queries:
        log.debug("search: %s", q)
    out = Fetched()
    for q, nodes in zip(queries, _parallel(client.search_prs, queries)):
        for n in nodes:
            out.nodes[n["id"]] = n
            if REVIEW_REQUESTED_ME in q:
                out.requested_ids.add(n["id"])
    return out


Fingerprint = tuple[str, str, Optional[str]]


def allowed_repos(cfg: Config) -> set[str]:
    return {r.lower() for s in cfg.sections for r in cfg.section_repos(s)}


def check_all(client: GitHubClient, queries: list[str],
              allowed: set[str]) -> Optional[tuple[dict[str, Fingerprint], set[str]]]:
    """Cheap change check: (fingerprint per PR id, ids returned by `review-requested:@me`), or None if a
    search overflowed one page. PRs outside `allowed` repos are skipped.

    A fingerprint changes on reviews, comments, pushes, edits and CI results. It doesn't change when
    GitHub recomputes merge conflicts, so callers still need an occasional full poll.
    """
    results = client.check_searches(queries)
    if results is None:
        return None
    fingerprints: dict[str, Fingerprint] = {}
    requested: set[str] = set()
    for q, nodes in zip(queries, results):
        for n in nodes:
            if n["repository"]["nameWithOwner"].lower() not in allowed:
                continue
            target = (n.get("headRef") or {}).get("target") or {}
            fingerprints[n["id"]] = (n["updatedAt"], n.get("headRefOid") or "",
                                     (target.get("statusCheckRollup") or {}).get("state"))
            if REVIEW_REQUESTED_ME in q:
                requested.add(n["id"])
    return fingerprints, requested


def build_prs(fetched: Fetched, cfg: Config) -> list[PR]:
    """Parse nodes into PRs, keeping only repos some section watches. Newest activity first."""
    allowed = allowed_repos(cfg)
    prs = [parse_pr(node, requested_from_me=pid in fetched.requested_ids)
           for pid, node in fetched.nodes.items()
           if node["repository"]["nameWithOwner"].lower() in allowed]
    prs.sort(key=lambda p: p.updated_at, reverse=True)
    return prs


def fetch_prs(client: GitHubClient, cfg: Config, me: str) -> list[PR]:
    """Run planned queries, union by PR id, and mark which PRs request a review from me."""
    return build_prs(fetch_all(client, plan_queries(cfg, me)), cfg)
