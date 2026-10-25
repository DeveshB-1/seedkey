"""Turn CodeRabbit's GitHub review output into a flat list of findings.

CodeRabbit reports findings in two places:

* inline review comments (GitHub "pull request review comments"), one comment
  can hold several findings separated by a ``---`` line;
* sections inside the review body: "Duplicate comments", "Outside diff range
  comments", "Nitpick comments" and "Additional comments". Entries there look
  like "`14-19`: _category_ | _severity_ ..." under a
  "<summary>path (N)</summary>" block.

Everything here works on the raw JSON the GitHub REST API returns, so the same
code reads a live fetch and a saved fixture.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

BOT_LOGIN = "coderabbitai[bot]"

# How much each tier counts. "actionable" tiers are the ones CodeRabbit asks
# you to fix; "noted" is the Additional comments section, which is shown in the
# review body but is not posted as an actionable comment.
TIER_RANK = {"inline": 3, "duplicate": 3, "outside_diff": 3, "nitpick": 2, "additional": 1}
ACTIONABLE = {"inline", "duplicate", "outside_diff"}

_SECTION_RE = re.compile(
    r"<summary>[^<]*?(Duplicate|Outside diff range|Nitpick|Additional) comments \((\d+)\)\s*</summary>"
)
_SECTION_TIER = {
    "Duplicate": "duplicate",
    "Outside diff range": "outside_diff",
    "Nitpick": "nitpick",
    "Additional": "additional",
}
_END_RE = re.compile(
    r"<summary>[^<]*(Prompt for all review comments|Autofix|Review info|Review details|"
    r"Additional context used)[^<]*</summary>"
)
_FILE_RE = re.compile(r"<summary>\s*([^<\s][^<]*?) \((\d+)\)\s*</summary>")
_ENTRY_RE = re.compile(r"^`(\d+)(?:-(\d+))?`:\s*(.*)$")
_SEVERITY_RE = re.compile(r"_[^\w\s]+\s*(Critical|Major|Minor|Trivial|Info)_")
_CATEGORY_RE = re.compile(r"^_[^\w\s]*\s*([A-Z][^_|]*?)_\s*(?:\||$)", re.M)
_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_CONFIG_RE = re.compile(r"\*\*Configuration used\*\*:\s*([^\n]+)")
_PROFILE_RE = re.compile(r"\*\*Review profile\*\*:\s*(\w+)")
_ACTIONABLE_RE = re.compile(r"\*\*Actionable comments posted:\s*(\d+)\*\*")


@dataclass
class Finding:
    path: str
    start: int
    end: int
    tier: str
    text: str
    title: str = ""
    severity: str | None = None
    category: str | None = None
    review_id: int | None = None

    @property
    def actionable(self) -> bool:
        return self.tier in ACTIONABLE

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("text")
        return d


@dataclass
class Review:
    id: int
    commit: str
    submitted_at: str
    config_used: str | None
    profile: str | None
    actionable_posted: int | None
    findings: list[Finding] = field(default_factory=list)

    @property
    def config_source(self) -> str:
        """'repo' when the repo's .coderabbit.yaml was used, else what CodeRabbit fell back to."""
        c = (self.config_used or "").lower()
        if c.startswith("path:") and ".coderabbit.y" in c:
            return "repo"
        if "central" in c:
            return "central"
        if "organization" in c or "ui" in c.split():
            return "org-ui"
        return "unknown" if not c else "other"


