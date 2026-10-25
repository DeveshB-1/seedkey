"""Score a review against an answer key of seeded issues."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .parse import ACTIONABLE, TIER_RANK, Finding, Review

KINDS = {"bug", "convention", "standard", "intentional"}


@dataclass
class Issue:
    id: str
    kind: str
    path: str
    lines: tuple[int, int]
    match: str
    summary: str
    severity: str = "major"
    seeded: bool = True
    basis: str = "code"
    exclude: str = ""
    mention: str = ""

    def anchored(self, f: Finding, slop: int) -> bool:
        lo, hi = self.lines
        return f.path == self.path and not (f.end < lo - slop or f.start > hi + slop)

    def matches(self, f: Finding, slop: int) -> bool:
        """Same file, and either anchored near the issue with text matching
        `match`, or (for findings that bundle several issues in one comment)
        anywhere in the file with text matching the more specific `mention`."""
        if f.path != self.path:
            return False
        if self.exclude and re.search(self.exclude, f.text, re.I):
            return False
        if self.anchored(f, slop) and re.search(self.match, f.text, re.I):
            return True
        return bool(self.mention) and bool(re.search(self.mention, f.text, re.I))


@dataclass
class Profile:
    name: str
    description: str = ""
    must_flag: list[str] = field(default_factory=list)
    must_not_flag: list[str] = field(default_factory=list)
    config: str | None = None


@dataclass
class Key:
    name: str
    repo: str
    slop: int
    issues: list[Issue]
    profiles: dict[str, Profile]

    def get(self, issue_id: str) -> Issue:
        for i in self.issues:
            if i.id == issue_id:
                return i
        raise KeyError(issue_id)


def load_key(path: str | Path) -> Key:
    data = tomllib.loads(Path(path).read_text())
    meta = data.get("meta", {})
    issues = []
    for raw in data.get("issue", []):
        lines = raw["lines"]
        if isinstance(lines, int):
            lines = [lines, lines]
        if raw["kind"] not in KINDS:
            raise ValueError(f"{raw['id']}: kind must be one of {sorted(KINDS)}")
        re.compile(raw["match"])
        re.compile(raw.get("mention", ""))
        issues.append(
            Issue(
                id=raw["id"],
                kind=raw["kind"],
                path=raw["path"],
                lines=(int(lines[0]), int(lines[-1])),
                match=raw["match"],
                summary=raw.get("summary", ""),
                severity=raw.get("severity", "major"),
                seeded=raw.get("seeded", True),
                basis=raw.get("basis", "code"),
                exclude=raw.get("exclude", ""),
                mention=raw.get("mention", ""),
            )
        )
    ids = [i.id for i in issues]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate issue ids in key")
    profiles = {}
    for name, p in data.get("profiles", {}).items():
        for ref in p.get("must_flag", []) + p.get("must_not_flag", []):
            if ref not in ids:
                raise ValueError(f"profile {name} references unknown issue {ref}")
        profiles[name] = Profile(
            name=name,
            description=p.get("description", ""),
            must_flag=p.get("must_flag", []),
            must_not_flag=p.get("must_not_flag", []),
            config=p.get("config"),
        )
    return Key(meta.get("name", Path(path).stem), meta.get("repo", ""), int(meta.get("slop", 3)), issues, profiles)


def status_of(findings: list[Finding]) -> str:
    """Best tier among the findings that matched one issue."""
    if not findings:
        return "missed"
    best = max(findings, key=lambda f: TIER_RANK.get(f.tier, 0))
    if best.tier in ACTIONABLE:
        return "caught"
    return "nitpick" if best.tier == "nitpick" else "noted"


FLAGGED = {"caught", "nitpick"}


@dataclass
class Score:
    review: Review | None
    statuses: dict[str, str]
    hits: dict[str, list[Finding]]
    unkeyed: list[Finding]
    findings: list[Finding]
    key: Key
    checks: list[tuple[bool, str]] = field(default_factory=list)

    def _ids(self, kind: str, seeded: bool | None = None) -> list[str]:
        return [
            i.id
            for i in self.key.issues
            if i.kind == kind and (seeded is None or i.seeded == seeded)
        ]

    def recall(self, kind: str = "bug", seeded: bool = True) -> tuple[int, int]:
        ids = self._ids(kind, seeded)
        return sum(self.statuses[i] == "caught" for i in ids), len(ids)

    @property
    def keyed_precision(self) -> float | None:
        """Share of findings that hit something in the key. Unkeyed findings
        are not necessarily wrong, so treat this as a lower bound."""
        if not self.findings:
            return None
        return (len(self.findings) - len(self.unkeyed)) / len(self.findings)

    @property
    def passed(self) -> bool:
        return all(ok for ok, _ in self.checks)

    def to_dict(self) -> dict:
        c, n = self.recall()
        r = self.review
        return {
            "review_id": r.id if r else None,
            "commit": r.commit if r else None,
            "submitted_at": r.submitted_at if r else None,
            "config_used": r.config_used if r else None,
            "config_source": r.config_source if r else None,
            "seeded_bug_recall": {"caught": c, "total": n},
            "statuses": self.statuses,
            "findings": len(self.findings),
            "unkeyed": [f.to_dict() for f in self.unkeyed],
            "keyed_precision_lower_bound": self.keyed_precision,
            "checks": [{"ok": ok, "detail": d} for ok, d in self.checks],
            "passed": self.passed,
        }


def score(key: Key, findings: list[Finding], review: Review | None = None, profile: str | None = None) -> Score:
    hits: dict[str, list[Finding]] = {i.id: [] for i in key.issues}
    matched: set[int] = set()
    for idx, f in enumerate(findings):
        for issue in key.issues:
            if issue.matches(f, key.slop):
                hits[issue.id].append(f)
                matched.add(idx)
    statuses = {iid: status_of(fs) for iid, fs in hits.items()}
    unkeyed = [f for idx, f in enumerate(findings) if idx not in matched]
    s = Score(review, statuses, hits, unkeyed, findings, key)
    if profile:
        s.checks = check_profile(key.profiles[profile], s)
    return s


def check_profile(p: Profile, s: Score) -> list[tuple[bool, str]]:
    out = []
    if p.config and s.review is not None:
        src = s.review.config_source
        out.append((src == p.config, f"config source is {src!r} (want {p.config!r}; review says: {s.review.config_used})"))
    for iid in p.must_flag:
        st = s.statuses[iid]
        out.append((st in FLAGGED, f"must flag {iid}: {st}"))
    for iid in p.must_not_flag:
        st = s.statuses[iid]
        out.append((st not in FLAGGED, f"must not flag {iid}: {st}"))
    return out


def diff(a: Score, b: Score) -> list[tuple[str, str, str]]:
    """Issues whose status differs between two scored runs."""
    return [(iid, a.statuses[iid], b.statuses[iid]) for iid in a.statuses if a.statuses[iid] != b.statuses[iid]]
