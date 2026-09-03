#!/usr/bin/env python3
"""Deterministic offline security gate for the public repository."""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = (
    ("macOS home path", re.compile("/" + "Users" + r"/[^/{\s]+/")),
    ("Feishu chat id", re.compile(r"\boc_[0-9a-f]{24,}\b")),
    ("Feishu open id", re.compile(r"\bou_[0-9a-f]{24,}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}\b")),
    ("Google API key", re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")),
    ("private key", re.compile(r"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY")),
    (
        "literal app secret",
        re.compile(r'(?i)(?:app[_-]?secret|api[_-]?key|password)\s*[=:]\s*["\'](?!YOUR_|<|\{\{)[^"\'\n]{8,}["\']'),
    ),
    ("credential in URL", re.compile(r"https?://[^/\s:@]+:[^/\s@]+@")),
)
FORBIDDEN_TOOL_NAMES = {"browser", "cron", "gateway", "message", "nodes"}


def git_paths(arguments: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def candidate_files() -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z"],
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        return sorted(
            ROOT / raw.decode("utf-8")
            for raw in result.stdout.split(b"\0")
            if raw and (ROOT / raw.decode("utf-8")).is_file()
        )
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and ".git" not in path.parts and not path.name.startswith("._")
    )


def scan_text(label: str, text: str, findings: list[dict[str, str]]) -> None:
    for kind, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append({"severity": "high", "kind": kind, "location": label})


def scan_python(path: Path, findings: list[dict[str, str]]) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        findings.append(
            {"severity": "high", "kind": "unparseable Python", "location": f"{path.relative_to(ROOT)}: {exc}"}
        )
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
                findings.append(
                    {"severity": "high", "kind": f"dynamic {node.func.id}", "location": f"{path.relative_to(ROOT)}:{node.lineno}"}
                )
            for keyword in node.keywords:
                if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                    findings.append(
                        {"severity": "high", "kind": "subprocess shell=True", "location": f"{path.relative_to(ROOT)}:{node.lineno}"}
                    )
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules: Iterable[str]
            if isinstance(node, ast.Import):
                modules = (alias.name for alias in node.names)
            else:
                modules = (node.module or "",)
            if any(module.split(".", 1)[0] == "pickle" for module in modules):
                findings.append(
                    {"severity": "high", "kind": "pickle import", "location": f"{path.relative_to(ROOT)}:{node.lineno}"}
                )


def scan_history(findings: list[dict[str, str]]) -> int:
    commits = git_paths(["rev-list", "--all"])
    for commit in commits:
        names = git_paths(["ls-tree", "-r", "--name-only", commit])
        if any(name.startswith("skills/academic-writing-refiner/") for name in names):
            findings.append(
                {"severity": "high", "kind": "unlicensed skill in Git history", "location": commit[:12]}
            )
        for name in names:
            result = subprocess.run(
                ["git", "-C", str(ROOT), "show", f"{commit}:{name}"],
                check=False,
                capture_output=True,
            )
            if result.returncode != 0 or b"\0" in result.stdout:
                continue
            scan_text(
                f"{commit[:12]}:{name}",
                result.stdout.decode("utf-8", "replace"),
                findings,
            )
    return len(commits)


def version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(".") if part.isdigit())


def main() -> int:
    findings: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    files = candidate_files()
    for path in files:
        relative = str(path.relative_to(ROOT))
        try:
            data = path.read_bytes()
        except OSError as exc:
            findings.append({"severity": "high", "kind": "unreadable file", "location": f"{relative}: {exc}"})
            continue
        if b"\0" not in data:
            scan_text(relative, data.decode("utf-8", "replace"), findings)
        if path.suffix == ".py":
            scan_python(path, findings)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o022:
            findings.append({"severity": "high", "kind": "group/world-writable file", "location": relative})

    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    pillow = re.search(r"(?im)^Pillow==([0-9.]+)$", requirements)
    if not pillow or version_tuple(pillow.group(1)) < (12, 3, 0):
        findings.append({"severity": "high", "kind": "unsafe Pillow baseline", "location": "requirements.txt"})
    lock_path = ROOT / "requirements.lock"
    if not lock_path.is_file():
        findings.append({"severity": "high", "kind": "missing dependency lock", "location": "requirements.lock"})
    else:
        lock = lock_path.read_text(encoding="utf-8")
        if "Pillow==12.3.0" not in lock or "--hash=sha256:" not in lock:
            findings.append({"severity": "high", "kind": "incomplete dependency lock", "location": "requirements.lock"})

    job = json.loads((ROOT / "cron/arxiv-daily-review.job.template.json").read_text(encoding="utf-8"))
    allowed = set(((job.get("payload") or {}).get("toolsAllow") or []))
    forbidden = sorted(allowed & FORBIDDEN_TOOL_NAMES)
    if forbidden:
        findings.append(
            {"severity": "high", "kind": "privileged cron tools allowed", "location": ", ".join(forbidden)}
        )
    if "exec" in allowed:
        warnings.append(
            {
                "severity": "medium",
                "kind": "review agent retains exec",
                "location": "cron/arxiv-daily-review.job.template.json; use only in a trusted single-user environment",
            }
        )

    if (ROOT / "skills/academic-writing-refiner").exists():
        findings.append(
            {"severity": "high", "kind": "unlicensed bundled skill", "location": "skills/academic-writing-refiner"}
        )
    for required in (
        ROOT / "LICENSE",
        ROOT / "NOTICE",
        ROOT / "skills/academic-style-baseline/SKILL.md",
        ROOT / "skills/humanizer-zh/LICENSE",
    ):
        if not required.is_file():
            findings.append({"severity": "high", "kind": "missing release file", "location": str(required.relative_to(ROOT))})

    history_commits = scan_history(findings)
    authors = git_paths(["log", "--all", "--format=%ae"])
    public_emails = sorted({email for email in authors if email and not email.endswith("@users.noreply.github.com")})
    if public_emails:
        warnings.append(
            {"severity": "low", "kind": "public Git author email", "location": ", ".join(public_emails)}
        )

    result = {
        "status": "FAIL" if findings else "PASS",
        "files_scanned": len(files),
        "history_commits_scanned": history_commits,
        "findings": findings,
        "warnings": warnings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
