#!/usr/bin/env python3
"""Query OSV for the exact Python versions recorded in requirements.lock."""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)\s*\\?$", re.MULTILINE)


def locked_packages(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    packages = [
        {"name": name, "version": version}
        for name, version in PACKAGE_RE.findall(text)
    ]
    if not packages:
        raise ValueError(f"no locked packages found in {path}")
    return packages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=ROOT / "requirements.lock")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    try:
        packages = locked_packages(args.lock.resolve(strict=True))
        query = {
            "queries": [
                {
                    "package": {"ecosystem": "PyPI", "name": item["name"]},
                    "version": item["version"],
                }
                for item in packages
            ]
        }
        request = urllib.request.Request(
            "https://api.osv.dev/v1/querybatch",
            data=json.dumps(query).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "DailyPaperBrief-dependency-audit/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            result = json.loads(response.read())
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    vulnerabilities = []
    for package, response in zip(packages, result.get("results") or []):
        identifiers = sorted({item.get("id") for item in (response.get("vulns") or []) if item.get("id")})
        if identifiers:
            vulnerabilities.append({"package": package, "ids": identifiers})
    payload = {
        "status": "FAIL" if vulnerabilities else "PASS",
        "database": "https://osv.dev/",
        "packages_checked": len(packages),
        "vulnerabilities": vulnerabilities,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if vulnerabilities else 0


if __name__ == "__main__":
    raise SystemExit(main())
