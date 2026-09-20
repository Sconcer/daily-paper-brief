#!/usr/bin/env python3
"""The pushed-ids ledger must follow delivery, not fetching.

Papers used to be recorded as pushed as soon as the local markdown was written,
while the review, HTML build and Feishu send all happen afterwards. A failure in
any later stage therefore burned those papers: every subsequent run saw them as
already pushed, reported "no new papers" and exited successfully, so the outage
was both invisible and unrecoverable. These tests pin the corrected order and the
catch-up window that goes with it.
"""

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import arxiv_monitor_phd as monitor_module  # noqa: E402
import arxiv_send_html_to_feishu as sender_module  # noqa: E402


def write_sent(report_root, *file_names):
    report_root.mkdir(parents=True, exist_ok=True)
    (report_root / "sent.json").write_text(
        json.dumps({
            "schema_version": 2,
            "sent": {
                f"hash{index}:target": {
                    "message_id": f"om_{index}",
                    "file_name": name,
                    "bytes": 1,
                    "sent_at": "2026-09-20T10:00:00+08:00",
                }
                for index, name in enumerate(file_names)
            },
        }),
        encoding="utf-8",
    )


class CatchUpWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dpb-window-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "arxiv_reports"

    def test_no_receipts_at_all_keeps_the_normal_window(self):
        self.root.mkdir(parents=True)
        self.assertEqual(monitor_module.catchup_days(self.root), monitor_module.DEFAULT_LOOKBACK_DAYS)

    def test_window_reaches_back_to_the_last_delivered_report(self):
        write_sent(self.root, "arxiv_ai_infra_review_20260912.html")
        self.assertEqual(
            monitor_module.catchup_days(self.root, today=date(2026, 9, 20)), 8
        )

    def test_recent_delivery_does_not_shrink_below_the_floor(self):
        write_sent(self.root, "arxiv_ai_infra_review_20260920.html")
        self.assertEqual(
            monitor_module.catchup_days(self.root, today=date(2026, 9, 20)),
            monitor_module.DEFAULT_LOOKBACK_DAYS,
        )

    def test_a_long_outage_is_capped_rather_than_unbounded(self):
        write_sent(self.root, "arxiv_ai_infra_review_20250101.html")
        self.assertEqual(
            monitor_module.catchup_days(self.root, today=date(2026, 9, 20), cap=14), 14
        )

    def test_latest_receipt_wins_regardless_of_order(self):
        write_sent(
            self.root,
            "arxiv_ai_infra_review_20260912.html",
            "arxiv_ai_infra_review_20260905.html",
        )
        self.assertEqual(monitor_module.delivered_through(self.root), date(2026, 9, 12))

    def test_unreadable_receipts_fall_back_to_the_normal_window(self):
        self.root.mkdir(parents=True)
        (self.root / "sent.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(monitor_module.catchup_days(self.root), monitor_module.DEFAULT_LOOKBACK_DAYS)


class LedgerCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dpb-ledger-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "arxiv_pending_ids_20260920.json").write_text(
            json.dumps({"staged_at": "t", "ids": {"2609.11111v1": "t", "2609.22222v1": "t"}}),
            encoding="utf-8",
        )
        (self.base / "arxiv_pushed_ids.json").write_text(
            json.dumps({"2609.00000v1": "old"}), encoding="utf-8"
        )

    def ledger(self):
        return json.loads((self.base / "arxiv_pushed_ids.json").read_text(encoding="utf-8"))

    def test_commit_adds_staged_ids_and_keeps_history(self):
        self.assertEqual(monitor_module.commit_pending_pushed("20260920", self.base), 2)
        self.assertEqual(
            sorted(self.ledger()), ["2609.00000v1", "2609.11111v1", "2609.22222v1"]
        )

    def test_commit_archives_the_staging_file_and_is_idempotent(self):
        monitor_module.commit_pending_pushed("20260920", self.base)
        self.assertTrue((self.base / "arxiv_pending_ids_20260920.json.committed").exists())
        self.assertFalse((self.base / "arxiv_pending_ids_20260920.json").exists())
        self.assertEqual(monitor_module.commit_pending_pushed("20260920", self.base), 0)

    def test_a_day_that_was_never_staged_is_not_an_error(self):
        self.assertEqual(monitor_module.commit_pending_pushed("20991231", self.base), 0)

    def test_nothing_is_committed_until_delivery(self):
        """The whole point: staged ids must not reach the ledger on their own."""
        self.assertNotIn("2609.11111v1", self.ledger())


class CommitDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dpb-delivery-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "arxiv_pending_ids_20260920.json").write_text(
            json.dumps({"staged_at": "t", "ids": {"2609.33333v1": "t"}}), encoding="utf-8"
        )
        (self.base / "arxiv_pushed_ids.json").write_text("{}", encoding="utf-8")
        self.day_dir = self.base / "arxiv_reports" / "20260920"
        self.day_dir.mkdir(parents=True)

    def test_delivered_report_commits_its_day(self):
        report = self.day_dir / "arxiv_ai_infra_review_20260920.html"
        report.write_text("<html></html>", encoding="utf-8")
        self.assertEqual(sender_module.commit_delivery(report, self.base), 1)
        ledger = json.loads((self.base / "arxiv_pushed_ids.json").read_text(encoding="utf-8"))
        self.assertIn("2609.33333v1", ledger)

    def test_a_report_outside_a_dated_directory_is_left_alone(self):
        loose = self.base / "arxiv_reports" / "adhoc"
        loose.mkdir(parents=True)
        report = loose / "report.html"
        report.write_text("<html></html>", encoding="utf-8")
        self.assertIsNone(sender_module.commit_delivery(report, self.base))
        self.assertEqual(
            json.loads((self.base / "arxiv_pushed_ids.json").read_text(encoding="utf-8")), {}
        )


if __name__ == "__main__":
    unittest.main()
