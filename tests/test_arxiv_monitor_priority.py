#!/usr/bin/env python3

import unittest
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arxiv_monitor_phd import ArxivMonitorPhD


CONFIG = Path(__file__).resolve().parents[1] / "config" / "arxiv-monitor-config-phd.example.json"


class ArxivMonitorPriorityTest(unittest.TestCase):
    def setUp(self):
        self.monitor = ArxivMonitorPhD(str(CONFIG))

    def test_public_source_access_defaults_are_polite(self):
        self.assertEqual(self.monitor.base_url, "https://export.arxiv.org/api/query")
        self.assertGreaterEqual(self.monitor.request_delay_seconds, 3.0)

    def test_bounded_response_rejects_oversized_decoded_body(self):
        class Response:
            headers = {}

            def iter_content(self, _chunk_size):
                yield b"a" * 8
                yield b"b" * 8

        with self.assertRaisesRegex(ValueError, "response exceeds"):
            self.monitor._read_bounded_response(Response(), max_bytes=12)

    def test_config_rejects_category_path_injection_and_unbounded_report(self):
        original = json.loads(CONFIG.read_text(encoding="utf-8"))
        for categories, maximum in ((["../../private"], 18), (["cs.DC"], 25)):
            with self.subTest(categories=categories, maximum=maximum):
                candidate = dict(original)
                candidate["categories"] = categories
                candidate["max_papers_per_day"] = maximum
                with tempfile.TemporaryDirectory() as temporary_dir:
                    path = Path(temporary_dir) / "config.json"
                    path.write_text(json.dumps(candidate), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        ArxivMonitorPhD(str(path))

    @staticmethod
    def paper(identifier, score, ai_infra=False, hpc=False, ai4sci=False):
        return {
            "url": f"http://arxiv.org/abs/{identifier}",
            "relevance_score": score,
            "score_details": {
                "ai_infra": {
                    "score": 7.0 if ai_infra else 0.0,
                    "ai_terms": ["LLM"] if ai_infra else [],
                    "system_terms": ["serving"] if ai_infra else [],
                    "strong_match": ai_infra,
                },
                "ai4sci": {
                    "score": 6.0 if ai4sci else 0.0,
                    "domains": ["computational physics"] if ai4sci else [],
                    "infra": ["HPC"] if ai4sci else [],
                },
                "hpc": {
                    "score": 7.0 if hpc else 0.0,
                    "anchors": ["MPI"] if hpc else [],
                    "mechanisms": ["collective communication"] if hpc else [],
                    "strong_match": hpc,
                },
            },
        }

    def test_ai_infra_is_ranked_before_hpc_ai4sci_and_other(self):
        other = self.paper("2608.00001v1", 9.9)
        ai4sci = self.paper("2608.00002v1", 8.8, ai4sci=True)
        hpc = self.paper("2608.00006v1", 7.5, hpc=True)
        ai_infra = self.paper("2608.00003v1", 6.0, ai_infra=True)

        selected = self.monitor._select_balanced_papers(
            [other, ai4sci, hpc, ai_infra], max_papers=4
        )

        self.assertEqual(
            [paper["primary_topic"] for paper in selected],
            ["ai_infra", "hpc_systems", "ai4sci_infra", "other"],
        )

    def test_overlap_uses_ai_infra_as_primary_topic(self):
        overlap = self.paper("2608.00004v1", 7.0, ai_infra=True, ai4sci=True)
        self.assertEqual(self.monitor._paper_primary_topic(overlap), "ai_infra")

    def test_ai_infra_match_requires_model_and_system_signals(self):
        matched_score, details = self.monitor._calculate_ai_infra_match(
            {
                "title": "Fast LLM Serving with a Distributed KV Cache Runtime",
                "abstract": "We improve language model inference throughput on GPUs.",
            }
        )
        domain_only_score, _ = self.monitor._calculate_ai_infra_match(
            {
                "title": "Parametric Matrices for Nuclear Physics",
                "abstract": "A computational physics surrogate for many-body calculations.",
            }
        )

        self.assertGreater(matched_score, 0)
        self.assertTrue(details["ai_terms"])
        self.assertTrue(details["system_terms"])
        self.assertEqual(domain_only_score, 0)

    def test_hpc_match_does_not_require_ai_anchor(self):
        score, details = self.monitor._calculate_hpc_systems_match(
            {
                "title": "Topology-Aware MPI Collectives for Exascale Systems",
                "abstract": "We improve collective communication scalability on a supercomputer interconnect.",
            }
        )
        generic_score, _ = self.monitor._calculate_hpc_systems_match(
            {
                "title": "A Scientific Dataset for Molecular Properties",
                "abstract": "The dataset contains molecular structures.",
            }
        )
        self.assertGreater(score, 0)
        self.assertTrue(details["anchors"])
        self.assertTrue(details["mechanisms"])
        self.assertEqual(generic_score, 0)

    def test_hpc_quota_survives_many_ai_infra_candidates(self):
        ai_papers = [
            self.paper(f"2608.10{index:03d}v1", 10 - index * 0.1, ai_infra=True)
            for index in range(10)
        ]
        hpc_paper = self.paper("2608.20001v1", 6.0, hpc=True)
        selected = self.monitor._select_balanced_papers(
            ai_papers + [hpc_paper], max_papers=6
        )
        self.assertIn("hpc_systems", [paper["primary_topic"] for paper in selected])

    def test_term_matching_uses_boundaries(self):
        self.assertTrue(self.monitor._term_in_text("AI", "an AI accelerator"))
        self.assertFalse(self.monitor._term_in_text("AI", "training runtime"))
        self.assertTrue(self.monitor._term_in_text("serving", "LLM serving"))
        self.assertFalse(self.monitor._term_in_text("serving", "preserving accuracy"))

    def test_redo_today_preserves_historical_deduplication(self):
        paper = {"url": "http://arxiv.org/abs/2608.00005v1"}
        self.monitor.pushed_ids = {
            "2608.00005v1": datetime.now().isoformat(),
        }
        self.assertTrue(self.monitor._is_already_pushed(paper))

        self.monitor.redo_today = True
        self.assertFalse(self.monitor._is_already_pushed(paper))

        self.monitor.pushed_ids["2608.00005v1"] = (
            datetime.now() - timedelta(days=1)
        ).isoformat()
        self.assertTrue(self.monitor._is_already_pushed(paper))


if __name__ == "__main__":
    unittest.main()
