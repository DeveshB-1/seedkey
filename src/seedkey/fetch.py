"""Download a PR's CodeRabbit review data from the GitHub REST API.

Public repos need no token. Set GITHUB_TOKEN to raise the rate limit or to
read a private repo.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

API = "https://api.github.com"


def _get(url: str) -> object:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "seedkey"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _all(url: str) -> list:
    out, page = [], 1
    while True:
        batch = _get(f"{url}?per_page=100&page={page}")
        out.extend(batch)
        if len(batch) < 100:
            return out
        page += 1


def fetch_pr(repo: str, number: int, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = f"{API}/repos/{repo}/pulls/{number}"
    pr = _get(base)
    files = {
        "pr.json": {k: pr[k] for k in ("number", "title", "html_url", "created_at")}
        | {"head": pr["head"]["sha"], "head_ref": pr["head"]["ref"], "base": pr["base"]["sha"]},
        "reviews.json": _all(f"{base}/reviews"),
        "comments.json": _all(f"{base}/comments"),
    }
    for name, data in files.items():
        (out / name).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    return out
