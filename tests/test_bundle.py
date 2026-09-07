from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_openclaw_cron.py"
RELEASE_GATE = ROOT / "scripts" / "check_open_source_readiness.py"


def make_repo_copy(temporary_dir: str, with_local_config: bool = True) -> Path:
    """Copy the installer's required bundle subset into a throwaway repo root."""
    repo = Path(temporary_dir) / "repo"
    for name in ("src", "config", "cron"):
        shutil.copytree(ROOT / name, repo / name)
    for skill in ("humanizer-zh/SKILL.md", "academic-style-baseline/SKILL.md"):
        destination = repo / "skills" / skill
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "skills" / skill, destination)
    if with_local_config:
        shutil.copy2(
            ROOT / "config" / "arxiv-monitor-config-phd.example.json",
            repo / "arxiv-monitor-config-phd.json",
        )
    return repo


class BundleTests(unittest.TestCase):
    def test_direct_dependencies_match_lock(self) -> None:
        pattern = r"^([A-Za-z0-9_.-]+)==([^\s\\]+)"
        def pins(name):
            return {
                re.sub(r"[-_.]+", "-", package).lower(): version
                for package, version in re.findall(
                    pattern, (ROOT / name).read_text(encoding="utf-8"), re.MULTILINE
                )
            }
        declared = pins("requirements.txt")
        locked = pins("requirements.lock")
        self.assertTrue(declared)
        for package, version in declared.items():
            with self.subTest(package=package):
                self.assertEqual(locked.get(package), version)

    def test_templates_have_no_machine_specific_values(self) -> None:
        machine_prefix = "/" + "Users" + "/"
        for relative in (
            "cron/arxiv_cron_prompt.template.md",
            "cron/arxiv_review_policy.template.md",
            "cron/arxiv-daily-review.job.template.json",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn(machine_prefix, text)
            self.assertNotRegex(text, r"\boc_[0-9a-f]{24,}\b")
        prompt = (ROOT / "cron/arxiv_cron_prompt.template.md").read_text(encoding="utf-8")
        self.assertIn("{{WORKSPACE_ROOT}}", prompt)
        self.assertIn("{{RUNTIME_ROOT}}", prompt)
        self.assertIn("{{FEISHU_TARGET_FILE}}", prompt)
        self.assertNotIn("{{FEISHU_CHAT_ID}}", prompt)

    def test_public_config_contains_placeholders_not_credentials(self) -> None:
        config = json.loads(
            (ROOT / "config" / "arxiv-monitor-config-phd.example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(config["notification"]["feishu_group"], "YOUR_GROUP_CHAT_ID")
        self.assertGreaterEqual(config["request_delay_seconds"], 3.0)
        self.assertNotIn("appSecret", config)

    def test_cron_template_preserves_execution_contract(self) -> None:
        job = json.loads(
            (ROOT / "cron/arxiv-daily-review.job.template.json").read_text(encoding="utf-8")
        )
        self.assertEqual(job["schedule"]["expr"], "{{CRON_EXPR}}")
        self.assertEqual(job["schedule"]["staggerMs"], 0)
        self.assertEqual(job["sessionTarget"], "isolated")
        self.assertEqual(job["payload"]["timeoutSeconds"], 5400)
        self.assertEqual(job["delivery"]["to"], "{{FEISHU_CHAT_ID}}")
        self.assertIn("exec", job["payload"]["toolsAllow"])
        self.assertNotIn("message", job["payload"]["toolsAllow"])
        self.assertNotIn("cron", job["payload"]["toolsAllow"])
        self.assertNotIn("gateway", job["payload"]["toolsAllow"])
        self.assertIn("不发送旧报告", job["payload"]["message"])
        self.assertTrue(job["name"].startswith("Daily Paper Brief"))

    def test_render_only_creates_complete_private_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            repo = make_repo_copy(temporary_dir)
            runtime_root = Path(temporary_dir) / "runtime"
            fake_chat_id = "oc_testbundledestination"
            result = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--render-only",
                    "--repo-root",
                    str(repo),
                    "--runtime-root",
                    str(runtime_root),
                    "--chat-id",
                    fake_chat_id,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            prompt = (runtime_root / "arxiv_cron_prompt.md").read_text(encoding="utf-8")
            policy = (runtime_root / "arxiv_review_policy.md").read_text(encoding="utf-8")
            job = json.loads((runtime_root / "arxiv-daily-review.job.json").read_text(encoding="utf-8"))
            target = json.loads((runtime_root / "feishu-target.json").read_text(encoding="utf-8"))
            self.assertNotIn("{{", prompt)
            self.assertNotIn("{{", policy)
            self.assertEqual(job["delivery"]["to"], fake_chat_id)
            self.assertEqual(target["chat_id"], fake_chat_id)
            self.assertNotIn(fake_chat_id, prompt)
            self.assertEqual(job["payload"]["model"], "openai/gpt-5.6-sol")
            self.assertTrue(job["name"].startswith("Daily Paper Brief"))
            self.assertIn(str(repo), prompt)
            # The example config doubles as the ai-infra-hpc default profile.
            self.assertIn("AI 基础设施 → HPC 系统 → AI4Sci 基础设施 → 其他", prompt)
            self.assertIn("每日配额上限 4 篇", prompt)
            for name in (
                "arxiv_cron_prompt.md",
                "arxiv_review_policy.md",
                "arxiv-daily-review.job.json",
                "feishu-target.json",
            ):
                self.assertEqual((runtime_root / name).stat().st_mode & 0o777, 0o600)

    def test_render_only_requires_a_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            repo = make_repo_copy(temporary_dir, with_local_config=False)
            result = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--render-only",
                    "--repo-root",
                    str(repo),
                    "--runtime-root",
                    str(Path(temporary_dir) / "runtime"),
                    "--chat-id",
                    "oc_testbundledestination",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("setup_config.py", result.stderr)

    def test_apply_requires_explicit_local_agent_trust_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            repo = make_repo_copy(temporary_dir)
            result = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--apply",
                    "--repo-root",
                    str(repo),
                    "--runtime-root",
                    str(Path(temporary_dir) / "runtime"),
                    "--chat-id",
                    "oc_testbundledestination",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("--acknowledge-local-agent-trust", result.stderr)

    def test_open_source_gate_accepts_clean_licensed_tree(self) -> None:
        result = subprocess.run(
            [sys.executable, str(RELEASE_GATE)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "READY_FOR_OWNER_REVIEW")
        self.assertEqual(payload["blockers"], [])
        self.assertTrue((ROOT / "LICENSE").is_file())
        self.assertFalse((ROOT / "skills/academic-writing-refiner").exists())


if __name__ == "__main__":
    unittest.main()
