# seedkey

Score AI code review output against an answer key of seeded bugs. It reads CodeRabbit's real review output from the GitHub API (inline comments plus the Duplicate, Outside diff range, Nitpick and Additional sections of the review body), matches each finding to a known issue, and reports recall, unmatched findings, which config the review actually ran with, and whether a lesson's expected behavior happened.

It ships with an answer key for [CarrotStock](https://github.com/coderabbitai/testing2-carrotstock), the bug-seeded repo behind the CodeRabbit Certification course, and with the three real CodeRabbit reviews on that repo saved as fixtures.

## Why this is useful to CodeRabbit

CodeRabbit's own guide to evaluating review tools says recall is the metric a pilot can't measure, because "exhaustive counterfactual labeling is often impossible." A seeded repo fixes that: you know every bug in advance, so a missed bug is visible.

CarrotStock already is that repo, and the course depends on reviews behaving a certain way: lesson 2 needs the security issues to show up, lesson 3 needs the on-hand stock comment to appear and then disappear after a learning, lesson 4 needs the `**/*.ts` path instruction to take effect. Nothing checks that today. A model, prompt or config change can quietly break a lesson. `seedkey score --profile lesson4` turns each lesson into a check that exits 1 when it doesn't hold.

## What it found on the real CarrotStock reviews

All numbers below come from the public reviews on coderabbitai/testing2-carrotstock PRs #1 to #3 (August 2026), run through this tool.

1. **Same commit, different results.** PR #1 and PR #3 review the same commit (`9a84b80`). PR #1's first review caught 11 of 13 seeded bugs and missed the SQL injection in `services/reorder_report.py` and the swallowed `KeyError`s. PR #3 caught all 13. The SQL injection only showed up in PR #1 after `@coderabbitai full review`.
2. **branch-b's `.coderabbit.yaml` is invalid YAML,** so the lesson 4 setup can't work as committed. It looks like slide text pasted in (lines such as `← New section under reviews:` and lost indentation). CodeRabbit ignored it on PR #2 and ran with `Central YAML (base), Organization UI (inherited)`; none of the 4 convention issues the lesson expects (`any`, missing return types, non-null `!`) were flagged. The walkthrough calls branch-b "a clean copy of branch-a", but it differs in this file. Repro: `git show origin/branch-b:.coderabbit.yaml | python -c "import sys,yaml; yaml.safe_load(sys.stdin)"` fails with `could not find expected ':'`.
3. **The lesson 3 learning held inside PR #1 but not in PR #3.** The learning was recorded at 21:25 UTC. PR #1's full review afterwards respected it ("Keep the intentional on-hand-stock validation"). PR #3, reviewed 17 minutes later on identical code with the repo config, flagged it again: "Line 12 ignores `product.reserved`." I can't see how learnings are scoped from outside, so this may be expected behavior, but the lesson text implies it carries over to later reviews.
4. **On PR #2 the XSS and SQL injection were demoted** to "Additional comments" (shown in the review body, not posted as actionable comments) because reachability was unresolved. seedkey reports these as `noted`, separate from `caught`.

## Quickstart

```bash
uv sync
uv run pytest -q

# score a saved review against the CarrotStock key
uv run seedkey score --key keys/carrotstock.toml --pr fixtures/carrotstock-pr1 --profile lesson2

# fetch any PR's CodeRabbit reviews (public repos need no token; GITHUB_TOKEN raises the rate limit)
uv run seedkey fetch coderabbitai/testing2-carrotstock 3 --out out/pr3

# compare two reviews; DIR@first, DIR@last or DIR@<review id>
uv run seedkey diff --key keys/carrotstock.toml fixtures/carrotstock-pr1@first fixtures/carrotstock-pr3

# every finding the parser pulled out of a PR
uv run seedkey list fixtures/carrotstock-pr2
```

Exit codes: `score` exits 1 when a `--profile` check fails or recall is below `--min-recall`; `diff --fail-on-variance` exits 1 when the same commit scored differently. Both are meant for CI.

Other reviewers: pass `--findings file.json` with `[{"path", "start", "end", "body"}]` to score any tool's output against the same key.

## The answer key

`keys/carrotstock.toml` lists 23 issues on branch-a:

| kind | count | expected |
|---|---|---|
| `bug`, seeded | 13 | in every default review |
| `bug`, incidental | 4 | real issues that look unplanted; reported separately, not in recall |
| `intentional` | 1 | flagged in lesson 2, suppressed after the lesson 3 learning |
| `convention` | 4 | only with the lesson 4 path instruction |
| `standard` | 1 | only with CodeRabbit.md as a knowledge base (lesson 5) |

A finding matches an issue when it is in the same file, within 3 lines, and its text matches the issue's regex. One CodeRabbit comment often covers several issues ("Remove the billing credential from logs and source code"), so an issue can also carry a narrower `mention` regex that matches anywhere in the file. `exclude` handles the learning-aware case: a comment that says the on-hand check is intentional doesn't count as flagging it.

Profiles (`lesson2`, `after-learning`, `lesson4`, `lesson5`) list what must and must not be flagged, and which config source the review must report.

**Limits, plainly:** the repo doesn't publish its seeded bug list, so I wrote this key from the code and WALKTHROUGH.md, and I wrote it after reading CodeRabbit's reviews. "Keyed precision" (share of findings that hit the key) is therefore optimistic; an unkeyed finding could still be a real issue. The parser targets CodeRabbit's current markdown format and will need updates if that changes. The fixtures are public GitHub API responses; no CodeRabbit account or key was used.

## Sample run

```
$ seedkey score --key keys/carrotstock.toml --pr fixtures/carrotstock-pr1 --profile lesson2
review 4878252667 on 9a84b80 at 2026-08-06T21:18:27Z
  config: Path: .coderabbit.yaml  (source: repo), profile: CHILL
  seeded bugs caught (actionable): 11/13 (85%)
  incidental issues caught: 1/4 (25%)
  bug:
    [x] tenant-scope           src/inventory.ts:14-19             caught via inline
    [x] reorder-boundary       src/inventory.ts:28-30             caught via inline
    [x] discount-crash         src/orders.ts:39-41                caught via inline
    [x] float-money            src/pricing.ts:29-40               caught via inline
    [x] empty-list-crash       web/InventoryDashboard.tsx:21      caught via inline
    [x] xss                    web/InventoryDashboard.tsx:33      caught via inline
    [x] state-mutation         web/OrderForm.tsx:14-17            caught via inline
    [x] no-prevent-default     web/OrderForm.tsx:20               caught via inline
    [x] mutable-default        services/reorder_report.py:11      caught via inline
    [ ] sql-injection          services/reorder_report.py:14-16   missed
    [ ] swallowed-errors       services/reorder_report.py:6-25    missed
    [x] hardcoded-secret       services/pricing_sync.py:7         caught via inline
    [x] secret-in-logs         services/pricing_sync.py:14-16     caught via inline
    [ ] stale-fetch            web/InventoryDashboard.tsx:14-16   missed (incidental)
    [x] bad-quantity           src/orders.ts:10-14                caught via inline (incidental)
    [ ] index-key              web/InventoryDashboard.tsx:32      missed (incidental)
    [ ] weak-order-id          src/orders.ts:30                   missed (incidental)
  intentional:
    [x] oversell               src/orders.ts:10-15                caught via inline
  convention:
    [ ] conv-any               src/pricing.ts:35                  missed
    [ ] conv-return-orders     src/orders.ts:43                   missed
    [ ] conv-return-pricing    src/pricing.ts:43                  missed
    [ ] conv-non-null          src/inventory.ts:44                missed
  standard:
    [ ] bare-error             src/orders.ts:8                    missed
  findings: 11, matched the key: 11 (keyed precision, lower bound: 100%)
  profile checks: FAIL
    ok   config source is 'repo' (want 'repo'; review says: Path: .coderabbit.yaml)
    ok   must flag xss: caught
    FAIL must flag sql-injection: missed
    ok   must flag hardcoded-secret: caught
    ok   must flag tenant-scope: caught
    ok   must flag oversell: caught
    ok   must not flag conv-any: missed
    ok   must not flag conv-return-orders: missed
    ok   must not flag conv-return-pricing: missed
    ok   must not flag conv-non-null: missed
    ok   must not flag bare-error: missed

$ seedkey diff --key keys/carrotstock.toml fixtures/carrotstock-pr1@first fixtures/carrotstock-pr3
A: review 4878252667 on 9a84b80  seeded 11/13 (85%)
B: review 4878353619 on 9a84b80  seeded 13/13 (100%)
same commit: yes, so the code under review was identical
  sql-injection            missed -> caught
  swallowed-errors         missed -> caught
  stale-fetch              missed -> caught

$ seedkey score --key keys/carrotstock.toml --pr fixtures/carrotstock-pr2 --profile lesson4   (excerpt)
review 4878334409 on 5aff111 at 2026-08-06T21:38:03Z
  config: Central YAML (base), Organization UI (inherited)  (source: central), profile: CHILL
  seeded bugs caught (actionable): 11/13 (85%), 2 only in 'Additional comments'
    [~] xss                    web/InventoryDashboard.tsx:33      noted via additional
    [~] sql-injection          services/reorder_report.py:14-16   noted via additional
  findings: 16, matched the key: 15 (keyed precision, lower bound: 94%)
    unkeyed: .coderabbit.yaml:2-15 [inline] Fix the YAML structure before merging.
  profile checks: FAIL
    FAIL config source is 'central' (want 'repo'; review says: Central YAML (base), Organization UI (inherited))
    FAIL must flag conv-any: missed
    FAIL must flag conv-return-orders: missed
    FAIL must flag conv-return-pricing: missed
    FAIL must flag conv-non-null: missed
    FAIL must not flag oversell: caught

$ uv run pytest -q
................                                                         [100%]
16 passed in 0.07s
```

## Layout

```
src/seedkey/parse.py   CodeRabbit review JSON -> findings (inline, duplicate, outside_diff, nitpick, additional)
src/seedkey/score.py   answer key loading, matching, recall, profile checks, run diff
src/seedkey/fetch.py   GitHub REST download (stdlib only)
src/seedkey/cli.py     fetch | list | score | diff
keys/carrotstock.toml  answer key + lesson profiles
fixtures/              real reviews from coderabbitai/testing2-carrotstock PRs #1-#3
```

No runtime dependencies; Python 3.11+.

## Next steps I'd take

- A second key on a mutation-seeded copy of a real OSS repo, so recall is measured on code nobody wrote to be reviewed.
- Score the CodeRabbit CLI's local reviews with the same key, to compare CLI and PR reviews on identical code.
- Run `diff` across N re-reviews of one commit to put a number on run-to-run variance per issue.

MIT licensed.
