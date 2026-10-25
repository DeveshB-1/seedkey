"""seedkey: score AI code review output against an answer key of seeded bugs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .parse import Review, generic_findings, parse_reviews
from .score import Key, Score, diff, load_key, score

MARK = {"caught": "[x]", "nitpick": "[n]", "noted": "[~]", "missed": "[ ]"}


def load_pr(pr_dir: str | Path) -> tuple[dict, list[Review]]:
    d = Path(pr_dir)
    meta = json.loads((d / "pr.json").read_text()) if (d / "pr.json").exists() else {}
    reviews = parse_reviews(json.loads((d / "reviews.json").read_text()), json.loads((d / "comments.json").read_text()))
    return meta, reviews


def pick(reviews: list[Review], which: str) -> Review:
    if not reviews:
        raise SystemExit("no CodeRabbit reviews with findings in this PR")
    if which == "first":
        return reviews[0]
    if which == "last":
        return reviews[-1]
    for r in reviews:
        if str(r.id) == which:
            return r
    raise SystemExit(f"review {which} not found; have {[r.id for r in reviews]}")


def pct(a: int, b: int) -> str:
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "0/0"


def render(s: Score, key: Key, label: str = "") -> str:
    r = s.review
    lines = []
    if r:
        lines.append(f"{label}review {r.id} on {r.commit[:7]} at {r.submitted_at}")
        lines.append(f"  config: {r.config_used}  (source: {r.config_source}), profile: {r.profile}")
    c, n = s.recall()
    noted = sum(s.statuses[i.id] == "noted" for i in key.issues if i.kind == "bug" and i.seeded)
    lines.append(f"  seeded bugs caught (actionable): {pct(c, n)}" + (f", {noted} only in 'Additional comments'" if noted else ""))
    ic, inn = s.recall("bug", seeded=False)
    if inn:
        lines.append(f"  incidental issues caught: {pct(ic, inn)}")
    for kind in ("bug", "intentional", "convention", "standard"):
        group = [i for i in key.issues if i.kind == kind]
        if not group:
            continue
        lines.append(f"  {kind}:")
        for i in group:
            st = s.statuses[i.id]
            where = f"{i.path}:{i.lines[0]}" + (f"-{i.lines[1]}" if i.lines[1] != i.lines[0] else "")
            tiers = ",".join(sorted({f.tier for f in s.hits[i.id]}))
            extra = "" if i.seeded else " (incidental)"
            lines.append(f"    {MARK[st]} {i.id:<22} {where:<34} {st}{' via ' + tiers if tiers else ''}{extra}")
    kp = s.keyed_precision
    lines.append(
        f"  findings: {len(s.findings)}, matched the key: {len(s.findings) - len(s.unkeyed)}"
        + (f" (keyed precision, lower bound: {100 * kp:.0f}%)" if kp is not None else "")
    )
    for f in s.unkeyed:
        lines.append(f"    unkeyed: {f.path}:{f.start}-{f.end} [{f.tier}] {f.title}")
    if s.checks:
        lines.append(f"  profile checks: {'PASS' if s.passed else 'FAIL'}")
        for ok, detail in s.checks:
            lines.append(f"    {'ok  ' if ok else 'FAIL'} {detail}")
    return "\n".join(lines)


def cmd_score(a) -> int:
    key = load_key(a.key)
    if a.findings:
        findings = generic_findings(json.loads(Path(a.findings).read_text()))
        results = [score(key, findings, None, a.profile)]
    else:
        _, reviews = load_pr(a.pr)
        chosen = reviews if a.review == "all" else [pick(reviews, a.review)]
        results = [score(key, r.findings, r, a.profile) for r in chosen]
    if a.json:
        print(json.dumps([s.to_dict() for s in results], indent=1))
    else:
        print("\n\n".join(render(s, key) for s in results))
    failed = any(not s.passed for s in results)
    low = a.min_recall is not None and any(
        (s.recall()[0] / s.recall()[1] if s.recall()[1] else 0) < a.min_recall for s in results
    )
    if low and not a.json:
        print(f"\nseeded recall below --min-recall {a.min_recall}")
    return 1 if failed or low else 0


def cmd_diff(a) -> int:
    key = load_key(a.key)
    runs = []
    for spec in (a.a, a.b):
        pr_dir, _, which = spec.partition("@")
        _, reviews = load_pr(pr_dir)
        r = pick(reviews, which or "first")
        runs.append(score(key, r.findings, r))
    sa, sb = runs
    same = sa.review.commit == sb.review.commit
    print(f"A: review {sa.review.id} on {sa.review.commit[:7]}  seeded {pct(*sa.recall())}")
    print(f"B: review {sb.review.id} on {sb.review.commit[:7]}  seeded {pct(*sb.recall())}")
    print("same commit: yes, so the code under review was identical" if same else "same commit: no")
    changes = diff(sa, sb)
    for iid, x, y in changes:
        print(f"  {iid:<22} {x:>8} -> {y}")
    if not changes:
        print("  no differences")
    return 1 if (changes and same and a.fail_on_variance) else 0


def cmd_fetch(a) -> int:
    from .fetch import fetch_pr

    out = fetch_pr(a.repo, a.number, a.out)
    _, reviews = load_pr(out)
    print(f"saved to {out}: {len(reviews)} CodeRabbit review(s) with findings")
    for r in reviews:
        print(f"  review {r.id} on {r.commit[:7]}: {len(r.findings)} findings, config: {r.config_used}")
    return 0


def cmd_list(a) -> int:
    _, reviews = load_pr(a.pr)
    for r in reviews:
        tiers = {}
        for f in r.findings:
            tiers[f.tier] = tiers.get(f.tier, 0) + 1
        print(f"review {r.id} on {r.commit[:7]} at {r.submitted_at}: {tiers}")
        for f in r.findings:
            print(f"  {f.path}:{f.start}-{f.end} [{f.tier}/{f.severity}] {f.title}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="seedkey", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download a PR's CodeRabbit reviews from GitHub")
    f.add_argument("repo", help="owner/name")
    f.add_argument("number", type=int)
    f.add_argument("--out", required=True)
    f.set_defaults(fn=cmd_fetch)

    l = sub.add_parser("list", help="print every finding parsed from a PR")
    l.add_argument("pr")
    l.set_defaults(fn=cmd_list)

    s = sub.add_parser("score", help="score a review against an answer key")
    s.add_argument("--key", required=True)
    src = s.add_mutually_exclusive_group(required=True)
    src.add_argument("--pr", help="directory written by 'seedkey fetch'")
    src.add_argument("--findings", help="JSON list of {path, start, end, body} from any reviewer")
    s.add_argument("--review", default="first", help="first | last | all | <review id>")
    s.add_argument("--profile", help="profile from the key to check (exit 1 on failure)")
    s.add_argument("--min-recall", type=float, help="exit 1 if seeded bug recall is below this (0-1)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_score)

    d = sub.add_parser("diff", help="compare two reviews (DIR or DIR@first|last|<id>)")
    d.add_argument("--key", required=True)
    d.add_argument("a")
    d.add_argument("b")
    d.add_argument("--fail-on-variance", action="store_true", help="exit 1 if the same commit scored differently")
    d.set_defaults(fn=cmd_diff)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
