#!/usr/bin/env python3
"""Fail closed on missing bundle files, private state, or obvious credentials."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    ".github/workflows/ci.yml",
    ".github/dependabot.yml",
    "LICENSE",
    "NOTICE",
    "README.md",
    "README.zh-CN.md",
    "src/arxiv_monitor_phd.py",
    "config/arxiv-monitor-config-phd.example.json",
    "src/arxiv_report_pipeline.py",
    "src/ai_writing_metrics.py",
    "src/arxiv_send_html_to_feishu.py",
    "requirements.lock",
    "cron/arxiv_cron_prompt.template.md",
    "cron/arxiv_review_policy.template.md",
    "cron/arxiv-daily-review.job.template.json",
    "docs/NAME_DECISION.md",
    "docs/OPEN_SOURCE_RISK_ASSESSMENT.md",
    "docs/PROVENANCE.md",
    "scripts/check_open_source_readiness.py",
    "scripts/audit_dependencies.py",
    "scripts/security_audit.py",
    "skills/humanizer-zh/SKILL.md",
    "skills/humanizer-zh/LICENSE",
    "skills/academic-style-baseline/SKILL.md",
)
FORBIDDEN_NAMES = {
    "openclaw.json",
    "arxiv-monitor-config-phd.json",
    "arxiv_pushed_ids.json",
    "papers_to_expand.json",
    "sent.json",
}
SECRET_PATTERNS = (
    ("macOS user absolute path", re.compile("/" + "Users" + r"/[^/{]+/")),
    ("Feishu concrete chat id", re.compile(r"\boc_[0-9a-f]{24,}\b")),
    ("OpenAI-style secret", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}\b")),
    ("private key", re.compile(r"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY")),
    (
        "literal app secret",
        re.compile(r'"appSecret"\s*:\s*"(?!YOUR_|<|\{\{)[^"\n]{8,}"'),
    ),
)
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def candidate_files() -> list[Path]:
    if (ROOT / ".git").is_dir():
        result = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z"],
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            files = [
                ROOT / value.decode("utf-8")
                for value in result.stdout.split(b"\0")
                if value
            ]
            return sorted(path for path in files if path.is_file())
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if ".git" in relative.parts or "runtime" in relative.parts:
            continue
        if path.name.startswith("._") or path.suffix in {".pyc"}:
            continue
        files.append(path)
    return sorted(files)


def main() -> int:
    errors: list[str] = []
    for relative in REQUIRED:
        if not (ROOT / relative).is_file():
            errors.append(f"missing required file: {relative}")

    for path in candidate_files():
        relative = path.relative_to(ROOT)
        if path.name in FORBIDDEN_NAMES and "examples" not in relative.parts:
            errors.append(f"private runtime state must not be tracked: {relative}")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"{label} found in {relative}")

    for relative in (
        "config/arxiv-monitor-config-phd.example.json",
        "cron/arxiv-daily-review.job.template.json",
        "examples/arxiv_pushed_ids.example.json",
        "examples/papers_to_expand.example.json",
        "examples/sent.example.json",
    ):
        try:
            json.loads((ROOT / relative).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid JSON in {relative}: {exc}")

    prompt = (ROOT / "cron/arxiv_cron_prompt.template.md").read_text(encoding="utf-8")
    policy = (ROOT / "cron/arxiv_review_policy.template.md").read_text(encoding="utf-8")
    for placeholder in ("{{WORKSPACE_ROOT}}", "{{RUNTIME_ROOT}}", "{{FEISHU_TARGET_FILE}}"):
        if placeholder not in prompt:
            errors.append(f"cron prompt is missing placeholder {placeholder}")
    if "{{WORKSPACE_ROOT}}" not in policy:
        errors.append("review policy is missing {{WORKSPACE_ROOT}}")
    if "{{FEISHU_CHAT_ID}}" in prompt:
        errors.append("cron prompt must not embed a concrete delivery destination")
    if (ROOT / "skills/academic-writing-refiner").exists():
        errors.append("unlicensed academic-writing-refiner must not be bundled")

    baseline = (ROOT / "skills/academic-style-baseline/SKILL.md").read_text(encoding="utf-8")
    if not baseline.startswith("---\nname: academic-style-baseline\n"):
        errors.append("academic-style-baseline has invalid frontmatter")
    if "license: \"Apache-2.0\"" not in baseline:
        errors.append("academic-style-baseline is missing its Apache-2.0 declaration")

    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    action_refs = re.findall(r"uses:\s*[^@\s]+@([^\s#]+)", workflow)
    if not action_refs or any(not re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs):
        errors.append("GitHub Actions must be pinned to full commit SHAs")

    for document in (ROOT / "README.md", ROOT / "README.zh-CN.md"):
        text = document.read_text(encoding="utf-8")
        for target in MARKDOWN_LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            local_target = target.split("#", 1)[0]
            if local_target and not (document.parent / local_target).exists():
                errors.append(f"broken local link in {document.name}: {target}")

    if errors:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 1
    print(
        json.dumps(
            {"ok": True, "files_scanned": len(candidate_files()), "required_files": len(REQUIRED)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
