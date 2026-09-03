import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from PIL import Image

import arxiv_report_pipeline as pipeline
import arxiv_send_html_to_feishu as sender
import ai_writing_metrics as writing_metrics


class ArxivReportPipelineTest(unittest.TestCase):
    def test_fetch_limited_revalidates_source_and_applies_delay(self):
        class FakeResponse:
            url = "https://arxiv.org/pdf/2608.13505"
            status_code = 200
            headers = {"content-type": "application/pdf", "content-length": "4"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, _chunk_size):
                yield b"%PDF"

        session = mock.Mock()
        session.get.return_value = FakeResponse()
        with mock.patch.object(pipeline.time, "sleep") as sleep:
            data, content_type, final_url = pipeline.fetch_limited(
                session,
                "https://arxiv.org/pdf/2608.13505",
                max_bytes=32,
            )
        self.assertEqual(data, b"%PDF")
        self.assertEqual(content_type, "application/pdf")
        self.assertEqual(final_url, "https://arxiv.org/pdf/2608.13505")
        sleep.assert_called_once_with(pipeline.REQUEST_DELAY_SECONDS)

    def test_fetch_limited_rejects_off_domain_redirect_before_following(self):
        class RedirectResponse:
            url = "https://arxiv.org/redirect"
            status_code = 302
            headers = {"location": "http://127.0.0.1/latest/meta-data"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def raise_for_status(self):
                return None

        session = mock.Mock()
        session.get.return_value = RedirectResponse()
        with self.assertRaisesRegex(pipeline.PipelineError, "refusing non-arXiv"):
            pipeline.fetch_limited(
                session,
                "https://arxiv.org/redirect",
                max_bytes=32,
            )
        self.assertEqual(session.get.call_count, 1)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_arxiv_url_rejects_userinfo_and_non_https_port(self):
        for value in (
            "https://attacker@example.com/file",
            "https://arxiv.org:8443/file",
            "http://arxiv.org/file",
        ):
            with self.subTest(value=value):
                with self.assertRaises(pipeline.PipelineError):
                    pipeline.require_arxiv_url(value)

    def test_normalize_arxiv_id(self):
        self.assertEqual(
            pipeline.normalize_arxiv_id("https://arxiv.org/abs/2608.13505v1"),
            "2608.13505v1",
        )
        self.assertEqual(
            pipeline.normalize_arxiv_id("https://arxiv.org/pdf/1706.03762.pdf"),
            "1706.03762",
        )
        self.assertEqual(pipeline.base_arxiv_id("2608.13505v3"), "2608.13505")

    def test_svg_sanitizer_removes_executable_content(self):
        source = b'''<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">
        <style>rect { fill: url(https://example.com/a); }</style>
        <script>alert(2)</script><rect width="10" height="10" style="filter:url(javascript:alert(3))"/>
        <image href="https://example.com/tracker.png"/></svg>'''
        cleaned = pipeline.sanitize_svg(source)
        self.assertNotIn(b"script", cleaned)
        self.assertNotIn(b"<style", cleaned)
        self.assertNotIn(b"onload", cleaned)
        self.assertNotIn(b"example.com", cleaned)
        self.assertNotIn(b"javascript:", cleaned)
        self.assertNotIn(b"url(", cleaned)
        self.assertIn(b"rect", cleaned)

    def test_svg_sanitizer_rejects_doctype_and_entities(self):
        payload = b'''<!DOCTYPE svg [<!ENTITY local SYSTEM "file:///etc/hosts">]>
        <svg xmlns="http://www.w3.org/2000/svg"><text>&local;</text></svg>'''
        with self.assertRaisesRegex(pipeline.PipelineError, "DOCTYPE"):
            pipeline.sanitize_svg(payload)

    def test_raster_metadata_rejects_animated_images(self):
        first = Image.new("RGB", (320, 180), "white")
        second = Image.new("RGB", (320, 180), "black")
        payload = io.BytesIO()
        first.save(payload, format="GIF", save_all=True, append_images=[second])
        with self.assertRaisesRegex(pipeline.PipelineError, "animated"):
            pipeline.raster_metadata(payload.getvalue())

    def test_data_uri_cannot_read_outside_report_root_or_follow_symlink(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            report_root = root / "report"
            report_root.mkdir()
            inside = report_root / "figure.png"
            Image.new("RGB", (320, 180), "white").save(inside)
            outside = root / "private.txt"
            outside.write_text("private marker", encoding="utf-8")

            self.assertTrue(
                pipeline.data_uri(inside, report_root).startswith("data:image/png")
            )
            with self.assertRaisesRegex(pipeline.PipelineError, "outside report root"):
                pipeline.data_uri(outside, report_root)

            link = report_root / "linked-private.txt"
            link.symlink_to(outside)
            with self.assertRaisesRegex(pipeline.PipelineError, "symbolic link"):
                pipeline.data_uri(link, report_root)

    def test_manifest_resource_paths_are_confined_to_dated_report(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            report = root / "20260903"
            paper_dir = report / "assets" / "2609.00001v1"
            paper_dir.mkdir(parents=True)
            outside = root / "private.png"
            Image.new("RGB", (320, 180), "white").save(outside)
            manifest = {
                "report_dir": str(report),
                "papers": [
                    {
                        "arxiv_id": "2609.00001v1",
                        "paper_dir": str(paper_dir),
                        "main_figure": {"path": str(outside)},
                    }
                ],
            }
            with self.assertRaisesRegex(pipeline.PipelineError, "outside report root"):
                pipeline.confine_manifest_paths(manifest, report.resolve())

    def test_sender_idempotency_is_scoped_to_destination_chat(self):
        report_hash = "a" * 64
        first_chat = "oc_first"
        second_chat = "oc_second"
        first_key = sender.delivery_state_key(report_hash, first_chat)
        second_key = sender.delivery_state_key(report_hash, second_chat)
        self.assertNotEqual(first_key, second_key)

        first_record = {
            "message_id": "om_first",
            "chat_id_hash": sender.delivery_target_hash(first_chat),
        }
        sent_v2 = {first_key: first_record}
        self.assertEqual(
            sender.previous_delivery(sent_v2, report_hash, first_chat),
            first_record,
        )
        self.assertIsNone(
            sender.previous_delivery(sent_v2, report_hash, second_chat)
        )

    def test_sender_resolves_private_target_file(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            target_path = Path(temporary_dir) / "target.json"
            target_path.write_text(
                json.dumps({"chat_id": "oc_external"}), encoding="utf-8"
            )
            self.assertEqual(
                sender.resolve_chat_id(None, str(target_path)), "oc_external"
            )
            with self.assertRaisesRegex(sender.SendError, "invalid Feishu group"):
                sender.resolve_chat_id("ou_not_a_group", None)
            link = Path(temporary_dir) / "target-link.json"
            link.symlink_to(target_path)
            with self.assertRaisesRegex(sender.SendError, "symbolic link"):
                sender.resolve_chat_id(None, str(link))

    def test_contribution_drawio_uses_vertical_single_column_layout(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            output = Path(temporary_dir) / "reviewer_contribution.drawio"
            pipeline.write_drawio_contribution(
                {
                    "problem": "The problem statement.",
                    "approach": "The proposed approach.",
                    "mechanism": "The key mechanism.",
                    "evidence": "The evidence and impact.",
                },
                output,
            )

            root = ET.parse(output).getroot()
            model = root.find(".//mxGraphModel")
            self.assertIsNotNone(model)
            self.assertGreater(
                int(model.attrib["pageHeight"]),
                int(model.attrib["pageWidth"]),
            )
            geometries = {}
            for cell_id in ("2", "3", "4", "5"):
                cell = root.find(f".//mxCell[@id='{cell_id}']")
                self.assertIsNotNone(cell)
                geometry = cell.find("mxGeometry")
                self.assertIsNotNone(geometry)
                geometries[cell_id] = geometry.attrib
            self.assertIn("问题 / Problem", root.find(".//mxCell[@id='2']").attrib["value"])
            self.assertEqual(
                {geometry["x"] for geometry in geometries.values()},
                {"30"},
            )
            self.assertEqual(
                len({geometry["width"] for geometry in geometries.values()}),
                1,
            )
            y_positions = [
                int(geometries[cell_id]["y"])
                for cell_id in ("2", "3", "4", "5")
            ]
            self.assertEqual(y_positions, sorted(y_positions))
            self.assertEqual(len(set(y_positions)), 4)
            for edge_id in ("10", "11", "12"):
                edge = root.find(f".//mxCell[@id='{edge_id}']")
                self.assertIn("exitY=1", edge.attrib["style"])
                self.assertIn("entryY=0", edge.attrib["style"])

    def test_ai_writing_metrics_are_descriptive_and_traceable(self):
        text = """
        Abstract
        It is worth noting that this example is deliberately formulaic. This
        underscores the need for direct evidence. Certainly! Here is a revised
        version of the paragraph. The intricate and meticulous system is
        described without claiming that any metric determines authorship.

        Introduction
        The method records a source hash and matched context. The method records
        a source hash and matched context. The method records a source hash and
        matched context.

        References
        It is worth noting that bibliography text must not be analyzed.
        """
        text = text.replace(
            "        References",
            ("The documented method links each claim to a measured result. " * 70)
            + "\nReferences",
        )
        result = writing_metrics.analyze_text(text, "/tmp/example.txt")
        self.assertTrue(result["analysis_scope"]["bibliography_removed"])
        self.assertEqual(result["metrics"]["chatbot_residue"]["count"], 2)
        self.assertGreater(
            result["metrics"]["formulaic_transition_density"]["count"],
            0,
        )
        self.assertTrue(
            result["metrics"]["formulaic_transition_density"]["examples"]
        )
        self.assertNotIn("probability", result)
        repeated = writing_metrics.analyze_text(text, "/tmp/example.txt")
        self.assertEqual(result["source_sha256"], repeated["source_sha256"])

    def test_build_standalone_html_and_sender_dry_validation(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            paper_dir = root / "assets" / "2608.13505v1"
            paper_dir.mkdir(parents=True)
            main_figure = paper_dir / "main_figure.png"
            Image.new("RGB", (900, 480), "#e7edf4").save(main_figure)
            analysis_text = paper_dir / "paper_analysis.txt"
            section_body = " ".join(
                [
                    "The evaluated system records its inputs, mechanism, baseline, measurement, and uncertainty."
                    for _ in range(42)
                ]
            )
            analysis_text.write_text(
                "\n\n".join(
                    f"{heading}\n{section_body}"
                    for heading in (
                        "Abstract",
                        "1 Introduction",
                        "2 Method",
                        "3 Experiments",
                        "4 Limitations",
                    )
                ),
                encoding="utf-8",
            )
            metrics_path = writing_metrics.write_metrics_json(
                writing_metrics.analyze_file(analysis_text),
                paper_dir / "ai_writing_metrics.json",
            )
            analyzed_word_count = writing_metrics.analyze_file(analysis_text)[
                "document_counts"
            ]["words"]

            manifest = {
                "schema_version": 2,
                "date": "2026-08-14",
                "report_dir": str(root),
                "papers": [
                    {
                        "arxiv_id": "2608.13505v1",
                        "title": "A Test Paper for Reliable Daily Reviews",
                        "url": "https://arxiv.org/abs/2608.13505v1",
                        "authors": ["A. Researcher", "B. Scientist"],
                        "categories": ["cs.DC", "cs.LG"],
                        "relevance_score": 9.1,
                        "primary_topic": "ai_infra",
                        "code_url": "https://example.org/code",
                        "paper_dir": str(paper_dir),
                        "analysis_text_path": str(analysis_text),
                        "ai_writing_metrics_path": str(metrics_path),
                        "main_figure": {
                            "path": str(main_figure),
                            "kind": "paper_main_figure",
                            "caption": "Overview of the evaluated system.",
                            "source_url": "https://arxiv.org/html/2608.13505v1",
                        },
                    }
                ],
            }
            indicator_scorecard = []
            for indicator in pipeline.AI_WRITING_INDICATORS:
                score = 1 if indicator["id"] == "formulaic_scaffolding" else 0
                indicator_scorecard.append(
                    {
                        "id": indicator["id"],
                        "score": score,
                        "locations": [
                            "§1 引言"
                            if score
                            else "摘要及第 1–4 节"
                        ],
                        "evidence": (
                            "引言中出现一处较通用的模板化衔接表达。"
                            if score
                            else "在已审阅章节中未观察到实质性异常。"
                        ),
                        "counter_evidence": "技术术语、定义和测量口径在各章节之间保持稳定。",
                    }
                )
            review = {
                "schema_version": 2,
                "date": "2026-08-14",
                "run_summary": {
                    "total_fetched": 300,
                    "recommended": 1,
                    "ai_infra_matches": 1,
                    "hpc_matches": 0,
                    "ai4sci_matches": 0,
                    "top3_arxiv_ids": ["2608.13505v1"],
                },
                "papers": [
                    {
                        "arxiv_id": "2608.13505v1",
                        "title_zh": "可靠每日评审的测试论文",
                        "summary_en": "The paper studies a reproducible systems workflow with explicit evidence boundaries.",
                        "summary_zh": "该论文研究一套具有明确证据边界的可复现系统工作流。方法记录输入、关键机制与评测口径，并在生成报告前执行结构化校验。实验示例展示了如何把具体系统机制对应到可测量结果。其主要价值在于让每日论文筛选过程可以复核，而不是依赖无法追踪的主观摘要。",
                        "why_read": "论文把具体机制、可测量结果和可复核证据链连接起来，适合作为研究情报流水线的最小验证样例。",
                        "priority": "High",
                        "group_profile": {
                            "lead_groups": "公开材料显示主要团队为 Systems Lab。",
                            "key_people": "关键作者包括 A. Researcher 与 B. Scientist。",
                            "focus": "团队主要研究分布式系统与高效机器学习基础设施。",
                            "prior_work": "此前公开过相关运行时、基准测试和可复现实验材料。",
                            "why_follow": "该团队持续发布带有明确测量口径的系统工件，值得跟踪。",
                            "evidence_confidence": "中等；依据论文元数据与公开项目页面。",
                        },
                        "reviews": {
                            "systems": "系统机制总体合理，但尾延迟结论仍需要更强基线和更完整的负载分布支撑。",
                            "ai4sci": "该工作流可用于科学模拟任务编排，不过当前没有展示具体领域案例，因此相关性仍属间接。",
                            "research_value": "进行小规模复现的成本较低，可以有效检验证据记录、报告构建和异常回退是否可靠。",
                        },
                        "ai_writing_assessment": {
                            "label": "low",
                            "confidence": "Medium",
                            "disclosure": "在已审阅正文、致谢和作者说明中未发现明确的 AI 写作使用声明。",
                            "scope": {
                                "analysis_text_path": str(analysis_text),
                                "automatic_metrics_path": str(metrics_path),
                                "analyzed_word_count": analyzed_word_count,
                                "section_checks": [
                                    {"section": "abstract", "status": "reviewed", "location": "摘要"},
                                    {"section": "introduction", "status": "reviewed", "location": "第 1 节"},
                                    {"section": "method", "status": "reviewed", "location": "第 2 节"},
                                    {"section": "experiments", "status": "reviewed", "location": "第 3 节"},
                                    {"section": "limitations_or_conclusion", "status": "reviewed", "location": "第 4 节"},
                                    {"section": "ai_use_disclosure", "status": "searched_not_found", "location": "全文、致谢与作者说明"},
                                ],
                                "coverage": "sufficient",
                                "coverage_note": "已在可正常读取的文本上完成五个内容区域和 AI 使用声明检索。",
                            },
                            "indicator_scorecard": indicator_scorecard,
                            "aggregate": {
                                "rubric_version": pipeline.AI_WRITING_RUBRIC_VERSION,
                                "score_total": 1,
                                "positive_indicators": 1,
                                "positive_families": ["discourse_style"],
                                "positive_sections": ["§1 引言"],
                                "rationale": "总分为 1，落在固定的 0–3 分低迹象区间；唯一弱风格指标可以由普通学术写作习惯解释。",
                            },
                            "evidence_for": ["引言中有一处衔接语使用了较通用的学术脚手架。"],
                            "counter_evidence": ["术语、符号和定义在各章节之间保持一致。"],
                            "confounders": ["会议模板与常规英语编辑也可能产生模板化衔接。"],
                            "caveat": "该结果只评估写作辅助迹象，不能证明作者身份、学术不端、抄袭或研究有效性。",
                        },
                        "contribution_diagram": {
                            "problem": "每日论文筛选缺少可追踪到原文的证据。",
                            "approach": "获取论文资源，并要求填写结构化评审字段。",
                            "mechanism": "渲染前校验来源、核心主张和可复现性信息。",
                            "evidence": "离线 HTML 同时保留论文原图与评审贡献图。",
                        },
                        "claim_evidence": [
                            {
                                "claim": "该工作流具备可复现性。",
                                "evidence": "工件记录输入、来源链接以及可编辑贡献图。",
                                "strength": "Medium",
                            }
                        ],
                        "novelty_boundary": "最接近的工作流没有同时保留论文原图、结构化证据审计和写作迹象解释边界。",
                        "evaluation_validity": "测试覆盖结构校验与 HTML 渲染，但尚不能说明长期论文推荐质量。",
                        "reproducibility": {
                            "code": "代码位于本地工作区，可直接执行测试。",
                            "data_models": "使用合成测试数据，不需要模型权重。",
                            "license": "公开信息不足，当前测试不推断许可证状态。",
                            "requirements": "需要 Python、Pillow、lxml 和 Poppler。",
                            "effort": "结构烟雾测试的复现工作量较低。",
                            "smallest_test": "构建包含一篇论文的报告，并打开生成的独立 HTML。",
                        },
                        "artifact_risks": ["远程主图提取可能失败，必须使用带明确标签的首页回退。"],
                        "limitations": ["合成测试数据无法评估真实科学论文的推荐质量。"],
                        "next_steps": ["使用真实 arXiv 论文执行主图提取和中文报告烟雾测试。"],
                    }
                ],
            }

            manifest_path = root / "assets_manifest.json"
            reviews_path = root / "reviews.json"
            reviews_template_path = root / "reviews.template.json"
            batch_path = root / "review_batch_1.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            reviews_template_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "date": "2026-08-14",
                        "run_summary": {},
                        "papers": [{"arxiv_id": "2608.13505v1"}],
                    }
                ),
                encoding="utf-8",
            )
            batch_path.write_text(json.dumps(review["papers"]), encoding="utf-8")
            pipeline.merge_review_batches(
                manifest_path,
                reviews_template_path,
                [batch_path],
                reviews_path,
                review["run_summary"],
            )
            merged_review = pipeline.load_json(reviews_path)
            self.assertEqual(merged_review["papers"], review["papers"])
            self.assertEqual(merged_review["run_summary"], review["run_summary"])
            with self.assertRaisesRegex(
                pipeline.PipelineError,
                "review batches not ready before timeout",
            ):
                pipeline.merge_review_batches(
                    manifest_path,
                    reviews_template_path,
                    [root / "missing-review-batch.json"],
                    root / "must-not-exist.json",
                    review["run_summary"],
                    wait_seconds=0,
                    poll_seconds=0.01,
                )

            output = pipeline.build_report(manifest_path, reviews_path)
            report = output.read_text(encoding="utf-8")
            self.assertIn("Content-Security-Policy", report)
            self.assertIn('content="no-referrer"', report)
            self.assertIn("data:image/png;base64", report)
            self.assertIn("data:image/svg+xml;base64", report)
            self.assertIn("AI-assisted writing signals", report)
            self.assertIn(">低 / low</span>", report)
            self.assertIn("指标评分卡 / Indicator scorecard", report)
            self.assertIn("自动描述指标 / Automatic metrics", report)
            self.assertIn("AI-writing-signals-v1.0", report)
            self.assertIn("formulaic_transition_density", report)
            self.assertIn(">0/2</span>", report)
            self.assertIn(">1/2</span>", report)
            self.assertIn("Claim–evidence audit", report)
            self.assertIn("Daily arXiv Report / 每日 arXiv 论文分析报告", report)
            self.assertIn("Thank you to arXiv for use of its open access interoperability.", report)
            self.assertIn("Daily Paper Brief 是独立项目", report)
            self.assertIn("AI 基础设施 · 主主题", report)
            self.assertIn(".topic-hpc_systems", report)
            self.assertIn("<h2>可靠每日评审的测试论文</h2>", report)
            self.assertIn(
                '<p class="title-en">A Test Paper for Reliable Daily Reviews</p>',
                report,
            )
            self.assertIn('<details class="english-summary">', report)
            self.assertIn("中文总结", report)
            self.assertTrue((paper_dir / "reviewer_contribution.drawio").exists())
            self.assertTrue((paper_dir / "reviewer_contribution.svg").exists())
            contribution_root = ET.parse(
                paper_dir / "reviewer_contribution.svg"
            ).getroot()
            view_box = [
                float(value)
                for value in contribution_root.attrib["viewBox"].split()
            ]
            self.assertGreater(view_box[3], view_box[2])

            invalid_review = copy.deepcopy(review)
            invalid_review["papers"][0]["ai_writing_assessment"]["aggregate"][
                "score_total"
            ] = 7
            invalid_path = root / "reviews-invalid.json"
            invalid_path.write_text(json.dumps(invalid_review), encoding="utf-8")
            with self.assertRaisesRegex(
                pipeline.PipelineError,
                "score_total must equal scorecard sum 1",
            ):
                pipeline.build_report(manifest_path, invalid_path)

            english_primary_review = copy.deepcopy(review)
            english_primary_review["papers"][0]["summary_zh"] = (
                "This field incorrectly replaces the Chinese primary summary with English."
            )
            english_primary_path = root / "reviews-english-primary.json"
            english_primary_path.write_text(
                json.dumps(english_primary_review), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                pipeline.PipelineError,
                "at least 40 Chinese characters in summary_zh",
            ):
                pipeline.build_report(manifest_path, english_primary_path)

            receipt = output.with_suffix(".receipt.json")
            self.assertTrue(receipt.exists())
            metadata, body = sender.load_validated_html(
                output,
                report_root=root,
                receipt_path=receipt,
            )
            self.assertEqual(metadata["name"], output.name)
            self.assertGreater(metadata["bytes"], 512)
            self.assertEqual(metadata["bytes"], len(body))
            dry_run = subprocess.run(
                [
                    sys.executable,
                    str(Path(sender.__file__).resolve()),
                    "--file",
                    str(output),
                    "--report-root",
                    str(root),
                    "--chat-id",
                    "oc_testdestination",
                    "--dry-run",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(dry_run.returncode, 0, dry_run.stdout + dry_run.stderr)
            self.assertTrue(json.loads(dry_run.stdout)["dry_run"])
            receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
            receipt_payload["report_sha256"] = "0" * 64
            receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
            with self.assertRaisesRegex(sender.SendError, "does not match"):
                sender.load_validated_html(
                    output,
                    report_root=root,
                    receipt_path=receipt,
                )
            session = sender.feishu_session()
            try:
                self.assertFalse(session.trust_env)
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
