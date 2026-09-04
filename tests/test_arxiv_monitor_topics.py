#!/usr/bin/env python3
"""Tests for the generic topics engine and legacy config auto-conversion."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arxiv_monitor_phd import ArxivMonitorPhD


EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "arxiv-monitor-config-phd.example.json"


def write_config(directory, config):
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return str(path)


CUSTOM_CONFIG = {
    "categories": ["cs.LG"],
    "max_papers_per_day": 6,
    "min_score": 0.0,
    "topics": [
        {
            "id": "nlp",
            "label": "NLP",
            "base": 2.0,
            "cap": 9.0,
            "score_weight": 0.5,
            "term_groups": [
                {"name": "models", "terms": ["language model", "parser"], "cap": 2, "weight": 1.0},
                {"name": "tasks", "terms": ["translation", "question answering"], "cap": 2, "weight": 0.5},
            ],
            "require_all_groups": True,
            "strong_rule": {"title_group": "tasks"},
            "title_all_groups_bonus": 0.5,
            "quota": None,
        },
        {
            "id": "vision",
            "label": "Vision",
            "base": 1.0,
            "cap": 9.0,
            "score_weight": 0.4,
            "term_groups": [
                {"name": "models", "terms": ["convnet", "transformer"], "cap": 2, "weight": 1.0},
                {"name": "tasks", "terms": ["segmentation", "detection"], "cap": 2, "weight": 0.5},
            ],
            "require_all_groups": True,
            "strong_rule": {"title_any": True},
            "title_all_groups_bonus": 0.0,
            "quota": 1,
        },
    ],
}


def sample_paper(title, abstract, url="http://arxiv.org/abs/2608.00001v1"):
    return {
        "url": url,
        "title": title,
        "abstract": abstract,
        "authors": ["A. Researcher"],
        "categories": ["cs.LG"],
        "submitted_date": datetime(2026, 8, 1, tzinfo=timezone.utc),
    }


LEGACY_KEYWORD_KEYS = {
    ("ai_infra", "ai_terms"): "ai_infra_ai_terms",
    ("ai_infra", "system_terms"): "ai_infra_system_terms",
    ("hpc_systems", "anchors"): "hpc_systems_anchor",
    ("hpc_systems", "mechanisms"): "hpc_systems_mechanisms",
    ("ai4sci_infra", "domains"): "ai4sci_domains",
    ("ai4sci_infra", "infra"): "ai4sci_infra",
}
LEGACY_WEIGHT_KEYS = {
    "ai_infra": "ai_infra_match",
    "hpc_systems": "hpc_match",
    "ai4sci_infra": "ai4sci_match",
}


def legacy_config_from_example(example):
    """Rebuild a pre-topics legacy config equivalent to the new example config."""
    config = {key: value for key, value in example.items() if key != "topics"}
    keywords = dict(config.get("keywords") or {})
    weights = dict(config.get("scoring_weights") or {})
    for topic in example["topics"]:
        for group in topic["term_groups"]:
            keywords[LEGACY_KEYWORD_KEYS[(topic["id"], group["name"])]] = group["terms"]
        weights[LEGACY_WEIGHT_KEYS[topic["id"]]] = topic["score_weight"]
    config["keywords"] = keywords
    config["scoring_weights"] = weights
    return config


class CustomTopicsEngineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.monitor = ArxivMonitorPhD(write_config(self._tmp.name, CUSTOM_CONFIG))

    def test_custom_topic_scoring_formula_and_title_bonus(self):
        score, details = self.monitor._calculate_topic_match(
            sample_paper(
                "A Parser for Question Answering",
                "We study question answering with a small parser.",
            ),
            self.monitor._topics_by_id["nlp"],
        )
        # base 2.0 + min(1, 2) * 1.0 + min(1, 2) * 0.5 + title bonus 0.5
        self.assertEqual(score, 4.0)
        self.assertEqual(details["models"], ["parser"])
        self.assertEqual(details["tasks"], ["question answering"])
        self.assertTrue(details["strong_match"])

    def test_strong_rule_gates_score_to_zero_without_title_hit(self):
        score, details = self.monitor._calculate_topic_match(
            sample_paper(
                "Improving Benchmarks",
                "Our parser excels at question answering.",
            ),
            self.monitor._topics_by_id["nlp"],
        )
        self.assertEqual(score, 0.0)
        self.assertFalse(details["strong_match"])

    def test_title_any_strong_rule_accepts_any_group_in_title(self):
        score, details = self.monitor._calculate_topic_match(
            sample_paper(
                "A Convnet for Image Recognition",
                "The convnet is evaluated on detection benchmarks.",
            ),
            self.monitor._topics_by_id["vision"],
        )
        # base 1.0 + min(1, 2) * 1.0 + min(1, 2) * 0.5, no title bonus
        self.assertEqual(score, 2.5)
        self.assertTrue(details["strong_match"])

    def test_require_all_groups_blocks_partial_matches(self):
        score, _ = self.monitor._calculate_topic_match(
            sample_paper("A Convnet", "Only a convnet, no task term here."),
            self.monitor._topics_by_id["vision"],
        )
        self.assertEqual(score, 0.0)

    def test_score_details_are_keyed_by_topic_id(self):
        paper = sample_paper(
            "A Parser for Question Answering",
            "We study question answering with a small parser.",
        )
        _, details = self.monitor._calculate_score_detailed(paper)
        self.assertIn("nlp", details)
        self.assertIn("vision", details)
        self.assertGreater(details["nlp"]["score"], 0)
        self.assertEqual(details["vision"]["score"], 0)

    @staticmethod
    def scored_paper(identifier, score, topic=None):
        details = {
            "nlp": {"score": 0.0, "strong_match": False},
            "vision": {"score": 0.0, "strong_match": False},
        }
        if topic:
            details[topic] = {"score": 5.0, "strong_match": True}
        return {
            "url": f"http://arxiv.org/abs/{identifier}",
            "relevance_score": score,
            "score_details": details,
        }

    def test_quota_limits_topic_and_priority_orders_selection(self):
        papers = [
            self.scored_paper("2608.00001v1", 9.0, "vision"),
            self.scored_paper("2608.00002v1", 8.5, "vision"),
            self.scored_paper("2608.00003v1", 8.0, "nlp"),
            self.scored_paper("2608.00004v1", 7.0, "nlp"),
        ]
        selected = self.monitor._select_balanced_papers(papers, max_papers=3)
        self.assertEqual(
            [paper["primary_topic"] for paper in selected],
            ["nlp", "nlp", "vision"],
        )

    def test_topics_validation_rejects_bad_definitions(self):
        invalid = dict(CUSTOM_CONFIG)
        invalid["topics"] = CUSTOM_CONFIG["topics"] + [
            {
                "id": "nlp",
                "term_groups": [{"name": "x", "terms": ["y"]}],
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "unique"):
                ArxivMonitorPhD(write_config(directory, invalid))

        empty_group = dict(CUSTOM_CONFIG)
        empty_group["topics"] = [
            {"id": "empty", "term_groups": [{"name": "x", "terms": []}]}
        ]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "must list terms"):
                ArxivMonitorPhD(write_config(directory, empty_group))


class LegacyConversionEquivalenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        example = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        self.legacy_config = legacy_config_from_example(example)
        self.legacy = ArxivMonitorPhD(write_config(self._tmp.name, self.legacy_config))

    def _explicit_topics_config(self):
        keywords = self.legacy_config["keywords"]
        weights = self.legacy_config["scoring_weights"]
        config = dict(self.legacy_config)
        config["topics"] = [
            {
                "id": "ai_infra",
                "label": "AI Infra",
                "base": 4.0,
                "cap": 8.0,
                "score_weight": weights["ai_infra_match"],
                "term_groups": [
                    {"name": "ai_terms", "terms": keywords["ai_infra_ai_terms"], "cap": 3, "weight": 0.75},
                    {"name": "system_terms", "terms": keywords["ai_infra_system_terms"], "cap": 5, "weight": 0.60},
                ],
                "require_all_groups": True,
                "strong_rule": {"title_group": "system_terms"},
                "title_all_groups_bonus": 1.0,
                "quota": None,
            },
            {
                "id": "hpc_systems",
                "label": "HPC Systems",
                "base": 3.5,
                "cap": 8.0,
                "score_weight": weights["hpc_match"],
                "term_groups": [
                    {"name": "anchors", "terms": keywords["hpc_systems_anchor"], "cap": 3, "weight": 0.9},
                    {"name": "mechanisms", "terms": keywords["hpc_systems_mechanisms"], "cap": 5, "weight": 0.65},
                ],
                "require_all_groups": True,
                "strong_rule": {"title_any": True},
                "title_all_groups_bonus": 0.75,
                "quota": {"cap": 4, "divisor": 4},
            },
            {
                "id": "ai4sci_infra",
                "label": "AI4Sci Infra",
                "base": 3.0,
                "cap": 8.0,
                "score_weight": weights["ai4sci_match"],
                "term_groups": [
                    {"name": "domains", "terms": keywords["ai4sci_domains"], "cap": 3, "weight": 1.0},
                    {"name": "infra", "terms": keywords["ai4sci_infra"], "cap": 4, "weight": 0.75},
                ],
                "require_all_groups": True,
                "title_all_groups_bonus": 0.0,
                "quota": {"cap": 3, "divisor": 6},
            },
        ]
        return config

    def test_legacy_config_converts_to_equivalent_topics(self):
        converted = self.legacy.topics
        self.assertEqual(
            [topic["id"] for topic in converted],
            ["ai_infra", "hpc_systems", "ai4sci_infra"],
        )
        explicit = ArxivMonitorPhD(
            write_config(self._tmp.name, self._explicit_topics_config())
        )
        self.assertEqual(converted, explicit.topics)
        self.assertEqual(self.legacy._topic_order, explicit._topic_order)

    def test_scores_are_identical_between_legacy_and_new_config(self):
        explicit = ArxivMonitorPhD(
            write_config(self._tmp.name, self._explicit_topics_config())
        )
        papers = [
            sample_paper(
                "Fast LLM Serving with a Distributed KV Cache Runtime",
                "We improve language model inference throughput on GPUs.",
                "http://arxiv.org/abs/2608.00011v1",
            ),
            sample_paper(
                "Topology-Aware MPI Collectives for Exascale Systems",
                "We improve collective communication scalability on a supercomputer interconnect.",
                "http://arxiv.org/abs/2608.00012v1",
            ),
            sample_paper(
                "A Surrogate Model for Molecular Dynamics on HPC Clusters",
                "Scientific machine learning for molecular dynamics with parallel training on GPU clusters.",
                "http://arxiv.org/abs/2608.00013v1",
            ),
            sample_paper(
                "An Unrelated Combinatorics Result",
                "We prove a new bound for graph colorings.",
                "http://arxiv.org/abs/2608.00014v1",
            ),
        ]
        for paper in papers:
            with self.subTest(paper=paper["title"]):
                legacy_score, legacy_details = self.legacy._calculate_score_detailed(paper)
                explicit_score, explicit_details = explicit._calculate_score_detailed(paper)
                self.assertEqual(legacy_score, explicit_score)
                for topic_id in ("ai_infra", "hpc_systems", "ai4sci_infra"):
                    self.assertEqual(
                        legacy_details[topic_id]["score"],
                        explicit_details[topic_id]["score"],
                    )

    def test_save_selected_papers_writes_topic_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = ArxivMonitorPhD(
                write_config(directory, self.legacy_config)
            )
            monitor._save_selected_papers([])
            payload = json.loads(
                (Path(directory) / "papers_to_expand.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["papers"], [])
            self.assertEqual(
                payload["topic_labels"],
                {
                    "ai_infra": "AI Infra",
                    "hpc_systems": "HPC Systems",
                    "ai4sci_infra": "AI4Sci Infra",
                },
            )


if __name__ == "__main__":
    unittest.main()
