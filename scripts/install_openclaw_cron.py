#!/usr/bin/env python3
"""Render portable runtime files and optionally create the OpenClaw cron job."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


PLACEHOLDER_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")
SAFE_CHAT_ID_RE = re.compile(r"^oc_[A-Za-z0-9]+$")
SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")
SAFE_TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_+./-]+$")
SAFE_CRON_RE = re.compile(r"^[0-9*/?,\- ]+$")
LEGACY_JOB_NAMES = {
    "arXiv 每日论文 HTML 评审推送（AI Infra 优先）",
    "LitFacet 每日论文证据评审（AI Infra 优先）",
}


class InstallError(RuntimeError):
    pass


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def render_text(text: str, values: dict[str, str]) -> str:
    rendered = text
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    unresolved = sorted(set(PLACEHOLDER_RE.findall(rendered)))
    if unresolved:
        raise InstallError(f"unresolved template placeholders: {', '.join(unresolved)}")
    return rendered


def render_value(value: Any, values: dict[str, str]) -> Any:
    if isinstance(value, str):
        return render_text(value, values)
    if isinstance(value, list):
        return [render_value(item, values) for item in value]
    if isinstance(value, dict):
        return {key: render_value(item, values) for key, item in value.items()}
    return value


def require_safe_value(name: str, value: str, pattern: re.Pattern[str]) -> str:
    if not value or not pattern.fullmatch(value):
        raise InstallError(f"unsafe or empty {name}: {value!r}")
    return value


def require_bundle(root: Path) -> None:
    required = (
        "src/arxiv_monitor_phd.py",
        "config/arxiv-monitor-config-phd.example.json",
        "src/arxiv_report_pipeline.py",
        "src/ai_writing_metrics.py",
        "src/arxiv_send_html_to_feishu.py",
        "cron/arxiv_cron_prompt.template.md",
        "cron/arxiv_review_policy.template.md",
        "cron/arxiv-daily-review.job.template.json",
        "skills/humanizer-zh/SKILL.md",
        "skills/academic-style-baseline/SKILL.md",
    )
    missing = [relative for relative in required if not (root / relative).is_file()]
    if missing:
        raise InstallError("bundle is incomplete: " + ", ".join(missing))
    if any(character.isspace() for character in str(root)):
        raise InstallError("workspace path contains whitespace; the frozen runbook commands require a path without spaces")


def render_runtime(
    root: Path,
    runtime_root: Path,
    chat_id: str,
    model: str,
    cron_expr: str,
    timezone: str,
    agent_id: str,
) -> tuple[Path, Path, Path, Path, dict[str, Any]]:
    prompt_path = runtime_root / "arxiv_cron_prompt.md"
    policy_path = runtime_root / "arxiv_review_policy.md"
    job_path = runtime_root / "arxiv-daily-review.job.json"
    target_path = runtime_root / "feishu-target.json"
    values = {
        "WORKSPACE_ROOT": str(root),
        "RUNTIME_ROOT": str(runtime_root),
        "RUNTIME_PROMPT_PATH": str(prompt_path),
        "RUNTIME_POLICY_PATH": str(policy_path),
        "FEISHU_TARGET_FILE": str(target_path),
        "FEISHU_CHAT_ID": chat_id,
        "MODEL": model,
        "CRON_EXPR": cron_expr,
        "TIMEZONE": timezone,
        "AGENT_ID": agent_id,
    }
    prompt = render_text(
        (root / "cron/arxiv_cron_prompt.template.md").read_text(encoding="utf-8"),
        values,
    )
    policy = render_text(
        (root / "cron/arxiv_review_policy.template.md").read_text(encoding="utf-8"),
        values,
    )
    job_template = json.loads(
        (root / "cron/arxiv-daily-review.job.template.json").read_text(encoding="utf-8")
    )
    job = render_value(job_template, values)
    atomic_write(prompt_path, prompt)
    atomic_write(policy_path, policy)
    atomic_write(job_path, json.dumps(job, ensure_ascii=False, indent=2) + "\n")
    atomic_write(target_path, json.dumps({"chat_id": chat_id}, ensure_ascii=False, indent=2) + "\n")
    return prompt_path, policy_path, job_path, target_path, job


def find_openclaw(binary: str) -> str:
    if "/" in binary:
        resolved = str(Path(binary).expanduser().resolve())
        if not Path(resolved).is_file():
            raise InstallError(f"OpenClaw binary does not exist: {resolved}")
        return resolved
    resolved = shutil.which(binary)
    if not resolved:
        raise InstallError(f"OpenClaw binary not found on PATH: {binary}")
    return resolved


def refuse_duplicate(openclaw: str, job_name: str) -> None:
    result = subprocess.run(
        [openclaw, "cron", "list", "--all", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    duplicates = []
    for job in payload.get("jobs", []):
        existing_name = str(job.get("name") or "")
        message = str(((job.get("payload") or {}).get("message") or ""))
        if (
            existing_name == job_name
            or existing_name in LEGACY_JOB_NAMES
            or ("arxiv_monitor_phd.py" in message and "arxiv_report_pipeline.py" in message)
        ):
            duplicates.append(job)
    if duplicates:
        identifiers = ", ".join(str(job.get("id") or "unknown") for job in duplicates)
        raise InstallError(
            f"a cron job named {job_name!r} already exists ({identifiers}); "
            "compare and edit it explicitly instead of creating a duplicate"
        )


def cron_add_command(openclaw: str, job: dict[str, Any]) -> list[str]:
    payload = job["payload"]
    delivery = job["delivery"]
    schedule = job["schedule"]
    return [
        openclaw,
        "cron",
        "add",
        "--name",
        job["name"],
        "--description",
        job["description"],
        "--cron",
        schedule["expr"],
        "--tz",
        schedule["tz"],
        "--exact",
        "--session",
        job["sessionTarget"],
        "--wake",
        job["wakeMode"],
        "--agent",
        job["agentId"],
        "--message",
        payload["message"],
        "--model",
        payload["model"],
        "--thinking",
        payload["thinking"],
        "--timeout-seconds",
        str(payload["timeoutSeconds"]),
        "--light-context",
        "--tools",
        ",".join(payload["toolsAllow"]),
        "--announce",
        "--channel",
        delivery["channel"],
        "--to",
        delivery["to"],
        "--best-effort-deliver",
        "--json",
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = argparse.ArgumentParser(description=__doc__)
    mode = root.add_mutually_exclusive_group(required=True)
    mode.add_argument("--render-only", action="store_true", help="render ignored runtime files only")
    mode.add_argument("--apply", action="store_true", help="render files and create a new OpenClaw cron job")
    root.add_argument(
        "--acknowledge-local-agent-trust",
        action="store_true",
        help="required with --apply: confirms that OpenClaw exec is not an OS sandbox",
    )
    root.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    root.add_argument("--runtime-root", type=Path)
    root.add_argument("--chat-id", default=os.environ.get("FEISHU_CHAT_ID", ""))
    root.add_argument("--model", default=os.environ.get("ARXIV_REVIEW_MODEL", "openai/gpt-5.6-sol"))
    root.add_argument("--cron", dest="cron_expr", default=os.environ.get("ARXIV_REVIEW_CRON", "0 10 * * *"))
    root.add_argument("--timezone", default=os.environ.get("ARXIV_REVIEW_TIMEZONE", "Asia/Shanghai"))
    root.add_argument("--agent", dest="agent_id", default=os.environ.get("OPENCLAW_AGENT_ID", "main"))
    root.add_argument("--openclaw-bin", default="openclaw")
    return root.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_args(argv)
    try:
        root = args.repo_root.expanduser().resolve()
        runtime_root = (args.runtime_root or (root / "runtime")).expanduser().resolve()
        require_bundle(root)
        chat_id = require_safe_value("chat id", args.chat_id, SAFE_CHAT_ID_RE)
        model = require_safe_value("model", args.model, SAFE_MODEL_RE)
        timezone = require_safe_value("timezone", args.timezone, SAFE_TIMEZONE_RE)
        agent_id = require_safe_value("agent id", args.agent_id, SAFE_ID_RE)
        cron_expr = require_safe_value("cron expression", args.cron_expr, SAFE_CRON_RE)
        prompt_path, policy_path, job_path, target_path, job = render_runtime(
            root, runtime_root, chat_id, model, cron_expr, timezone, agent_id
        )

        result: dict[str, Any] = {
            "ok": True,
            "mode": "apply" if args.apply else "render-only",
            "job_name": job["name"],
            "model": model,
            "schedule": {"expr": cron_expr, "timezone": timezone},
            "chat_id_sha256": hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:16],
            "runtime_files": [str(prompt_path), str(policy_path), str(job_path), str(target_path)],
        }
        if args.apply:
            if not args.acknowledge_local_agent_trust:
                raise InstallError(
                    "--apply requires --acknowledge-local-agent-trust; an isolated "
                    "OpenClaw session is not an OS sandbox"
                )
            if not (root / ".venv/bin/python").is_file():
                raise InstallError("missing .venv/bin/python; run scripts/bootstrap.sh first")
            if not (root / "arxiv-monitor-config-phd.json").is_file():
                raise InstallError(
                    "missing local arxiv-monitor-config-phd.json; copy and review the example first"
                )
            openclaw = find_openclaw(args.openclaw_bin)
            refuse_duplicate(openclaw, job["name"])
            created = subprocess.run(
                cron_add_command(openclaw, job),
                check=True,
                capture_output=True,
                text=True,
            )
            created_payload = json.loads(created.stdout)
            created_job = created_payload.get("job", created_payload)
            result["created_job"] = {
                "id": created_job.get("id"),
                "name": created_job.get("name", job["name"]),
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (InstallError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
