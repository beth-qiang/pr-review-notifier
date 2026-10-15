"""Config models, loading, validation, and comment-preserving edits."""

from __future__ import annotations

import io
import re
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from ruamel.yaml import YAML

from . import paths

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

MyReviewState = Literal["none", "commented", "changes_requested", "approved", "dismissed"]
ReviewDecision = Literal["approved", "changes_requested", "review_required", "none"]
CIState = Literal["success", "failure", "pending", "none"]
ReviewRequested = Literal["me", "team", "any", "none"]


class ConfigError(Exception):
    """Raised for any problem loading or validating config. Message is user-facing."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Range(_Strict):
    min: Optional[int] = Field(default=None, ge=0)
    max: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check(self) -> "Range":
        if self.min is None and self.max is None:
            raise ValueError("set at least one of min / max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min cannot be greater than max")
        return self


class Not(_Strict):
    not_: MyReviewState = Field(alias="not")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Filters(_Strict):
    repos: Optional[list[str]] = None
    exclude_repos: list[str] = Field(default_factory=list)
    authors: Optional[list[str]] = None
    exclude_authors: list[str] = Field(default_factory=list)
    review_requested: Optional[ReviewRequested] = None
    my_review: Optional[Union[MyReviewState, Not]] = None
    review_decision: Optional[ReviewDecision] = None
    approvals: Optional[Range] = None
    has_reviews_from_others: Optional[bool] = None
    has_reviews: Optional[bool] = None          # anyone but the author (me included) has reviewed
    awaiting_my_review: Optional[bool] = None   # I haven't reviewed yet, OR my review was re-requested directly
    draft: Optional[bool] = None
    labels: list[str] = Field(default_factory=list)          # PR has ANY of these
    exclude_labels: list[str] = Field(default_factory=list)  # PR has NONE of these
    ci: Optional[CIState] = None
    conflicts: Optional[bool] = None            # GitHub reports merge conflicts with the base branch
    updated_within_days: Optional[int] = Field(default=None, ge=1)

    @field_validator("repos", "exclude_repos")
    @classmethod
    def _repo_format(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        for r in v or []:
            if not REPO_RE.match(r):
                raise ValueError(f"repo '{r}' must be in owner/name form")
        return v


class Section(_Strict):
    name: str = Field(min_length=1)
    notify: bool = True
    badge: bool = False
    display: bool = True     # false: still claims matching PRs, but is left out of every inbox view
    filters: Filters = Field(default_factory=Filters)


class NotificationToggles(_Strict):
    new_in_section: bool = True
    re_review_requested: bool = True
    my_pr_status: bool = True
    ci_status: bool = True


class Settings(_Strict):
    poll_interval_seconds: int = Field(default=300, ge=60)
    open_with: Literal["github"] = "github"
    notifier: Literal["terminal-notifier", "osascript"] = "terminal-notifier"
    hide_empty_sections: bool = True
    max_title_length: int = Field(default=50, ge=10)
    ignore_bot_reviews: bool = False   # don't count bot reviews for has_reviews_from_others
    # Reviewers whose reviews never count toward `approvals` or `has_reviews_from_others`
    ignored_reviewers: list[str] = Field(default_factory=list)
    notifications: NotificationToggles = Field(default_factory=NotificationToggles)


class Defaults(_Strict):
    repos: list[str] = Field(min_length=1)

    @field_validator("repos")
    @classmethod
    def _repo_format(cls, v: list[str]) -> list[str]:
        for r in v:
            if not REPO_RE.match(r):
                raise ValueError(f"repo '{r}' must be in owner/name form")
        return v


class Config(_Strict):
    settings: Settings = Field(default_factory=Settings)
    defaults: Defaults
    groups: dict[str, list[str]] = Field(default_factory=dict)
    sections: list[Section] = Field(min_length=1)

    @model_validator(mode="after")
    def _cross_checks(self) -> "Config":
        seen: set[str] = set()
        for s in self.sections:
            key = s.name.lower()
            if key in seen:
                raise ValueError(f"duplicate section name '{s.name}'")
            seen.add(key)
            for who in (s.filters.authors or []) + s.filters.exclude_authors:
                if who.startswith("group:") and who[6:] not in self.groups:
                    raise ValueError(f"section '{s.name}' references unknown group '{who}'")
        return self

    def ignored_reviewer_logins(self) -> frozenset[str]:
        """Lowercase logins; a trailing '[bot]' is dropped because GraphQL reports app logins without it."""
        return frozenset(u.lower().removesuffix("[bot]").lstrip("@") for u in self.settings.ignored_reviewers)

    def section_repos(self, section: Section) -> list[str]:
        repos = section.filters.repos or self.defaults.repos
        excluded = {r.lower() for r in section.filters.exclude_repos}
        return [r for r in repos if r.lower() not in excluded]

    def resolve_people(self, tokens: list[str], me: str) -> set[str]:
        """Expand '@me' and 'group:<name>' into lowercase logins."""
        out: set[str] = set()
        for t in tokens:
            if t == "@me":
                out.add(me.lower())
            elif t.startswith("group:"):
                out.update(u.lower() for u in self.groups.get(t[6:], []))
            else:
                out.add(t.lower())
        return out


# ---------------------------------------------------------------- loading


def _yaml() -> YAML:
    y = YAML()  # round-trip mode keeps comments and ordering
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 120
    return y


EXAMPLE_NAME = "config.example.yaml"


def example_config_text() -> str:
    """The committed example config. Packaged into the wheel; read from the repo root when running from source."""
    packaged = resources.files("prn").joinpath(EXAMPLE_NAME)
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    repo_copy = Path(__file__).resolve().parents[2] / EXAMPLE_NAME
    return repo_copy.read_text(encoding="utf-8")


default_config_text = example_config_text  # backwards-compatible alias


def _format_validation_error(err: ValidationError) -> str:
    lines = []
    unknown_keys = False
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"])
        lines.append(f"  - {loc or '<root>'}: {e['msg']}")
        unknown_keys = unknown_keys or e.get("type") == "extra_forbidden"
    msg = "Invalid config:\n" + "\n".join(lines)
    if unknown_keys:
        msg += ("\n\nIf that key is a typo, fix it. If it's a newer setting, your installed prn is older than the "
                "repo: run `uv tool install --force --reinstall <repo>` then `prn daemon restart` "
                "(or re-run ./setup.sh and choose 'keep').")
    return msg


def parse(text: str) -> Config:
    try:
        data = _yaml().load(text) or {}
    except Exception as exc:  # ruamel raises several error types
        raise ConfigError(f"Could not parse YAML: {exc}") from exc
    try:
        return Config.model_validate(_to_plain(data))
    except ValidationError as err:
        raise ConfigError(_format_validation_error(err)) from err


def load(path: Optional[Path] = None) -> Config:
    path = path or paths.config_path()
    if not path.exists():
        raise ConfigError(f"No config at {path}. Run `prn config init` to create one.")
    return parse(path.read_text(encoding="utf-8"))


def init(force: bool = False, path: Optional[Path] = None) -> Path:
    """Copy the example config into place. If `path` is given, also point prn at it."""
    paths.ensure_dirs()
    target = path or paths.config_path()
    if target.exists() and not force:
        raise ConfigError(f"{target} already exists (use --force to overwrite)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(example_config_text(), encoding="utf-8")
    if path is not None:
        paths.set_config_pointer(target)
    return target


def _to_plain(obj: Any) -> Any:
    """Convert ruamel Commented* containers into plain dict/list for pydantic."""
    if isinstance(obj, dict):
        return {str(k): _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj


# ---------------------------------------------------------------- editing (comment-preserving)


class ConfigEditor:
    """Load the YAML in round-trip mode, mutate it, validate, then write it back."""

    def __init__(self, path: Optional[Path] = None, text: Optional[str] = None):
        self.path = path or paths.config_path()
        if text is None:
            if not self.path.exists():
                raise ConfigError(f"No config at {self.path}. Run ./setup.sh (or `prn setup`) first.")
            text = self.path.read_text(encoding="utf-8")
        self._y = _yaml()
        self.doc = self._y.load(text)

    @staticmethod
    def replace_seq(seq, values: list) -> None:
        """Replace a YAML list's items in place, keeping the trailing comment/blank line of the list."""
        trailing = seq.ca.items.get(len(seq) - 1) if len(seq) else None
        while len(seq):
            seq.pop()
        seq.ca.items.clear()
        seq.extend(values)
        if trailing is not None and values:
            seq.ca.items[len(values) - 1] = trailing

    def section_names(self) -> list[str]:
        return [str(s.get("name", "")) for s in self._sections()]

    def _sections(self):
        secs = self.doc.get("sections")
        if secs is None:
            raise ConfigError("config has no `sections:` list")
        return secs

    def _index(self, name: str) -> int:
        for i, s in enumerate(self._sections()):
            if str(s.get("name", "")).lower() == name.lower():
                return i
        raise ConfigError(f"no section named '{name}'")

    def add_section(self, section: dict, position: Optional[int] = None) -> None:
        secs = self._sections()
        if position is None or position >= len(secs):
            secs.append(section)
        else:
            secs.insert(max(position, 0), section)

    def remove_section(self, name: str) -> None:
        del self._sections()[self._index(name)]

    def move_section(self, name: str, to: int) -> None:
        secs = self._sections()
        i = self._index(name)
        item = secs.pop(i)
        secs.insert(max(0, min(to, len(secs))), item)

    def dumps(self) -> str:
        buf = io.StringIO()
        self._y.dump(self.doc, buf)
        return buf.getvalue()

    def save(self) -> Config:
        text = self.dumps()
        cfg = parse(text)  # validate before writing
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)
        return cfg
