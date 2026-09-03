#!/usr/bin/env python3
"""Opt-in network probe for the two arXiv discovery endpoints."""

from __future__ import annotations

import os
import sys
import time

import requests


USER_AGENT = os.environ.get(
    "DAILY_PAPER_BRIEF_USER_AGENT",
    "DailyPaperBrief/1.0 network smoke test (independent research tool)",
)
ENDPOINTS = (
    (
        "API",
        "https://export.arxiv.org/api/query",
        {"search_query": "cat:cs.DC", "max_results": 1},
    ),
    ("RSS", "https://rss.arxiv.org/atom/cs.DC", None),
)


def main() -> int:
    failures = []
    for index, (name, url, params) in enumerate(ENDPOINTS):
        if index:
            time.sleep(3)
        try:
            response = requests.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=(10, 30),
            )
            response.raise_for_status()
            print(f"{name}: HTTP {response.status_code}, {len(response.content)} bytes")
        except requests.RequestException as exc:
            failures.append(f"{name}: {exc}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