def strip_details(text: str) -> str:
    """Drop HTML comments and every <details> block (prompts, analysis chains,
    reachability graphs). What is left is the finding as a reader sees it."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    out, depth = [], 0
    for tok in re.split(r"(<details>|</details>)", text):
        if tok == "<details>":
            depth += 1
        elif tok == "</details>":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(tok)
    text = "".join(out)
    text = re.sub(r"</?blockquote>", "", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def _title(text: str) -> str:
    for m in _BOLD_RE.finditer(text):
        t = m.group(1).strip()
        if t.endswith(":"):  # "**Injection (CWE-89):**", "**Exploitability:**"
            continue
        return t
    first = text.strip().splitlines()
    return first[0][:100] if first else ""


def _make(path, start, end, tier, text, review_id) -> Finding:
    sev = _SEVERITY_RE.search(text)
    cat = _CATEGORY_RE.search(text)
    return Finding(
        path=path,
        start=start,
        end=end,
        tier=tier,
        text=text,
        title=_title(text),
        severity=sev.group(1) if sev else None,
        category=cat.group(1).strip() if cat else None,
        review_id=review_id,
    )


def split_findings(stripped: str) -> list[str]:
    """One inline comment can carry several findings separated by '---'."""
    parts = [p.strip() for p in re.split(r"\n\s*---\s*\n", stripped)]
    parts = [p for p in parts if p]
    with_header = [p for p in parts if _SEVERITY_RE.search(p) or _CATEGORY_RE.search(p)]
    return with_header or ([stripped] if stripped else [])


def inline_findings(comments: list[dict], bot: str = BOT_LOGIN) -> list[Finding]:
    found = []
    for c in comments:
        if c.get("user", {}).get("login") != bot or c.get("in_reply_to_id"):
            continue
        end = c.get("line") or c.get("original_line")
        start = c.get("start_line") or c.get("original_start_line") or end
        if end is None:
            continue
        for part in split_findings(strip_details(c.get("body", ""))):
            found.append(_make(c["path"], int(start), int(end), "inline", part, c.get("pull_request_review_id")))
    return found


def body_findings(body: str, review_id: int | None = None) -> list[Finding]:
    """Parse the Duplicate / Outside diff range / Nitpick / Additional sections."""
    found: list[Finding] = []
    tier = path = None
    entry: tuple[int, int] | None = None
    buf: list[str] = []

    def flush():
        nonlocal entry, buf
        if entry and tier and path:
            text = strip_details("\n".join(buf))
            if text and not text.upper().startswith("LGTM"):
                found.append(_make(path, entry[0], entry[1], tier, text, review_id))
        entry, buf = None, []

    for line in body.splitlines():
        sec = _SECTION_RE.search(line)
        if sec:
            flush()
            tier, path = _SECTION_TIER[sec.group(1)], None
            continue
        if _END_RE.search(line):
            flush()
            tier = path = None
            continue
        if tier is None:
            continue
        f = _FILE_RE.search(line)
        if f:
            flush()
            path = f.group(1).strip().strip("`")
            continue
        e = _ENTRY_RE.match(line.strip())
        if e and path:
            flush()
            a = int(e.group(1))
            b = int(e.group(2) or a)
            entry, buf = (a, b), [e.group(3)]
            continue
        if line.strip() == "---":
            flush()
            continue
        if entry:
            buf.append(line)
    flush()
    return found


def parse_reviews(reviews: list[dict], comments: list[dict], bot: str = BOT_LOGIN) -> list[Review]:
    """One Review per CodeRabbit review that reported anything, oldest first."""
    inline = inline_findings(comments, bot)
    out = []
    for r in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        if r.get("user", {}).get("login") != bot:
            continue
        body = r.get("body") or ""
        mine = [f for f in inline if f.review_id == r["id"]]
        mine += body_findings(body, r["id"])
        posted = _ACTIONABLE_RE.search(body)
        if not mine and not posted:
            continue  # e.g. the review object GitHub creates for a chat reply
        cfg = _CONFIG_RE.search(body)
        prof = _PROFILE_RE.search(body)
        out.append(
            Review(
                id=r["id"],
                commit=r.get("commit_id", ""),
                submitted_at=r.get("submitted_at", ""),
                config_used=cfg.group(1).strip() if cfg else None,
                profile=prof.group(1) if prof else None,
                actionable_posted=int(posted.group(1)) if posted else None,
                findings=mine,
            )
        )
    return out


def generic_findings(items: list[dict]) -> list[Finding]:
    """Findings from any other reviewer: [{path, start?, end|line, body, tier?}]."""
    out = []
    for it in items:
        end = int(it.get("end") or it.get("line"))
        start = int(it.get("start") or it.get("start_line") or end)
        text = it.get("body", "")
        out.append(_make(it["path"], start, end, it.get("tier", "inline"), text, None))
    return out
