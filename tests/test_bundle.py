from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_openclaw_cron.py"
RELEASE_GATE = ROOT / "scripts" / "check_open_source_readiness.py"


class BundleTests(unittest.TestCase):
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
            (ROOT / "arxiv-monitor-config-phd.example.json").read_text(encoding="utf-8")
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
            runtime_root = Path(temporary_dir) / "runtime"
            fake_chat_id = "oc_testbundledestination"
            result = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--render-only",
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
            self.assertIn(str(ROOT), prompt)
            for name in (
                "arxiv_cron_prompt.md",
                "arxiv_review_policy.md",
                "arxiv-daily-review.job.json",
                "feishu-target.json",
            ):
                self.assertEqual((runtime_root / name).stat().st_mode & 0o777, 0o600)

    def test_apply_requires_explicit_local_agent_trust_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            result = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--apply",
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
