#!/usr/bin/env python3
"""Report deterministic public-release blockers. A blocked result exits 1."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def tracked_files() -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def main() -> int:
    tracked = tracked_files()
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    owner_license = next(
        (
            path
            for path in (ROOT / "LICENSE", ROOT / "LICENSE.md", ROOT / "LICENSE.txt")
            if path.is_file()
        ),
        None,
    )
    if owner_license is None:
        blockers.append(
            {
                "id": "missing_repository_license",
                "detail": "No owner-selected repository-wide license is present.",
            }
        )

    elif "Apache License\n                           Version 2.0" not in owner_license.read_text(encoding="utf-8"):
        blockers.append(
            {
                "id": "unexpected_repository_license",
                "detail": "The repository license is not the selected Apache-2.0 text.",
            }
        )

    if (ROOT / "skills/academic-writing-refiner").exists():
        blockers.append(
            {
                "id": "unlicensed_academic_writing_refiner",
                "detail": "The unlicensed academic-writing-refiner directory must not be published.",
            }
        )
    if not (ROOT / "skills/academic-style-baseline/SKILL.md").is_file():
        blockers.append(
            {
                "id": "missing_academic_style_baseline",
                "detail": "The Apache-2.0 academic-style baseline is missing.",
            }
        )
    if not (ROOT / "skills/humanizer-zh/LICENSE").is_file():
        blockers.append(
            {
                "id": "missing_humanizer_license",
                "detail": "The separately licensed humanizer-zh snapshot lacks its MIT license.",
            }
        )

    forbidden_tracked = sorted(
        path
        for path in tracked
        if Path(path).name
        in {
            "openclaw.json",
            "arxiv-monitor-config-phd.json",
            "arxiv_pushed_ids.json",
            "papers_to_expand.json",
            "sent.json",
        }
        and not path.startswith("examples/")
    )
    if forbidden_tracked:
        blockers.append(
            {
                "id": "private_runtime_files_tracked",
                "detail": ", ".join(forbidden_tracked),
            }
        )

    generated_suffixes = {".pdf", ".html"}
    generated_tracked = sorted(
        path
        for path in tracked
        if Path(path).suffix.lower() in generated_suffixes
        and not path.startswith("docs/")
    )
    if generated_tracked:
        warnings.append(
            {
                "id": "generated_artifacts_require_rights_review",
                "detail": ", ".join(generated_tracked),
            }
        )

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if not readme.startswith("# Daily Paper Brief\n"):
        blockers.append(
            {
                "id": "project_brand_mismatch",
                "detail": "README must use the source-neutral Daily Paper Brief project name.",
            }
        )
    acknowledgement = "Thank you to arXiv for use of its open access interoperability."
    if acknowledgement not in readme:
        blockers.append(
            {
                "id": "missing_arxiv_acknowledgement",
                "detail": "README is missing the requested source acknowledgement.",
            }
        )
    if "not affiliated with, endorsed by, or operated by arXiv" not in readme:
        blockers.append(
            {
                "id": "missing_independence_disclaimer",
                "detail": "README is missing the independent-project disclaimer.",
            }
        )

    security_audit = subprocess.run(
        [sys.executable, str(ROOT / "scripts/security_audit.py")],
        check=False,
        capture_output=True,
        text=True,
    )
    if security_audit.returncode != 0:
        blockers.append(
            {
                "id": "security_audit_failed",
                "detail": security_audit.stdout.strip() or security_audit.stderr.strip(),
            }
        )
    else:
        try:
            security_payload = json.loads(security_audit.stdout)
            warnings.extend(security_payload.get("warnings") or [])
        except json.JSONDecodeError:
            blockers.append(
                {
                    "id": "security_audit_invalid_output",
                    "detail": "security audit returned invalid JSON",
                }
            )

    result = {
        "status": "BLOCKED" if blockers else "READY_FOR_OWNER_REVIEW",
        "blockers": blockers,
        "warnings": warnings,
        "note": "This deterministic gate is not legal advice or trademark clearance.",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
