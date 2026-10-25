"""Tests run against real CodeRabbit reviews saved from
coderabbitai/testing2-carrotstock PRs #1-#3 (see fixtures/), plus small
synthetic cases for the parser edge cases."""

import json
from pathlib import Path

import pytest

from seedkey.cli import load_pr, main
from seedkey.parse import body_findings, inline_findings, split_findings, strip_details
from seedkey.score import load_key, score

ROOT = Path(__file__).resolve().parents[1]
KEY = ROOT / "keys" / "carrotstock.toml"
PR1, PR2, PR3 = (ROOT / "fixtures" / f"carrotstock-pr{n}" for n in (1, 2, 3))


@pytest.fixture(scope="module")
def key():
    return load_key(KEY)


# ---- parser ----------------------------------------------------------------


def test_strip_details_removes_nested_blocks():
    raw = "head\n<details><summary>x</summary>\n<details>inner</details>\nstill hidden</details>\ntail<!-- c -->"
    assert strip_details(raw) == "head\ntail"


def test_split_findings_on_rule_between_headers():
    text = "_🩺 Stability_ | _🟡 Minor_\n**A**\n---\n_🎯 Functional Correctness_ | _🟠 Major_\n**B**"
    assert len(split_findings(text)) == 2


def test_body_sections_and_lgtm_skipped():
    body = "\n".join(
        [
            "<summary>🔇 Additional comments (2)</summary><blockquote>",
            "<summary>a.py (2)</summary><blockquote>",
            "`3-8`: LGTM!",
            "---",
            "`14-16`: _🔒 Security & Privacy_",
            "**Bind the parameter.**",
            "<summary>🤖 Prompt for all review comments with AI agents</summary>",
            "`1-2`: _x_ | _🟠 Major_ not a finding",
        ]
    )
    found = body_findings(body)
    assert [(f.path, f.start, f.end, f.tier, f.title) for f in found] == [
        ("a.py", 14, 16, "additional", "Bind the parameter.")
    ]


def test_replies_and_humans_ignored():
    comments = [
        {"user": {"login": "coderabbitai[bot]"}, "path": "a.ts", "line": 3, "body": "_🎯 X_ | _🟠 Major_\n**t**"},
        {"user": {"login": "coderabbitai[bot]"}, "path": "a.ts", "line": 3, "body": "reply", "in_reply_to_id": 1},
        {"user": {"login": "someone"}, "path": "a.ts", "line": 3, "body": "human"},
    ]
    found = inline_findings(comments)
    assert len(found) == 1 and found[0].severity == "Major" and found[0].start == 3


def test_real_pr1_has_two_scored_reviews():
    _, reviews = load_pr(PR1)
    assert [r.id for r in reviews] == [4878252667, 4878304680]
    first, full = reviews
    assert len(first.findings) == 11 == first.actionable_posted
    # The "full review" posts 2 new comments and lists 10 earlier ones as duplicates.
    tiers = [f.tier for f in full.findings]
    assert tiers.count("inline") == 2 and tiers.count("duplicate") == 10


def test_real_pr2_counts_and_config():
    _, (r,) = load_pr(PR2)
    tiers = [f.tier for f in r.findings]
    # 13 inline comments carry 14 findings (one comment holds two), as the header says.
    assert tiers.count("inline") == 14 == r.actionable_posted
    assert tiers.count("additional") == 2  # two LGTM entries are dropped
    assert r.config_source == "central"


# ---- scoring on real reviews ----------------------------------------------


def test_pr1_first_review_misses_sql_injection(key):
    _, reviews = load_pr(PR1)
    s = score(key, reviews[0].findings, reviews[0], "lesson2")
    assert s.recall() == (11, 13)
    assert s.statuses["sql-injection"] == "missed"
    assert s.statuses["swallowed-errors"] == "missed"
    assert s.statuses["oversell"] == "caught"
    assert not s.passed


def test_pr1_learning_suppresses_oversell(key):
    _, reviews = load_pr(PR1)
    s = score(key, reviews[-1].findings, reviews[-1], "after-learning")
    assert s.statuses["oversell"] == "missed"
    assert s.statuses["sql-injection"] == "caught"
    assert s.passed


def test_pr3_same_commit_full_recall_but_reflags_oversell(key):
    _, (r,) = load_pr(PR3)
    s = score(key, r.findings, r, "after-learning")
    assert r.commit.startswith("9a84b80")
    assert s.recall() == (13, 13)
    assert s.statuses["oversell"] == "caught"
    assert not s.passed


def test_pr2_lesson4_fails_on_broken_config(key):
    _, (r,) = load_pr(PR2)
    s = score(key, r.findings, r, "lesson4")
    assert s.statuses["xss"] == "noted" and s.statuses["sql-injection"] == "noted"
    assert all(s.statuses[i] == "missed" for i in ("conv-any", "conv-return-orders", "conv-non-null"))
    failed = [d for ok, d in s.checks if not ok]
    assert any(d.startswith("config source is 'central'") for d in failed)
    assert [f.path for f in s.unkeyed] == [".coderabbit.yaml"]


def test_exclude_keeps_learning_aware_comment_out(key):
    # PR1's full review says "Keep the intentional on-hand-stock validation."
    _, reviews = load_pr(PR1)
    new = [f for f in reviews[-1].findings if f.path == "src/orders.ts" and f.tier == "inline"]
    assert new and "intentional" in new[0].text
    assert not key.get("oversell").matches(new[0], key.slop)


# ---- CLI -------------------------------------------------------------------


def test_cli_score_exit_codes(capsys):
    assert main(["score", "--key", str(KEY), "--pr", str(PR3)]) == 0
    assert main(["score", "--key", str(KEY), "--pr", str(PR3), "--profile", "after-learning"]) == 1
    assert main(["score", "--key", str(KEY), "--pr", str(PR1), "--min-recall", "0.9"]) == 1
    out = capsys.readouterr().out
    assert "seeded bugs caught (actionable): 13/13" in out


def test_cli_json(capsys):
    main(["score", "--key", str(KEY), "--pr", str(PR2), "--json"])
    (d,) = json.loads(capsys.readouterr().out)
    assert d["config_source"] == "central" and d["seeded_bug_recall"] == {"caught": 11, "total": 13}


def test_cli_diff_same_commit(capsys):
    rc = main(["diff", "--key", str(KEY), f"{PR1}@first", str(PR3), "--fail-on-variance"])
    out = capsys.readouterr().out
    assert rc == 1 and "same commit: yes" in out and "sql-injection" in out


def test_generic_findings_input(tmp_path, capsys):
    f = tmp_path / "other.json"
    f.write_text(json.dumps([{"path": "services/reorder_report.py", "line": 15, "body": "SQL injection via f-string"}]))
    main(["score", "--key", str(KEY), "--findings", str(f)])
    out = capsys.readouterr().out
    assert "[x] sql-injection" in out and "1/13" in out


def test_key_rejects_unknown_profile_ref(tmp_path):
    bad = tmp_path / "k.toml"
    bad.write_text(
        '[[issue]]\nid="a"\nkind="bug"\npath="x"\nlines=1\nmatch="y"\n[profiles.p]\nmust_flag=["nope"]\n'
    )
    with pytest.raises(ValueError, match="unknown issue nope"):
        load_key(bad)
