#!/usr/bin/env python3
"""Prepare paper assets and build the daily standalone arXiv HTML report.

The pipeline deliberately separates retrieval from model judgment:

* ``prepare`` downloads trusted arXiv assets, extracts review text, and records
  provenance in ``assets_manifest.json``.
* ``merge-batches`` blocks until sub-agent review batches are complete, restores
  template order, validates them, and writes ``reviews.json``.
* ``build`` validates that payload, creates editable draw.io contribution maps,
  exports them to SVG, and embeds all assets into one offline HTML file.

Downloaded paper content is treated as data. This module never executes source
archives, LaTeX, notebooks, or commands supplied by a paper.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import html
import io
import json
import math
import mimetypes
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import time
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

import requests
from lxml import etree
from lxml import html as lxml_html
from PIL import Image

from ai_writing_metrics import analyze_file, write_metrics_json


WORKSPACE = Path(__file__).resolve().parent
DEFAULT_REPORT_ROOT = WORKSPACE / "arxiv_reports"
USER_AGENT = os.environ.get(
    "DAILY_PAPER_BRIEF_USER_AGENT",
    "DailyPaperBrief/1.0 (independent research tool)",
)
REQUEST_DELAY_SECONDS = max(
    0.0,
    float(os.environ.get("DAILY_PAPER_BRIEF_REQUEST_DELAY_SECONDS", "3")),
)
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
MAX_EMBEDDED_ASSET_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_IMAGE_PIXELS = 50_000_000
MAX_IMAGE_DIMENSION = 16_384
MAX_SVG_ELEMENTS = 50_000
MAX_NATIVE_LOG_BYTES = 2 * 1024 * 1024
MAX_NATIVE_FILE_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_PAPERS_PER_REPORT = 24
ALLOWED_RASTER_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "TIFF")
PDFTOTEXT_BIN = shutil.which("pdftotext") or "/opt/homebrew/bin/pdftotext"
PDFTOPPM_BIN = shutil.which("pdftoppm") or "/opt/homebrew/bin/pdftoppm"

MAIN_FIGURE_KEYWORDS = (
    "overview",
    "architecture",
    "framework",
    "pipeline",
    "method",
    "system",
    "workflow",
    "teaser",
    "illustration",
    "model overview",
    "overall",
)
SECONDARY_FIGURE_KEYWORDS = ("approach", "design", "training", "inference")

REQUIRED_REVIEW_FIELDS = (
    "title_zh",
    "summary_en",
    "summary_zh",
    "why_read",
    "priority",
    "group_profile",
    "reviews",
    "ai_writing_assessment",
    "contribution_diagram",
    "claim_evidence",
    "novelty_boundary",
    "evaluation_validity",
    "reproducibility",
    "artifact_risks",
    "limitations",
    "next_steps",
)

AI_WRITING_RUBRIC_VERSION = "AI-writing-signals-v1.0"
AI_WRITING_INDICATORS: Tuple[Dict[str, str], ...] = (
    {
        "id": "chatbot_residue",
        "name": "对话界面残留 / Chatbot residue",
        "family": "direct_artifact",
    },
    {
        "id": "lexical_overrepresentation",
        "name": "特征词过度集中 / Lexical overrepresentation",
        "family": "lexical_style",
    },
    {
        "id": "formulaic_scaffolding",
        "name": "模板化衔接 / Formulaic scaffolding",
        "family": "discourse_style",
    },
    {
        "id": "template_repetition",
        "name": "句式与结构重复 / Template repetition",
        "family": "discourse_style",
    },
    {
        "id": "rhythm_uniformity",
        "name": "句段节奏同质化 / Rhythm uniformity",
        "family": "stylometry",
    },
    {
        "id": "terminology_notation_drift",
        "name": "术语与符号漂移 / Terminology/notation drift",
        "family": "cross_section_consistency",
    },
    {
        "id": "citation_context_anomaly",
        "name": "引文语境异常 / Citation-context anomaly",
        "family": "evidence_integrity",
    },
    {
        "id": "claim_evidence_miscalibration",
        "name": "主张—证据失配 / Claim–evidence miscalibration",
        "family": "evidence_integrity",
    },
)
AI_WRITING_INDICATOR_MAP = {item["id"]: item for item in AI_WRITING_INDICATORS}
AI_WRITING_SECTION_IDS = (
    "abstract",
    "introduction",
    "method",
    "experiments",
    "limitations_or_conclusion",
    "ai_use_disclosure",
)

WRITING_LABEL_ZH = {
    "disclosed": "已披露",
    "low": "低",
    "medium": "中",
    "high": "高",
    "insufficient_evidence": "证据不足",
}
CONFIDENCE_ZH = {"High": "高", "Medium": "中", "Low": "低"}
PRIORITY_ZH = {"High": "高优先级", "Medium": "中优先级", "Low": "低优先级"}
DEFAULT_TOPIC_LABELS = {
    "ai_infra": "AI 基础设施 · 主主题",
    "hpc_systems": "HPC 系统 · 次主题",
    "ai4sci_infra": "AI4Sci 基础设施 · 扩展主题",
    "other": "相关主题",
}
SECTION_ZH = {
    "abstract": "摘要",
    "introduction": "引言",
    "method": "方法",
    "experiments": "实验",
    "limitations_or_conclusion": "局限或结论",
    "ai_use_disclosure": "AI 使用声明",
}
STATUS_ZH = {
    "reviewed": "已审阅",
    "not_present": "未设置该节",
    "unreadable": "无法可靠读取",
    "disclosed": "已披露",
    "searched_not_found": "已检索但未发现",
}
FAMILY_ZH = {
    "direct_artifact": "直接残留",
    "lexical_style": "词汇风格",
    "discourse_style": "篇章风格",
    "stylometry": "文体统计",
    "cross_section_consistency": "跨章节一致性",
    "evidence_integrity": "证据完整性",
}


class PipelineError(RuntimeError):
    """Raised when a report cannot be built without violating its contract."""


def normalize_arxiv_id(value: Any) -> str:
    """Return a normalized arXiv id, retaining a version suffix when present."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    raw = re.sub(r"^https?://[^/]+/(?:abs|pdf|html)/", "", raw, flags=re.I)
    raw = re.sub(r"\.pdf$", "", raw, flags=re.I)
    match = re.search(r"(\d{4}\.\d{4,5}(?:v\d+)?)$", raw)
    return match.group(1) if match else ""


def base_arxiv_id(value: Any) -> str:
    return re.sub(r"v\d+$", "", normalize_arxiv_id(value))


def validate_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise PipelineError(f"invalid date {value!r}; expected YYYY-MM-DD") from exc


def date_slug(value: str) -> str:
    return validate_date(value).replace("-", "")


def safe_slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned[:96] or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def load_json(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise PipelineError(f"JSON file exceeds {MAX_JSON_BYTES} bytes: {path}")
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PipelineError(f"required JSON file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PipelineError(f"invalid JSON in {path}: {exc}") from exc


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def require_path_within(
    root: Path,
    value: Any,
    label: str,
    *,
    must_exist: bool = True,
) -> Path:
    """Resolve a path below root while rejecting traversal and symbolic links."""
    root_lexical = Path(os.path.abspath(root.expanduser()))
    if root_lexical.is_symlink():
        raise PipelineError(f"report root must not be a symbolic link: {root_lexical}")
    root_resolved = root_lexical.resolve(strict=True)
    candidate = Path(str(value or ""))
    if not candidate.is_absolute():
        candidate = root_lexical / candidate
    candidate = Path(os.path.abspath(candidate))
    if candidate.is_symlink():
        raise PipelineError(f"{label} must not use a symbolic link: {candidate}")
    try:
        resolved = candidate.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise PipelineError(f"{label} does not exist: {candidate}") from exc
    try:
        relative = resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PipelineError(f"{label} is outside report root: {resolved}") from exc
    current = root_resolved
    for component in relative.parts[:-1]:
        current = current / component
        if current.is_symlink():
            raise PipelineError(f"{label} must not use a symbolic link: {current}")
    return resolved


def require_arxiv_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise PipelineError(f"refusing malformed arXiv asset URL: {url}") from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not (host == "arxiv.org" or host.endswith(".arxiv.org"))
    ):
        raise PipelineError(f"refusing non-arXiv asset URL: {url}")


def _set_native_resource_limits(memory_bytes: int, cpu_seconds: int) -> None:
    try:
        import resource

        limits = (
            (resource.RLIMIT_AS, (memory_bytes, memory_bytes)),
            (resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds)),
            (resource.RLIMIT_FSIZE, (MAX_NATIVE_FILE_BYTES, MAX_NATIVE_FILE_BYTES)),
            (resource.RLIMIT_NOFILE, (128, 128)),
        )
        for resource_id, value in limits:
            try:
                resource.setrlimit(resource_id, value)
            except (OSError, ValueError):
                continue
    except ImportError:
        return


def run_native_command(
    command: Sequence[str],
    *,
    timeout: int,
    cwd: Path,
    memory_bytes: int = 2 * 1024 * 1024 * 1024,
) -> None:
    executable = Path(command[0])
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise PipelineError(f"native executable is unavailable: {executable}")
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            preexec_fn=lambda: _set_native_resource_limits(
                memory_bytes,
                max(1, min(timeout, 180)),
            ),
        )
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise PipelineError(f"native command timed out after {timeout}s: {executable.name}") from exc
        if return_code != 0:
            stderr.seek(0)
            detail = stderr.read(MAX_NATIVE_LOG_BYTES).decode("utf-8", "replace").strip()
            raise PipelineError(
                f"native command failed ({return_code}): {executable.name}: {detail[:2000]}"
            )


def fetch_limited(
    session: requests.Session,
    url: str,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> Tuple[bytes, str, str]:
    current_url = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        require_arxiv_url(current_url)
        with session.get(
            current_url,
            stream=True,
            timeout=(15, 90),
            allow_redirects=False,
        ) as response:
            status_code = int(getattr(response, "status_code", 200))
            if status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise PipelineError(f"redirect contained no location: {current_url}")
                next_url = urljoin(current_url, location)
                require_arxiv_url(next_url)
                current_url = next_url
                continue

            response.raise_for_status()
            final_url = str(getattr(response, "url", current_url) or current_url)
            require_arxiv_url(final_url)
            announced = response.headers.get("content-length")
            if announced:
                try:
                    announced_bytes = int(announced)
                except ValueError as exc:
                    raise PipelineError(f"invalid content-length from {final_url}") from exc
                if announced_bytes > max_bytes:
                    raise PipelineError(f"download exceeds {max_bytes} bytes: {final_url}")
            chunks: List[bytes] = []
            total = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise PipelineError(f"download exceeds {max_bytes} bytes: {final_url}")
                chunks.append(chunk)
            payload = b"".join(chunks)
            if REQUEST_DELAY_SECONDS:
                time.sleep(REQUEST_DELAY_SECONDS)
            return payload, response.headers.get("content-type", "").split(";", 1)[0].strip().lower(), final_url

    raise PipelineError(f"too many redirects while fetching {url}")


def sanitize_svg(data: bytes) -> bytes:
    """Remove executable/external SVG content while preserving vector geometry."""
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.I):
        raise PipelineError("SVG DOCTYPE and ENTITY declarations are not allowed")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    root = etree.fromstring(data, parser=parser)
    elements = list(root.iter())
    if len(elements) > MAX_SVG_ELEMENTS:
        raise PipelineError(f"SVG exceeds {MAX_SVG_ELEMENTS} elements")
    forbidden = {"script", "foreignObject", "style", "iframe", "object", "embed"}
    for element in elements:
        local_name = etree.QName(element).localname
        if local_name in forbidden:
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
            continue
        for attribute in list(element.attrib):
            local_attr = etree.QName(attribute).localname.lower()
            value = element.attrib.get(attribute, "").strip()
            if local_attr.startswith("on"):
                del element.attrib[attribute]
            elif local_attr in {"href", "src"} and not value.startswith("#"):
                del element.attrib[attribute]
            elif re.search(r"(?:javascript:|data:|@import|url\s*\()", value, re.I):
                del element.attrib[attribute]
    return etree.tostring(root, encoding="utf-8", xml_declaration=True)


def raster_metadata(data: bytes) -> Tuple[str, int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=list(ALLOWED_RASTER_FORMATS)) as image:
                width, height = image.size
                image_format = (image.format or "").upper()
                frames = int(getattr(image, "n_frames", 1))
                if frames != 1:
                    raise PipelineError(f"animated or multi-frame images are not allowed ({frames} frames)")
                if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                    raise PipelineError(f"image dimensions exceed {MAX_IMAGE_DIMENSION}: {width}x{height}")
                if width * height > MAX_IMAGE_PIXELS:
                    raise PipelineError(f"image exceeds {MAX_IMAGE_PIXELS} pixels: {width}x{height}")
                image.verify()
    except PipelineError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise PipelineError(f"image decompression limit exceeded: {exc}") from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise PipelineError(f"unsupported or invalid raster image: {exc}") from exc
    if width < 180 or height < 100:
        raise PipelineError(f"image is too small to be a paper figure: {width}x{height}")
    extension_map = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif", "TIFF": "tiff"}
    extension = extension_map.get(image_format)
    if not extension:
        raise PipelineError(f"unsupported raster image format: {image_format.lower() or 'unknown'}")
    return extension, width, height


def image_payload(data: bytes, content_type: str) -> Tuple[bytes, str, Optional[int], Optional[int]]:
    is_svg = content_type == "image/svg+xml" or b"<svg" in data[:500]
    if is_svg:
        return sanitize_svg(data), "svg", None, None
    extension, width, height = raster_metadata(data)
    return data, extension, width, height


def clean_text(parts: Iterable[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip()


def figure_candidates(page: bytes, page_url: str) -> List[Dict[str, Any]]:
    document = lxml_html.fromstring(page, base_url=page_url)
    candidates: List[Dict[str, Any]] = []
    figures = document.xpath("//figure")
    for index, figure in enumerate(figures):
        caption = clean_text(figure.xpath(".//figcaption//text()"))
        caption_lower = caption.lower()
        base_score = max(0, 8 - index)
        if any(keyword in caption_lower for keyword in MAIN_FIGURE_KEYWORDS):
            base_score += 24
        if any(keyword in caption_lower for keyword in SECONDARY_FIGURE_KEYWORDS):
            base_score += 8
        if any(keyword in caption_lower for keyword in ("ablation", "accuracy", "results on", "comparison")):
            base_score -= 4
        for image_index, image in enumerate(figure.xpath(".//img")):
            source = image.get("src") or image.get("data-src")
            if not source or source.startswith("data:"):
                continue
            alt = clean_text([image.get("alt", "")])
            candidates.append(
                {
                    "url": urljoin(page_url, source),
                    "caption": caption or alt or f"Figure {index + 1}",
                    "score": base_score - image_index,
                    "figure_index": index,
                }
            )
    candidates.sort(key=lambda item: (-item["score"], item["figure_index"]))
    return candidates


def download_pdf(session: requests.Session, arxiv_id: str, paper_dir: Path) -> Path:
    pdf_path = paper_dir / "paper.pdf"
    if pdf_path.is_symlink():
        raise PipelineError(f"cached PDF must not be a symbolic link: {pdf_path}")
    if pdf_path.exists() and pdf_path.is_file() and pdf_path.stat().st_size > 1024:
        return pdf_path
    data, content_type, _ = fetch_limited(session, f"https://arxiv.org/pdf/{arxiv_id}")
    if not data.startswith(b"%PDF") and content_type != "application/pdf":
        raise PipelineError(f"arXiv PDF endpoint returned {content_type or 'unknown content'}")
    atomic_write_bytes(pdf_path, data)
    return pdf_path


def extract_pdf_text(pdf_path: Path, paper_dir: Path) -> Path:
    text_path = paper_dir / "paper.txt"
    if text_path.is_symlink():
        raise PipelineError(f"text output must not be a symbolic link: {text_path}")
    command = [PDFTOTEXT_BIN, "-layout", str(pdf_path), str(text_path)]
    run_native_command(command, timeout=180, cwd=paper_dir)
    if not text_path.exists() or text_path.stat().st_size < 100:
        raise PipelineError(f"pdftotext produced no usable text for {pdf_path}")
    os.chmod(text_path, 0o600)
    return text_path


def extract_analysis_text(pdf_path: Path, paper_dir: Path) -> Path:
    """Create a reading-order-oriented extraction for descriptive text metrics."""
    text_path = paper_dir / "paper_analysis.txt"
    if text_path.is_symlink():
        raise PipelineError(f"analysis output must not be a symbolic link: {text_path}")
    command = [
        PDFTOTEXT_BIN,
        "-nopgbrk",
        "-enc",
        "UTF-8",
        str(pdf_path),
        str(text_path),
    ]
    run_native_command(command, timeout=180, cwd=paper_dir)
    if not text_path.exists() or text_path.stat().st_size < 100:
        raise PipelineError(f"pdftotext produced no usable analysis text for {pdf_path}")
    os.chmod(text_path, 0o600)
    return text_path


def create_ai_writing_metrics(text_path: Path, paper_dir: Path) -> Path:
    metrics_path = paper_dir / "ai_writing_metrics.json"
    return write_metrics_json(analyze_file(text_path), metrics_path)


def extract_main_figure(
    session: requests.Session,
    arxiv_id: str,
    paper_dir: Path,
    pdf_path: Optional[Path],
) -> Dict[str, Any]:
    html_url = f"https://arxiv.org/html/{arxiv_id}"
    html_error = ""
    try:
        page, content_type, final_url = fetch_limited(session, html_url, max_bytes=15 * 1024 * 1024)
        if "html" not in content_type and b"<html" not in page[:2000].lower():
            raise PipelineError(f"arXiv HTML endpoint returned {content_type or 'unknown content'}")
        for candidate in figure_candidates(page, final_url)[:16]:
            try:
                raw, figure_type, resolved_url = fetch_limited(session, candidate["url"], max_bytes=20 * 1024 * 1024)
                payload, extension, width, height = image_payload(raw, figure_type)
                output_path = paper_dir / f"main_figure.{extension}"
                atomic_write_bytes(output_path, payload)
                return {
                    "path": str(output_path),
                    "kind": "paper_main_figure",
                    "caption": candidate["caption"],
                    "source_url": resolved_url,
                    "html_source_url": final_url,
                    "mime_type": mimetypes.guess_type(output_path.name)[0] or figure_type,
                    "width": width,
                    "height": height,
                }
            except Exception:
                continue
        raise PipelineError("no trustworthy figure image found in arXiv HTML")
    except Exception as exc:
        html_error = str(exc)

    if pdf_path is None:
        raise PipelineError(f"main figure extraction failed and no PDF fallback is available: {html_error}")

    prefix = paper_dir / "first_page_preview"
    output_path = prefix.with_suffix(".png")
    if output_path.is_symlink():
        raise PipelineError(f"preview output must not be a symbolic link: {output_path}")
    run_native_command(
        [PDFTOPPM_BIN, "-f", "1", "-l", "1", "-singlefile", "-png", "-r", "120", str(pdf_path), str(prefix)],
        timeout=180,
        cwd=paper_dir,
    )
    if not output_path.exists() or output_path.stat().st_size < 1024:
        raise PipelineError(f"failed to create first-page preview: {html_error}")
    os.chmod(output_path, 0o600)
    with Image.open(output_path) as image:
        width, height = image.size
    return {
        "path": str(output_path),
        "kind": "first_page_preview",
        "caption": (
            "未可靠提取主图，展示首页预览 / "
            "Main figure unavailable; first-page preview shown"
        ),
        "source_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "html_source_url": html_url,
        "mime_type": "image/png",
        "width": width,
        "height": height,
        "fallback_reason": html_error,
    }


def writing_section_template() -> List[Dict[str, str]]:
    return [
        {"section": section_id, "status": "", "location": ""}
        for section_id in AI_WRITING_SECTION_IDS
    ]


def writing_scorecard_template() -> List[Dict[str, Any]]:
    return [
        {
            "id": indicator["id"],
            "score": None,
            "locations": [],
            "evidence": "",
            "counter_evidence": "",
        }
        for indicator in AI_WRITING_INDICATORS
    ]


def writing_assessment_template(paper: Dict[str, Any]) -> Dict[str, Any]:
    metrics_path = str(paper.get("ai_writing_metrics_path") or "")
    text_path = str(paper.get("analysis_text_path") or paper.get("text_path") or "")
    analyzed_word_count = 0
    if metrics_path:
        try:
            metrics = load_json(Path(metrics_path))
            analyzed_word_count = int((metrics.get("document_counts") or {}).get("words") or 0)
        except (PipelineError, TypeError, ValueError):
            analyzed_word_count = 0
    unavailable = "公开信息不足 / insufficient public evidence"
    return {
        "label": "",
        "confidence": "",
        "disclosure": "",
        "scope": {
            "analysis_text_path": text_path or unavailable,
            "automatic_metrics_path": metrics_path or unavailable,
            "analyzed_word_count": analyzed_word_count,
            "section_checks": writing_section_template(),
            "coverage": "",
            "coverage_note": "",
        },
        "indicator_scorecard": writing_scorecard_template(),
        "aggregate": {
            "rubric_version": AI_WRITING_RUBRIC_VERSION,
            "score_total": None,
            "positive_indicators": None,
            "positive_families": [],
            "positive_sections": [],
            "rationale": "",
        },
        "evidence_for": [],
        "counter_evidence": [],
        "confounders": [],
        "caveat": "",
    }


def prepare_assets(input_path: Path, report_date: str, report_root: Path, limit: Optional[int] = None) -> Path:
    payload = load_json(input_path)
    topic_labels: Dict[str, str] = {}
    if isinstance(payload, dict):
        raw_labels = payload.get("topic_labels") or {}
        if isinstance(raw_labels, dict):
            topic_labels = {str(key): str(value) for key, value in raw_labels.items()}
        papers = payload.get("papers")
    else:
        papers = payload
    if not isinstance(papers, list):
        raise PipelineError(
            f"{input_path} must contain a JSON array or an object with a papers array"
        )
    if limit is not None and limit > MAX_PAPERS_PER_REPORT:
        raise PipelineError(f"paper limit exceeds {MAX_PAPERS_PER_REPORT}")
    if limit is not None:
        papers = papers[: max(0, limit)]
    elif len(papers) > MAX_PAPERS_PER_REPORT:
        raise PipelineError(
            f"input contains {len(papers)} papers; maximum is {MAX_PAPERS_PER_REPORT}"
        )

    report_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if report_root.is_symlink():
        raise PipelineError(f"report root must not be a symbolic link: {report_root}")
    report_root = report_root.resolve(strict=True)
    os.chmod(report_root, 0o700)
    report_dir = report_root / date_slug(report_date)
    assets_dir = report_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if report_dir.is_symlink() or assets_dir.is_symlink():
        raise PipelineError("report directories must not be symbolic links")
    os.chmod(report_dir, 0o700)
    os.chmod(assets_dir, 0o700)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,image/*;q=0.9,*/*;q=0.1"})

    manifest: Dict[str, Any] = {
        "schema_version": 2,
        "date": validate_date(report_date),
        "source_input": str(input_path.resolve()),
        "report_dir": str(report_dir.resolve()),
        "topic_labels": topic_labels,
        "papers": [],
    }

    for index, paper in enumerate(papers, start=1):
        if not isinstance(paper, dict):
            continue
        arxiv_id = normalize_arxiv_id(paper.get("url") or paper.get("arxiv_id"))
        entry: Dict[str, Any] = {
            "index": index,
            "arxiv_id": arxiv_id,
            "base_arxiv_id": base_arxiv_id(arxiv_id),
            "title": str(paper.get("title") or "Untitled"),
            "url": str(paper.get("url") or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "")),
            "categories": paper.get("categories") or [],
            "authors": paper.get("authors") or [],
            "relevance_score": paper.get("relevance_score"),
            "primary_topic": paper.get("primary_topic") or "other",
            "score_details": paper.get("score_details") or {},
            "code_url": paper.get("code_url"),
            "errors": [],
        }
        if not arxiv_id:
            entry["errors"].append("missing or invalid arXiv id")
            manifest["papers"].append(entry)
            continue

        paper_dir = assets_dir / safe_slug(arxiv_id)
        paper_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if paper_dir.is_symlink():
            raise PipelineError(f"paper directory must not be a symbolic link: {paper_dir}")
        os.chmod(paper_dir, 0o700)
        entry["paper_dir"] = str(paper_dir.resolve())
        pdf_path: Optional[Path] = None
        try:
            pdf_path = download_pdf(session, arxiv_id, paper_dir)
            entry["pdf_path"] = str(pdf_path.resolve())
        except Exception as exc:
            entry["errors"].append(f"PDF: {exc}")

        if pdf_path is not None:
            try:
                text_path = extract_pdf_text(pdf_path, paper_dir)
                entry["text_path"] = str(text_path.resolve())
            except Exception as exc:
                entry["errors"].append(f"text: {exc}")
            try:
                analysis_text_path = extract_analysis_text(pdf_path, paper_dir)
                entry["analysis_text_path"] = str(analysis_text_path.resolve())
                metrics_path = create_ai_writing_metrics(analysis_text_path, paper_dir)
                entry["ai_writing_metrics_path"] = str(metrics_path.resolve())
            except Exception as exc:
                entry["errors"].append(f"AI-writing metrics: {exc}")

        try:
            entry["main_figure"] = extract_main_figure(session, arxiv_id, paper_dir, pdf_path)
        except Exception as exc:
            entry["main_figure"] = None
            entry["errors"].append(f"main figure: {exc}")
        manifest["papers"].append(entry)

    reviews_template_path = report_dir / "reviews.template.json"
    reviews_template = {
        "schema_version": 2,
        "date": validate_date(report_date),
        "run_summary": {
            "total_fetched": 0,
            "recommended": len(manifest["papers"]),
            "topic_counts": {},
            "top3_arxiv_ids": [],
        },
        "papers": [
            {
                "arxiv_id": item.get("arxiv_id", ""),
                "title_zh": "",
                "summary_en": "",
                "summary_zh": "",
                "why_read": "",
                "priority": "",
                "group_profile": {
                    "lead_groups": "",
                    "key_people": "",
                    "focus": "",
                    "prior_work": "",
                    "why_follow": "",
                    "evidence_confidence": "",
                },
                "reviews": {"systems": "", "ai4sci": "", "research_value": ""},
                "ai_writing_assessment": writing_assessment_template(item),
                "contribution_diagram": {"problem": "", "approach": "", "mechanism": "", "evidence": ""},
                "claim_evidence": [],
                "novelty_boundary": "",
                "evaluation_validity": "",
                "reproducibility": {
                    "code": "",
                    "data_models": "",
                    "license": "",
                    "requirements": "",
                    "effort": "",
                    "smallest_test": "",
                },
                "artifact_risks": [],
                "limitations": [],
                "next_steps": [],
            }
            for item in manifest["papers"]
        ],
    }
    manifest["reviews_template_path"] = str(reviews_template_path.resolve())
    atomic_write_text(reviews_template_path, json.dumps(reviews_template, ensure_ascii=False, indent=2))

    manifest_path = report_dir / "assets_manifest.json"
    atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest_path


def nonempty(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")


def collect_narrative_text(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        collected: List[str] = []
        for item in value:
            collected.extend(collect_narrative_text(item))
        return collected
    if isinstance(value, dict):
        collected = []
        for item in value.values():
            collected.extend(collect_narrative_text(item))
        return collected
    return []


def validate_chinese_primary_narrative(
    identifier: str,
    review: Dict[str, Any],
    errors: List[str],
) -> None:
    prefix = f"{identifier}: Chinese-primary narrative"
    required_cjk = {
        "title_zh": (review.get("title_zh"), 2),
        "summary_zh": (review.get("summary_zh"), 40),
        "why_read": (review.get("why_read"), 12),
    }
    perspectives = review.get("reviews") or {}
    for field in ("systems", "ai4sci", "research_value"):
        required_cjk[f"reviews.{field}"] = (perspectives.get(field), 12)
    contribution = review.get("contribution_diagram") or {}
    for field in ("problem", "approach", "mechanism", "evidence"):
        required_cjk[f"contribution_diagram.{field}"] = (contribution.get(field), 4)
    for field, (value, minimum) in required_cjk.items():
        count = len(CJK_RE.findall(str(value or "")))
        if count < minimum:
            errors.append(f"{prefix} requires at least {minimum} Chinese characters in {field}")

    writing = review.get("ai_writing_assessment") or {}
    scope = writing.get("scope") or {}
    aggregate = writing.get("aggregate") or {}
    scorecard = writing.get("indicator_scorecard") or []
    writing_narrative = {
        "disclosure": writing.get("disclosure"),
        "evidence_for": writing.get("evidence_for"),
        "counter_evidence": writing.get("counter_evidence"),
        "confounders": writing.get("confounders"),
        "caveat": writing.get("caveat"),
        "coverage_note": scope.get("coverage_note"),
        "rationale": aggregate.get("rationale"),
        "scorecard": [
            {
                "evidence": item.get("evidence"),
                "counter_evidence": item.get("counter_evidence"),
            }
            for item in scorecard
            if isinstance(item, dict)
        ],
    }
    narrative_values = [
        review.get("title_zh"),
        review.get("summary_zh"),
        review.get("why_read"),
        review.get("group_profile"),
        review.get("reviews"),
        writing_narrative,
        review.get("contribution_diagram"),
        review.get("claim_evidence"),
        review.get("novelty_boundary"),
        review.get("evaluation_validity"),
        review.get("reproducibility"),
        review.get("artifact_risks"),
        review.get("limitations"),
        review.get("next_steps"),
    ]
    narrative = "\n".join(
        part
        for value in narrative_values
        for part in collect_narrative_text(value)
        if part
    )
    cjk_characters = len(CJK_RE.findall(narrative))
    latin_words = len(LATIN_WORD_RE.findall(narrative))
    if cjk_characters < 120:
        errors.append(f"{prefix} requires at least 120 Chinese characters across narrative fields")
    if cjk_characters < math.ceil(1.5 * latin_words):
        errors.append(
            f"{prefix} is English-dominant ({cjk_characters} Chinese characters vs {latin_words} Latin words)"
        )


def automatic_metrics_word_count(paper: Dict[str, Any]) -> int:
    metrics_path = paper.get("ai_writing_metrics_path")
    if not metrics_path:
        return 0
    try:
        metrics = load_json(Path(str(metrics_path)))
        return int((metrics.get("document_counts") or {}).get("words") or 0)
    except (PipelineError, TypeError, ValueError):
        return 0


def validate_ai_writing_v2(
    identifier: str,
    paper: Dict[str, Any],
    writing: Dict[str, Any],
    errors: List[str],
) -> None:
    prefix = f"{identifier}: ai_writing_assessment"
    if writing.get("confidence") not in {"High", "Medium", "Low"}:
        errors.append(f"{prefix}.confidence must be High, Medium, or Low")
    for field in ("disclosure", "evidence_for", "counter_evidence", "confounders", "caveat"):
        if not nonempty(writing.get(field)):
            errors.append(f"{prefix}.{field} is empty")

    scope = writing.get("scope") or {}
    for field in ("analysis_text_path", "automatic_metrics_path", "coverage_note"):
        if not nonempty(scope.get(field)):
            errors.append(f"{prefix}.scope.{field} is empty")
    expected_words = automatic_metrics_word_count(paper)
    analyzed_words = scope.get("analyzed_word_count")
    if not isinstance(analyzed_words, int) or isinstance(analyzed_words, bool) or analyzed_words < 0:
        errors.append(f"{prefix}.scope.analyzed_word_count must be a non-negative integer")
        analyzed_words = 0
    elif analyzed_words != expected_words:
        errors.append(
            f"{prefix}.scope.analyzed_word_count={analyzed_words} does not match automatic metrics {expected_words}"
        )

    checks = scope.get("section_checks")
    check_map: Dict[str, Dict[str, Any]] = {}
    if not isinstance(checks, list):
        errors.append(f"{prefix}.scope.section_checks must be an array")
        checks = []
    for check in checks:
        if not isinstance(check, dict):
            errors.append(f"{prefix}.scope.section_checks contains a non-object")
            continue
        section_id = str(check.get("section") or "")
        if section_id in check_map:
            errors.append(f"{prefix}.scope.section_checks duplicates {section_id}")
            continue
        check_map[section_id] = check
    missing_sections = set(AI_WRITING_SECTION_IDS) - set(check_map)
    extra_sections = set(check_map) - set(AI_WRITING_SECTION_IDS)
    if missing_sections:
        errors.append(f"{prefix}.scope.section_checks missing {sorted(missing_sections)}")
    if extra_sections:
        errors.append(f"{prefix}.scope.section_checks has unknown sections {sorted(extra_sections)}")

    reviewed_content = 0
    for section_id in AI_WRITING_SECTION_IDS:
        check = check_map.get(section_id) or {}
        status = check.get("status")
        allowed = (
            {"disclosed", "searched_not_found", "unreadable"}
            if section_id == "ai_use_disclosure"
            else {"reviewed", "not_present", "unreadable"}
        )
        if status not in allowed:
            errors.append(f"{prefix}.scope.section_checks[{section_id}] has invalid status")
        if not nonempty(check.get("location")):
            errors.append(f"{prefix}.scope.section_checks[{section_id}].location is empty")
        if section_id != "ai_use_disclosure" and status == "reviewed":
            reviewed_content += 1

    disclosure_status = (check_map.get("ai_use_disclosure") or {}).get("status")
    coverage_sufficient = (
        analyzed_words >= 1500
        and (check_map.get("abstract") or {}).get("status") == "reviewed"
        and (check_map.get("introduction") or {}).get("status") == "reviewed"
        and reviewed_content >= 4
        and disclosure_status in {"disclosed", "searched_not_found"}
    )
    expected_coverage = "sufficient" if coverage_sufficient else "insufficient"
    if scope.get("coverage") != expected_coverage:
        errors.append(
            f"{prefix}.scope.coverage must be {expected_coverage} for the recorded word/section coverage"
        )

    scorecard = writing.get("indicator_scorecard")
    score_map: Dict[str, Dict[str, Any]] = {}
    if not isinstance(scorecard, list):
        errors.append(f"{prefix}.indicator_scorecard must be an array")
        scorecard = []
    for item in scorecard:
        if not isinstance(item, dict):
            errors.append(f"{prefix}.indicator_scorecard contains a non-object")
            continue
        indicator_id = str(item.get("id") or "")
        if indicator_id in score_map:
            errors.append(f"{prefix}.indicator_scorecard duplicates {indicator_id}")
            continue
        score_map[indicator_id] = item
    missing_indicators = set(AI_WRITING_INDICATOR_MAP) - set(score_map)
    extra_indicators = set(score_map) - set(AI_WRITING_INDICATOR_MAP)
    if missing_indicators:
        errors.append(f"{prefix}.indicator_scorecard missing {sorted(missing_indicators)}")
    if extra_indicators:
        errors.append(f"{prefix}.indicator_scorecard has unknown indicators {sorted(extra_indicators)}")

    total = 0
    positive_ids: List[str] = []
    positive_location_values: List[str] = []
    for indicator_id in AI_WRITING_INDICATOR_MAP:
        item = score_map.get(indicator_id) or {}
        score = item.get("score")
        if not isinstance(score, int) or isinstance(score, bool) or score not in {0, 1, 2}:
            errors.append(f"{prefix}.indicator_scorecard[{indicator_id}].score must be 0, 1, or 2")
            score = 0
        total += score
        if score > 0:
            positive_ids.append(indicator_id)
        locations = item.get("locations")
        if not isinstance(locations, list) or not locations or not all(nonempty(value) for value in locations):
            errors.append(f"{prefix}.indicator_scorecard[{indicator_id}].locations must be non-empty")
        elif score > 0:
            positive_location_values.extend(str(value) for value in locations)
        for field in ("evidence", "counter_evidence"):
            if not nonempty(item.get(field)):
                errors.append(f"{prefix}.indicator_scorecard[{indicator_id}].{field} is empty")

    positive_families = sorted(
        {AI_WRITING_INDICATOR_MAP[indicator_id]["family"] for indicator_id in positive_ids}
    )
    aggregate = writing.get("aggregate") or {}
    if aggregate.get("rubric_version") != AI_WRITING_RUBRIC_VERSION:
        errors.append(f"{prefix}.aggregate.rubric_version must be {AI_WRITING_RUBRIC_VERSION}")
    if aggregate.get("score_total") != total:
        errors.append(f"{prefix}.aggregate.score_total must equal scorecard sum {total}")
    if aggregate.get("positive_indicators") != len(positive_ids):
        errors.append(
            f"{prefix}.aggregate.positive_indicators must equal {len(positive_ids)}"
        )
    supplied_families = aggregate.get("positive_families")
    if not isinstance(supplied_families, list) or sorted(set(supplied_families)) != positive_families:
        errors.append(f"{prefix}.aggregate.positive_families must equal {positive_families}")
    positive_sections = aggregate.get("positive_sections")
    if not isinstance(positive_sections, list) or not all(nonempty(value) for value in positive_sections):
        errors.append(f"{prefix}.aggregate.positive_sections must be an array of non-empty locations")
        positive_sections = []
    expected_positive_sections = sorted(set(positive_location_values))
    if sorted(set(str(value) for value in positive_sections)) != expected_positive_sections:
        errors.append(
            f"{prefix}.aggregate.positive_sections must equal positive scorecard locations {expected_positive_sections}"
        )
    if not nonempty(aggregate.get("rationale")):
        errors.append(f"{prefix}.aggregate.rationale is empty")

    label = writing.get("label")
    if label not in {"disclosed", "low", "medium", "high", "insufficient_evidence"}:
        errors.append(f"{prefix}.label is invalid")
        return
    if disclosure_status == "disclosed" and label != "disclosed":
        errors.append(f"{prefix}.label must be disclosed when explicit disclosure is recorded")
    elif label == "disclosed" and disclosure_status != "disclosed":
        errors.append(f"{prefix}.label disclosed requires an explicit disclosure record")
    elif label != "disclosed":
        if not coverage_sufficient:
            expected_label = "insufficient_evidence"
        elif total <= 3:
            expected_label = "low"
        elif total <= 7:
            expected_label = "medium"
        else:
            high_gate = (
                len(positive_ids) >= 3
                and len(positive_families) >= 3
                and len(expected_positive_sections) >= 3
            )
            expected_label = "high" if high_gate else "medium"
        if label != expected_label:
            errors.append(
                f"{prefix}.label must be {expected_label} under rubric score/coverage gates"
            )
    if writing.get("confidence") == "High" and not coverage_sufficient and label != "disclosed":
        errors.append(f"{prefix}.confidence cannot be High with insufficient coverage")


def validate_review_payload(manifest: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    schema_version = payload.get("schema_version")
    if schema_version not in {1, 2}:
        raise PipelineError("reviews.json schema_version must be 1 or 2")
    if manifest.get("schema_version") == 2 and schema_version != 2:
        raise PipelineError("schema_version 2 assets require schema_version 2 reviews")
    reviews = payload.get("papers")
    if not isinstance(reviews, list):
        raise PipelineError("reviews.json papers must be an array")

    review_map: Dict[str, Dict[str, Any]] = {}
    for review in reviews:
        if not isinstance(review, dict):
            continue
        identifier = base_arxiv_id(review.get("arxiv_id") or review.get("url"))
        if not identifier:
            raise PipelineError("review entry has no valid arxiv_id")
        if identifier in review_map:
            raise PipelineError(f"duplicate review entry for {identifier}")
        review_map[identifier] = review

    errors: List[str] = []
    for paper in manifest.get("papers", []):
        identifier = base_arxiv_id(paper.get("arxiv_id"))
        review = review_map.get(identifier)
        if review is None:
            errors.append(f"{identifier}: missing review")
            continue
        for field in REQUIRED_REVIEW_FIELDS:
            if not nonempty(review.get(field)):
                errors.append(f"{identifier}: empty required field {field}")
        if schema_version == 2:
            validate_chinese_primary_narrative(identifier, review, errors)

        group = review.get("group_profile") or {}
        for field in ("lead_groups", "key_people", "focus", "prior_work", "why_follow", "evidence_confidence"):
            if not nonempty(group.get(field)):
                errors.append(f"{identifier}: empty group_profile.{field}")

        perspectives = review.get("reviews") or {}
        for field in ("systems", "ai4sci", "research_value"):
            if not nonempty(perspectives.get(field)):
                errors.append(f"{identifier}: empty reviews.{field}")

        writing = review.get("ai_writing_assessment") or {}
        if schema_version == 2:
            validate_ai_writing_v2(identifier, paper, writing, errors)
        else:
            if writing.get("label") not in {"disclosed", "low", "medium", "high", "insufficient_evidence"}:
                errors.append(f"{identifier}: invalid ai_writing_assessment.label")
            for field in ("confidence", "disclosure", "evidence_for", "counter_evidence", "caveat"):
                if not nonempty(writing.get(field)):
                    errors.append(f"{identifier}: empty ai_writing_assessment.{field}")

        diagram = review.get("contribution_diagram") or {}
        for field in ("problem", "approach", "mechanism", "evidence"):
            if not nonempty(diagram.get(field)):
                errors.append(f"{identifier}: empty contribution_diagram.{field}")

        reproducibility = review.get("reproducibility") or {}
        for field in ("code", "data_models", "license", "requirements", "effort", "smallest_test"):
            if not nonempty(reproducibility.get(field)):
                errors.append(f"{identifier}: empty reproducibility.{field}")

    if errors:
        preview = "\n".join(f"- {item}" for item in errors[:40])
        suffix = f"\n- ... and {len(errors) - 40} more" if len(errors) > 40 else ""
        raise PipelineError(f"reviews.json failed validation:\n{preview}{suffix}")
    return review_map


def drawio_text(title: str, body: str) -> str:
    compact = re.sub(r"\s+", " ", str(body)).strip()
    escaped = html.escape(compact)
    # Long technical compounds such as ``roles/resources/barriers/pipelines``
    # otherwise overflow draw.io's XHTML label box. Keep the visible text
    # unchanged while giving the renderer safe line-break opportunities.
    for separator in ("/", "+", "·", ";", ","):
        escaped = escaped.replace(separator, f"{separator}&#8203;")
    return f"<b>{html.escape(title)}</b><br>{escaped}"


def write_drawio_contribution(diagram: Dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mxfile = ET.Element("mxfile", host="app.diagrams.net", agent="Daily Paper Brief")
    diagram_el = ET.SubElement(mxfile, "diagram", id="contribution", name="Contribution Map")
    model = ET.SubElement(
        diagram_el,
        "mxGraphModel",
        dx="760",
        dy="900",
        grid="1",
        gridSize="10",
        page="1",
        pageScale="1",
        pageWidth="827",
        pageHeight="1169",
        math="0",
        shadow="0",
    )
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")

    nodes = (
        ("problem", "问题 / Problem", "#F0F0F0", "#777777"),
        ("approach", "方法 / Approach", "#E7EDF4", "#4477AA"),
        ("mechanism", "关键机制 / Key mechanism", "#DFF3F8", "#3A9BB7"),
        ("evidence", "证据与影响 / Evidence", "#E4F1E8", "#228833"),
    )
    heights = [max(116, 66 + len(contribution_lines(diagram[key])) * 23) for key, *_ in nodes]
    y_positions = [20]
    for height in heights[:-1]:
        y_positions.append(y_positions[-1] + height + 30)
    model.set("pageHeight", str(max(1169, y_positions[-1] + heights[-1] + 40)))
    for index, ((key, title, fill, stroke), y_position) in enumerate(
        zip(nodes, y_positions), start=2
    ):
        style = (
            "rounded=1;whiteSpace=wrap;html=1;arcSize=6;"
            f"fillColor={fill};strokeColor={stroke};fontColor=#333333;"
            "fontSize=13;fontFamily=PingFang SC;verticalAlign=middle;align=left;"
            "spacingLeft=12;spacingRight=12;strokeWidth=1.5;"
        )
        cell = ET.SubElement(
            root,
            "mxCell",
            id=str(index),
            value=drawio_text(title, diagram[key]),
            style=style,
            vertex="1",
            parent="1",
        )
        ET.SubElement(
            cell,
            "mxGeometry",
            x="30",
            y=str(y_position),
            width="520",
            height=str(heights[index - 2]),
            **{"as": "geometry"},
        )

    for edge_index, (source, target) in enumerate(((2, 3), (3, 4), (4, 5)), start=10):
        edge = ET.SubElement(
            root,
            "mxCell",
            id=str(edge_index),
            style=(
                "edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;"
                "html=1;exitX=0.5;exitY=1;entryX=0.5;entryY=0;"
                "exitDx=0;exitDy=0;entryDx=0;entryDy=0;"
                "endArrow=block;endFill=1;strokeColor=#333333;strokeWidth=1.8;"
            ),
            edge="1",
            parent="1",
            source=str(source),
            target=str(target),
        )
        ET.SubElement(edge, "mxGeometry", relative="1", **{"as": "geometry"})

    xml_content = ET.tostring(mxfile, encoding="unicode")
    atomic_write_text(output_path, xml_content)


def contribution_lines(value: Any) -> List[str]:
    """Wrap mixed CJK/Latin text with conservative font-independent widths.

    Keep all content. The node grows vertically instead of silently clipping
    the fourth line or discarding characters beyond an arbitrary length.
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        text = "公开信息不足 / insufficient evidence"
    lines: List[str] = []
    line = ""
    width = 0.0
    for char in text:
        if unicodedata.combining(char):
            advance = 0.0
        elif unicodedata.east_asian_width(char) in {"W", "F"}:
            advance = 1.1
        elif char in "MW@%":
            advance = 1.1
        else:
            advance = 0.8
        if line and width + advance > 30:
            lines.append(line)
            line, width = "", 0.0
        line += char
        width += advance
    if line:
        lines.append(line)
    return lines


def write_contribution_svg(diagram: Dict[str, Any], svg_path: Path) -> None:
    """Render the portrait contribution map without invoking an SVG parser."""
    if svg_path.is_symlink():
        raise PipelineError(f"SVG output must not be a symbolic link: {svg_path}")
    namespace = "http://www.w3.org/2000/svg"
    ET.register_namespace("", namespace)
    wrapped = [contribution_lines(diagram.get(key)) for key in ("problem", "approach", "mechanism", "evidence")]
    heights = [max(116, 66 + len(lines) * 23) for lines in wrapped]
    y_positions = [18]
    for height in heights[:-1]:
        y_positions.append(y_positions[-1] + height + 38)
    canvas_height = max(650, y_positions[-1] + heights[-1] + 24)
    svg = ET.Element(
        f"{{{namespace}}}svg",
        viewBox=f"0 0 600 {canvas_height}",
        width="600",
        height=str(canvas_height),
        role="img",
        **{"aria-label": "Reviewer-generated contribution map"},
    )
    defs = ET.SubElement(svg, f"{{{namespace}}}defs")
    marker = ET.SubElement(
        defs,
        f"{{{namespace}}}marker",
        id="arrow",
        markerWidth="10",
        markerHeight="10",
        refX="8",
        refY="3",
        orient="auto",
        markerUnits="strokeWidth",
    )
    ET.SubElement(marker, f"{{{namespace}}}path", d="M0,0 L0,6 L9,3 z", fill="#333333")

    nodes = (
        ("problem", "问题 / Problem", "#F0F0F0", "#777777"),
        ("approach", "方法 / Approach", "#E7EDF4", "#4477AA"),
        ("mechanism", "关键机制 / Key mechanism", "#DFF3F8", "#3A9BB7"),
        ("evidence", "证据与影响 / Evidence", "#E4F1E8", "#228833"),
    )
    for index, ((key, title, fill, stroke), y_position) in enumerate(zip(nodes, y_positions)):
        ET.SubElement(
            svg,
            f"{{{namespace}}}rect",
            x="30",
            y=str(y_position),
            width="540",
            height=str(heights[index]),
            rx="8",
            fill=fill,
            stroke=stroke,
            **{"stroke-width": "2"},
        )
        title_text = ET.SubElement(
            svg,
            f"{{{namespace}}}text",
            x="50",
            y=str(y_position + 28),
            fill="#25282d",
            **{"font-family": "PingFang SC, Helvetica, Arial, sans-serif", "font-size": "15", "font-weight": "700"},
        )
        title_text.text = title
        body_text = ET.SubElement(
            svg,
            f"{{{namespace}}}text",
            x="50",
            y=str(y_position + 54),
            fill="#333333",
            **{"font-family": "PingFang SC, Helvetica, Arial, sans-serif", "font-size": "16"},
        )
        for line_index, line in enumerate(wrapped[index]):
            tspan = ET.SubElement(
                body_text,
                f"{{{namespace}}}tspan",
                x="50",
                dy="0" if line_index == 0 else "23",
            )
            tspan.text = line
        if index < len(nodes) - 1:
            ET.SubElement(
                svg,
                f"{{{namespace}}}line",
                x1="300",
                y1=str(y_position + heights[index]),
                x2="300",
                y2=str(y_positions[index + 1] - 10),
                stroke="#333333",
                **{"stroke-width": "2", "marker-end": "url(#arrow)"},
            )
    atomic_write_bytes(svg_path, ET.tostring(svg, encoding="utf-8", xml_declaration=True))


def escape_text(value: Any) -> str:
    normalized = "" if value is None else str(value)
    return html.escape(normalized).replace("\n", "<br>")


def render_list(values: Any, empty: str = "公开信息不足 / Insufficient public evidence") -> str:
    if not isinstance(values, list) or not values:
        return f"<p class=\"muted\">{html.escape(empty)}</p>"
    return "<ul>" + "".join(f"<li>{escape_text(value)}</li>" for value in values) + "</ul>"


def data_uri(path_value: Any, report_root: Path) -> str:
    if not path_value:
        return ""
    path = require_path_within(report_root, path_value, "embedded asset")
    if not path.is_file():
        raise PipelineError(f"embedded asset is not a regular file: {path}")
    if path.stat().st_size > MAX_EMBEDDED_ASSET_BYTES:
        raise PipelineError(
            f"embedded asset exceeds {MAX_EMBEDDED_ASSET_BYTES} bytes: {path}"
        )
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def safe_http_link(value: Any) -> str:
    url = str(value or "").strip()
    if urlparse(url).scheme not in {"http", "https"}:
        return "#"
    return html.escape(url, quote=True)


def authors_summary(authors: Any) -> str:
    if not isinstance(authors, list):
        return ""
    visible = [str(item) for item in authors[:12]]
    suffix = f" et al. ({len(authors)} authors)" if len(authors) > 12 else ""
    return ", ".join(visible) + suffix


def figure_block(
    title: str,
    image_path: Any,
    caption: str,
    source_url: str,
    badge: str,
    report_root: Path,
) -> str:
    uri = data_uri(image_path, report_root)
    if not uri:
        return (
            "<figure class=\"visual missing\"><div class=\"missing-box\">图像不可用 / Image unavailable</div>"
            f"<figcaption><strong>{html.escape(title)}</strong><br>{html.escape(caption)}</figcaption></figure>"
        )
    return f"""
      <figure class="visual">
        <div class="visual-head"><strong>{html.escape(title)}</strong><span class="badge">{html.escape(badge)}</span></div>
        <img src="{uri}" alt="{html.escape(title, quote=True)}" loading="lazy">
        <figcaption>{html.escape(caption)} <a href="{safe_http_link(source_url)}">来源 / source</a></figcaption>
      </figure>
    """


def render_claim_table(rows: Any) -> str:
    if not isinstance(rows, list) or not rows:
        return "<p class=\"muted\">未提供主张—证据映射。</p>"
    body = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        body.append(
            "<tr>"
            f"<td>{escape_text(row.get('claim'))}</td>"
            f"<td>{escape_text(row.get('evidence'))}</td>"
            f"<td><span class=\"strength\">{escape_text(row.get('strength'))}</span></td>"
            "</tr>"
        )
    return (
        "<div class=\"table-wrap\"><table><thead><tr><th>主张 / Claim</th><th>证据 / Evidence</th>"
        "<th>证据强度</th></tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
    )


def load_automatic_writing_metrics(paper: Dict[str, Any], report_root: Path) -> Dict[str, Any]:
    metrics_path = paper.get("ai_writing_metrics_path")
    if not metrics_path:
        return {}
    try:
        safe_path = require_path_within(
            report_root,
            metrics_path,
            "automatic writing metrics",
        )
        if not safe_path.is_file():
            raise PipelineError(f"automatic writing metrics is not a file: {safe_path}")
        payload = load_json(safe_path)
    except PipelineError:
        return {}
    return payload if isinstance(payload, dict) else {}


def render_automatic_writing_metrics(paper: Dict[str, Any], report_root: Path) -> str:
    payload = load_automatic_writing_metrics(paper, report_root)
    metrics = payload.get("metrics") or {}
    counts = payload.get("document_counts") or {}
    if not isinstance(metrics, dict) or not metrics:
        return '<p class="muted">自动描述指标不可用 / Metrics unavailable。</p>'

    rows: List[str] = []
    definitions = (
        ("lexical_marker_density", "特征词候选 / Lexical markers", "仅统计候选特征词；群体词频变化不能判定单篇论文的作者身份。"),
        ("formulaic_transition_density", "模板化衔接 / Formulaic transitions", "普通学术衔接语和非母语写作均可能产生该指标。"),
        ("promotional_claim_density", "宣传性主张候选 / Promotional claims", "必须回到论文核对相关主张是否获得实验或理论证据支持。"),
        ("vague_attribution_density", "模糊归因候选 / Vague attribution", "附近的有效引文可能足以消除该候选异常。"),
        ("chatbot_residue", "对话界面残留 / Chatbot residue", "需排除论文引用的提示词、数据样例和攻击文本。"),
        ("negative_parallelism_density", "否定式排比 / Negative parallelism", "这是修辞结构描述，不能单独作为 AI 辅助证据。"),
        ("tricolon_candidate_density", "三段式候选 / Three-part lists", "该结构在人类技术写作中同样常见，指标噪声较高。"),
        ("em_dash_density", "破折号密度 / Dash density", "排版转换和出版模板会显著影响该数值。"),
        ("repeated_sentence_opening_rate", "重复句首 / Repeated openings", "技术主语重复和章节模板是重要混杂因素。"),
        ("sentence_length_variation", "句长变异 / Sentence-length variation", "低变异可能来自模板，也可能来自公式、体裁或 PDF 提取方式。"),
        ("paragraph_length_variation", "段长变异 / Paragraph-length variation", "该指标对 PDF 分段质量较敏感。"),
        ("moving_average_type_token_ratio", "词汇多样性 / MATTR", "仅作上下文描述，尤其不能据此惩罚非母语英语写作。"),
    )
    unit_labels = {
        "matches_per_1000_words": "次/千词",
        "literal_matches": "处字面命中",
        "dashes_per_1000_words": "个/千词",
        "percent_excess_repeated_three_word_openings": "% 重复句首",
        "MATTR": "MATTR",
    }
    for metric_id, label, chinese_note in definitions:
        metric = metrics.get(metric_id)
        if not isinstance(metric, dict):
            continue
        if metric_id in {"sentence_length_variation", "paragraph_length_variation"}:
            value = (
                f"变异系数 {metric.get('coefficient_of_variation', 'n/a')} · "
                f"平均 {metric.get('mean_words', 'n/a')} 词 · 样本数 {metric.get('sample_size', 0)}"
            )
        elif metric_id == "moving_average_type_token_ratio":
            value = f"{metric.get('value', 'n/a')}（窗口 {metric.get('window_words', 0)} 词）"
        else:
            unit = unit_labels.get(str(metric.get("unit") or ""), str(metric.get("unit") or ""))
            value = f"{metric.get('value', 'n/a')} {unit}".strip()
        matched_terms = ", ".join(
            f"{item.get('term')}×{item.get('count')}"
            for item in (metric.get("matches") or [])[:8]
            if isinstance(item, dict)
        )
        detail = f"命中：{matched_terms}。{chinese_note}" if matched_terms else chinese_note
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(metric_id)}</code><br>{html.escape(label)}</td>"
            f"<td>{html.escape(value)}</td>"
            f"<td>{html.escape(detail)}</td>"
            "</tr>"
        )
    count_summary = (
        f"已分析 {counts.get('words', 0)} 词、{counts.get('sentences', 0)} 句、"
        f"{counts.get('paragraphs', 0)} 段 · 方法版本 {payload.get('method_version', 'unknown')}"
    )
    return (
        f'<p class="metric-scope">{html.escape(count_summary)}</p>'
        '<div class="table-wrap"><table class="metric-table"><thead><tr>'
        '<th>自动描述指标</th><th>原始值</th><th>命中项与解释</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        '<p class="metric-warning">这些数值是未经单篇论文校准的描述指标，不是 AI 概率。'
        "所有实质性命中都必须回到 PDF 核查，并评估正常学术写作中的混杂因素。</p>"
    )


def render_writing_scorecard(writing: Dict[str, Any]) -> str:
    scorecard = writing.get("indicator_scorecard")
    if not isinstance(scorecard, list) or not scorecard:
        return ""
    rows: List[str] = []
    for item in scorecard:
        if not isinstance(item, dict):
            continue
        indicator = AI_WRITING_INDICATOR_MAP.get(str(item.get("id"))) or {
            "name": str(item.get("id") or "unknown"),
            "family": "unknown",
        }
        locations = "; ".join(str(value) for value in (item.get("locations") or []))
        family = str(indicator.get("family") or "unknown")
        family_label = f"{FAMILY_ZH.get(family, family)} / {family}"
        rows.append(
            "<tr>"
            f"<td>{escape_text(indicator.get('name'))}<br><small>{escape_text(family_label)}</small></td>"
            f"<td><span class=\"indicator-score score-{escape_text(item.get('score'))}\">{escape_text(item.get('score'))}/2</span></td>"
            f"<td>{escape_text(locations)}</td>"
            f"<td>{escape_text(item.get('evidence'))}</td>"
            f"<td>{escape_text(item.get('counter_evidence'))}</td>"
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table class="scorecard"><thead><tr>'
        '<th>指标</th><th>评分</th><th>位置</th><th>支持证据</th><th>反证</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def render_writing_scope_and_aggregate(writing: Dict[str, Any]) -> str:
    scope = writing.get("scope") or {}
    aggregate = writing.get("aggregate") or {}
    checks = scope.get("section_checks") or []
    checked = ", ".join(
        f"{SECTION_ZH.get(str(item.get('section')), str(item.get('section')))}："
        f"{STATUS_ZH.get(str(item.get('status')), str(item.get('status')))}（{item.get('location')}）"
        for item in checks
        if isinstance(item, dict)
    )
    if not scope and not aggregate:
        return ""
    families = ", ".join(
        FAMILY_ZH.get(str(value), str(value))
        for value in (aggregate.get("positive_families") or [])
    ) or "无"
    sections = ", ".join(str(value) for value in (aggregate.get("positive_sections") or [])) or "无"
    coverage = {"sufficient": "充分", "insufficient": "不足"}.get(
        str(scope.get("coverage")), str(scope.get("coverage") or "")
    )
    return f"""
      <div class="audit-meta">
        <div><strong>审阅覆盖度</strong><p>{escape_text(coverage)} · 已分析 {escape_text(scope.get('analyzed_word_count'))} 词</p></div>
        <div><strong>序数审计分</strong><p>{escape_text(aggregate.get('score_total'))}/16 · {escape_text(aggregate.get('positive_indicators'))} 项阳性指标 · {escape_text(aggregate.get('rubric_version'))}</p></div>
        <div class="wide"><strong>章节检查</strong><p>{escape_text(checked)}</p></div>
        <div><strong>阳性指标族</strong><p>{escape_text(families)}</p></div>
        <div><strong>阳性证据位置</strong><p>{escape_text(sections)}</p></div>
        <div class="wide"><strong>分级理由</strong><p>{escape_text(aggregate.get('rationale'))}</p></div>
        <div class="wide"><strong>覆盖说明</strong><p>{escape_text(scope.get('coverage_note'))}</p></div>
      </div>
    """


def render_paper(
    paper: Dict[str, Any],
    review: Dict[str, Any],
    contribution_svg: Path,
    position: int,
    report_root: Path,
    topic_labels: Optional[Dict[str, str]] = None,
) -> str:
    arxiv_id = paper.get("arxiv_id", "")
    anchor = f"paper-{safe_slug(base_arxiv_id(arxiv_id))}"
    priority = str(review.get("priority", "Medium"))
    writing = review["ai_writing_assessment"]
    writing_label_raw = str(writing.get("label", "insufficient_evidence"))
    writing_label = f"{WRITING_LABEL_ZH.get(writing_label_raw, writing_label_raw)} / {writing_label_raw.replace('_', ' ')}"
    confidence_raw = str(writing.get("confidence") or "")
    confidence_label = f"{CONFIDENCE_ZH.get(confidence_raw, confidence_raw)} / {confidence_raw}"
    priority_label = f"{PRIORITY_ZH.get(priority, priority)} / {priority}"
    group = review["group_profile"]
    perspectives = review["reviews"]
    reproducibility = review["reproducibility"]
    source_figure = paper.get("main_figure") or {}

    source_visual = figure_block(
        "论文主图 / Paper main figure",
        source_figure.get("path"),
        source_figure.get("caption") or "主图不可用 / Main figure unavailable",
        source_figure.get("source_url") or paper.get("url"),
        source_figure.get("kind") or "unavailable",
        report_root,
    )
    contribution_visual = figure_block(
        "评审者生成的贡献图 / Reviewer contribution map",
        contribution_svg,
        "自上而下展示：问题 ↓ 方法 ↓ 关键机制 ↓ 证据与影响；可编辑 draw.io 源文件与报告资源一并保留。",
        paper.get("url"),
        "评审综合",
        report_root,
    )

    categories = " · ".join(str(item) for item in paper.get("categories") or [])
    score = paper.get("relevance_score")
    score_text = f"{float(score):.1f}/10" if isinstance(score, (int, float)) else "n/a"
    code_url = paper.get("code_url")
    code_link = f'<a href="{safe_http_link(code_url)}">代码 / Code</a>' if code_url else "未发现代码链接"
    topic = str(paper.get("primary_topic") or "other")
    topic_label = (topic_labels or {}).get(topic) or DEFAULT_TOPIC_LABELS.get(topic, topic)

    return f"""
    <article class="paper" id="{anchor}">
      <header class="paper-header">
        <div class="rank">{position:02d}</div>
        <div>
          <p class="eyebrow">arXiv {html.escape(str(arxiv_id))} · 相关性 {score_text}</p>
          <h2>{escape_text(review.get('title_zh'))}</h2>
          <p class="title-en">{escape_text(paper.get('title'))}</p>
          <p class="meta">{escape_text(authors_summary(paper.get('authors')))}<br>{escape_text(categories)} · {code_link} · <span class="topic-pill topic-{html.escape(topic)}">{escape_text(topic_label)}</span></p>
        </div>
        <div class="priority priority-{html.escape(priority.lower())}">{html.escape(priority_label)}</div>
      </header>

      <div class="verdict"><strong>为什么值得看 / Why read</strong><p>{escape_text(review.get('why_read'))}</p></div>

      <div class="visual-grid">{source_visual}{contribution_visual}</div>

      <section class="summary-primary">
        <h3>中文总结</h3><p>{escape_text(review.get('summary_zh'))}</p>
      </section>
      <details class="english-summary"><summary>英文摘要（补充） / English summary</summary><p>{escape_text(review.get('summary_en'))}</p></details>

      <section>
        <h3>AI 辅助写作迹象 / AI-assisted writing signals</h3>
        <div class="ai-audit ai-{html.escape(str(writing.get('label')))}">
          <div class="audit-label"><span>{escape_text(writing_label)}</span><small>证据置信度 {escape_text(confidence_label)}</small></div>
          <div><strong>使用声明</strong><p>{escape_text(writing.get('disclosure'))}</p></div>
          <div><strong>支持该分级的证据</strong>{render_list(writing.get('evidence_for'))}</div>
          <div><strong>反证</strong>{render_list(writing.get('counter_evidence'))}</div>
          <div><strong>混杂因素 / Confounders</strong>{render_list(writing.get('confounders'))}</div>
          <p class="caveat">{escape_text(writing.get('caveat'))}</p>
        </div>
        {render_writing_scope_and_aggregate(writing)}
        <details class="writing-details" open>
          <summary>指标评分卡 / Indicator scorecard</summary>
          {render_writing_scorecard(writing)}
        </details>
        <details class="writing-details">
          <summary>自动描述指标 / Automatic metrics</summary>
          {render_automatic_writing_metrics(paper, report_root)}
        </details>
      </section>

      <section><h3>主张—证据核查 / Claim–evidence audit</h3>{render_claim_table(review.get('claim_evidence'))}</section>

      <div class="analysis-grid">
        <section><h3>系统与基础设施视角</h3><p>{escape_text(perspectives.get('systems'))}</p></section>
        <section><h3>领域与应用视角</h3><p>{escape_text(perspectives.get('ai4sci'))}</p></section>
        <section><h3>研究价值与风险</h3><p>{escape_text(perspectives.get('research_value'))}</p></section>
        <section><h3>创新边界 / Novelty boundary</h3><p>{escape_text(review.get('novelty_boundary'))}</p></section>
        <section><h3>评测有效性 / Evaluation validity</h3><p>{escape_text(review.get('evaluation_validity'))}</p></section>
        <section><h3>局限</h3>{render_list(review.get('limitations'))}</section>
      </div>

      <details>
        <summary>课题组画像 / Research group profile</summary>
        <dl class="facts">
          <dt>主要团队</dt><dd>{escape_text(group.get('lead_groups'))}</dd>
          <dt>关键人员</dt><dd>{escape_text(group.get('key_people'))}</dd>
          <dt>研究重点</dt><dd>{escape_text(group.get('focus'))}</dd>
          <dt>相关工作</dt><dd>{escape_text(group.get('prior_work'))}</dd>
          <dt>关注理由</dt><dd>{escape_text(group.get('why_follow'))}</dd>
          <dt>证据置信度</dt><dd>{escape_text(group.get('evidence_confidence'))}</dd>
        </dl>
      </details>

      <details>
        <summary>可复现性与工件风险 / Reproducibility</summary>
        <dl class="facts">
          <dt>代码</dt><dd>{escape_text(reproducibility.get('code'))}</dd>
          <dt>数据与模型</dt><dd>{escape_text(reproducibility.get('data_models'))}</dd>
          <dt>许可证</dt><dd>{escape_text(reproducibility.get('license'))}</dd>
          <dt>环境与资源需求</dt><dd>{escape_text(reproducibility.get('requirements'))}</dd>
          <dt>复现工作量</dt><dd>{escape_text(reproducibility.get('effort'))}</dd>
          <dt>最小有效测试</dt><dd>{escape_text(reproducibility.get('smallest_test'))}</dd>
          <dt>工件风险</dt><dd>{render_list(review.get('artifact_risks'))}</dd>
        </dl>
      </details>

      <section class="next"><h3>后续阅读与复现 / Next steps</h3>{render_list(review.get('next_steps'))}</section>
      <p class="paper-links"><a href="{safe_http_link(paper.get('url'))}">arXiv 摘要页</a> · <a href="https://arxiv.org/pdf/{html.escape(str(arxiv_id), quote=True)}">论文 PDF</a></p>
    </article>
    """


REPORT_CSS = r"""
:root{--main:#4477AA;--cmp:#EE6677;--pos:#228833;--opt:#CCBB44;--aux:#66CCEE;--ink:#25282d;--muted:#69717d;--line:#dfe4ea;--paper:#fff;--bg:#f4f6f8;--radius:7px}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif;line-height:1.62}a{color:#285f9c;text-decoration-thickness:1px;text-underline-offset:2px}.shell{max-width:1180px;margin:auto;padding:32px 24px 80px}.hero{background:#17263a;color:#fff;border-top:5px solid var(--main);padding:34px 38px;margin-bottom:22px}.hero h1{font-size:clamp(28px,4vw,48px);line-height:1.12;margin:4px 0 12px;letter-spacing:-.025em}.hero p{max-width:860px;color:#d7e2ef}.kicker,.eyebrow{text-transform:uppercase;letter-spacing:.12em;font-size:12px;font-weight:700;color:#8fb5df}.hero .kicker{color:#8fd8ec}.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:24px}.stat{padding:15px;border:1px solid #39506c;background:#203650}.stat strong{font-size:27px;display:block}.notice{background:#fff8dc;border-left:4px solid var(--opt);padding:14px 17px;margin:18px 0;color:#4d4630}.toc{background:var(--paper);border:1px solid var(--line);padding:20px 24px;margin-bottom:22px}.toc ol{columns:2;column-gap:32px;margin:10px 0}.toc li{break-inside:avoid;margin:5px 0}.paper{background:var(--paper);border:1px solid var(--line);border-top:4px solid var(--main);padding:28px 30px;margin:0 0 26px;box-shadow:0 7px 20px rgba(28,42,58,.045)}.paper-header{display:grid;grid-template-columns:54px minmax(0,1fr) auto;gap:16px;align-items:start}.rank{font-size:28px;font-weight:800;color:#aab4c0;border-right:1px solid var(--line)}h2{font-size:clamp(22px,3vw,32px);line-height:1.23;margin:2px 0 4px;letter-spacing:-.018em}.title-zh{font-size:17px;margin:0;color:#4a5563}.meta{font-size:13px;color:var(--muted)}.topic-pill{display:inline-block;padding:1px 6px;border:1px solid #b8c5d3;background:#eef4fa;color:#2f5f8e;font-weight:750}.topic-ai4sci_infra{background:#f1f7ee;border-color:#a6c69d;color:#3d6934}.topic-other{background:#f5f5f5;border-color:#ccc;color:#666}.priority{font-weight:800;border:1px solid;padding:4px 9px;font-size:12px;text-transform:uppercase}.priority-high{color:#a72e42;border-color:#d998a4;background:#fff0f2}.priority-medium{color:#7a6419;border-color:#d8c36a;background:#fff9dc}.priority-low{color:#4f5e6e;border-color:#b8c1cc;background:#f5f7f9}.verdict{border-left:4px solid var(--main);background:#f1f6fb;padding:13px 16px;margin:20px 0}.verdict p{margin:4px 0}.visual-grid,.summary-grid,.analysis-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.visual{margin:0;border:1px solid var(--line);background:#fbfcfd;padding:12px}.visual-head{display:flex;justify-content:space-between;gap:12px;margin-bottom:8px}.visual img{display:block;width:100%;height:auto;max-height:520px;object-fit:contain;background:#fff}.visual figcaption{font-size:12px;color:var(--muted);padding-top:8px}.badge,.strength{font-size:10px;letter-spacing:.05em;text-transform:uppercase;background:#edf1f5;padding:3px 6px;white-space:nowrap}.missing-box{min-height:220px;display:grid;place-items:center;color:var(--muted);background:#f0f2f5}.summary-grid>div,.analysis-grid>section{border-top:1px solid var(--line);padding-top:9px}section{margin-top:22px}h3{font-size:16px;margin:0 0 8px;color:#26384b}.ai-audit{display:grid;grid-template-columns:minmax(150px,180px) minmax(0,1fr) minmax(0,1fr);gap:14px;border:1px solid var(--line);padding:16px}.audit-label{grid-row:1 / span 2}.audit-label span{font-size:17px;line-height:1.25;font-weight:850;text-transform:uppercase;display:block;overflow-wrap:anywhere}.audit-label small{color:var(--muted)}.ai-audit>div:nth-child(4){grid-column:2/-1}.ai-high,.ai-medium{border-left:5px solid var(--cmp)}.ai-low{border-left:5px solid var(--pos)}.ai-insufficient_evidence{border-left:5px solid #999}.ai-disclosed{border-left:5px solid var(--main)}.caveat{grid-column:1/-1;background:#f6f7f8;color:#505862;padding:10px;margin:0;font-size:12px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;vertical-align:top;border-bottom:1px solid var(--line);padding:9px 10px}th{background:#f4f6f8}ul{padding-left:20px}.muted{color:var(--muted)}details{border-top:1px solid var(--line);margin-top:18px;padding-top:12px}summary{cursor:pointer;font-weight:750;color:#304c6a}.facts{display:grid;grid-template-columns:170px 1fr;gap:7px 16px}.facts dt{font-weight:750;color:#596574}.facts dd{margin:0}.next{background:#f6f9fc;padding:14px 16px}.paper-links{text-align:right;font-size:13px}.footer{color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:18px;margin-top:30px}@media(max-width:780px){.shell{padding:12px}.hero{padding:24px 20px}.stats,.visual-grid,.summary-grid,.analysis-grid,.ai-audit{grid-template-columns:1fr}.toc ol{columns:1}.paper{padding:20px 17px}.paper-header{grid-template-columns:42px minmax(0,1fr)}.priority{grid-column:2}.facts{grid-template-columns:1fr}.facts dt{margin-top:8px}.audit-label{grid-row:auto}.ai-audit>div:nth-child(4),.caveat{grid-column:auto}}
.ai-audit>div:nth-child(4){grid-column:auto}.audit-meta{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 18px;background:#f8fafc;border:1px solid var(--line);border-top:0;padding:14px 16px;font-size:13px}.audit-meta p{margin:3px 0}.audit-meta .wide{grid-column:1/-1}.writing-details{border:1px solid var(--line);padding:12px 14px;margin-top:12px}.writing-details[open]>summary{margin-bottom:10px}.scorecard{min-width:980px}.scorecard small{color:var(--muted)}.indicator-score{display:inline-block;min-width:40px;text-align:center;font-weight:800;padding:2px 6px;border:1px solid #c9d1da;background:#f4f6f8}.score-1{background:#fff8dc;border-color:#d8c36a}.score-2{background:#fff0f2;border-color:#d998a4;color:#9a2d40}.metric-table{min-width:850px}.metric-table code{font-size:11px}.metric-scope{font-size:13px;color:#485666}.metric-warning{font-size:12px;background:#fff8dc;border-left:3px solid var(--opt);padding:9px 11px}.table-wrap+.metric-warning{margin-top:10px}@media(max-width:780px){.audit-meta{grid-template-columns:1fr}.audit-meta .wide{grid-column:auto}}
.title-en{font-size:15px;line-height:1.45;margin:5px 0 0;color:#69717d}.toc small{color:var(--muted);font-size:12px}.topic-hpc_systems{background:#fff8dc;border-color:#d8c36a;color:#6f5a12}.summary-primary{border-left:4px solid var(--main);background:#f6f9fc;padding:16px 18px}.summary-primary p{margin:0;font-size:15px}.english-summary{margin-top:10px;border:1px solid var(--line);padding:10px 14px;color:#56616d;background:#fbfcfd}.english-summary p{font-size:13px;margin:9px 0 2px}@media print{body{background:#fff}.shell{max-width:none;padding:0}.paper{box-shadow:none;break-inside:avoid}.english-summary{display:block}.writing-details{break-inside:avoid}a{color:#222;text-decoration:none}}
"""


# Let a longer contribution map grow rather than shrink its text to fit 520px.
REPORT_CSS += "\n.visual-grid{align-items:start}.visual-grid>.visual:nth-child(2) img{max-height:none}\n"


def confine_manifest_paths(manifest: Dict[str, Any], report_dir: Path) -> Dict[str, Any]:
    """Return a copy whose referenced resources are confined to report_dir."""
    safe_manifest = copy.deepcopy(manifest)
    recorded_root = safe_manifest.get("report_dir")
    if recorded_root:
        resolved_root = require_path_within(
            report_dir,
            recorded_root,
            "manifest report_dir",
        )
        if resolved_root != report_dir:
            raise PipelineError(
                f"manifest report_dir does not match manifest location: {resolved_root}"
            )
    safe_manifest["report_dir"] = str(report_dir)

    safe_papers: List[Dict[str, Any]] = []
    for paper in safe_manifest.get("papers") or []:
        if not isinstance(paper, dict):
            raise PipelineError("manifest papers must be objects")
        arxiv_id = normalize_arxiv_id(paper.get("arxiv_id"))
        if not arxiv_id:
            safe_papers.append(paper)
            continue
        expected_dir = report_dir / "assets" / safe_slug(arxiv_id)
        expected_dir = require_path_within(
            report_dir,
            expected_dir,
            f"{arxiv_id} paper directory",
        )
        recorded_dir = paper.get("paper_dir")
        if recorded_dir:
            resolved_dir = require_path_within(
                report_dir,
                recorded_dir,
                f"{arxiv_id} manifest paper_dir",
            )
            if resolved_dir != expected_dir:
                raise PipelineError(
                    f"{arxiv_id} paper_dir does not match its deterministic path"
                )
        paper["paper_dir"] = str(expected_dir)

        expected_names = {
            "pdf_path": "paper.pdf",
            "text_path": "paper.txt",
            "analysis_text_path": "paper_analysis.txt",
            "ai_writing_metrics_path": "ai_writing_metrics.json",
        }
        for field, expected_name in expected_names.items():
            if not paper.get(field):
                continue
            resolved = require_path_within(
                expected_dir,
                paper[field],
                f"{arxiv_id} {field}",
            )
            if not resolved.is_file() or resolved.name != expected_name:
                raise PipelineError(f"{arxiv_id} has an invalid {field}: {resolved}")
            paper[field] = str(resolved)

        main_figure = paper.get("main_figure")
        if isinstance(main_figure, dict) and main_figure.get("path"):
            resolved_figure = require_path_within(
                expected_dir,
                main_figure["path"],
                f"{arxiv_id} main figure",
            )
            if (
                not resolved_figure.is_file()
                or resolved_figure.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff", ".svg"}
                or not resolved_figure.name.startswith(("main_figure.", "first_page_preview."))
            ):
                raise PipelineError(
                    f"{arxiv_id} has an invalid main figure path: {resolved_figure}"
                )
            main_figure["path"] = str(resolved_figure)
        safe_papers.append(paper)

    safe_manifest["papers"] = safe_papers
    return safe_manifest


def write_build_receipt(
    output_path: Path,
    manifest_path: Path,
    reviews_path: Path,
    report_dir: Path,
) -> Path:
    receipt_path = output_path.with_suffix(".receipt.json")
    receipt = {
        "schema_version": 1,
        "report_file": output_path.name,
        "report_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "report_bytes": output_path.stat().st_size,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "reviews_sha256": hashlib.sha256(reviews_path.read_bytes()).hexdigest(),
        "generated_at": datetime.now().astimezone().isoformat(),
    }
    receipt_path = require_path_within(
        report_dir,
        receipt_path,
        "build receipt",
        must_exist=False,
    )
    atomic_write_text(
        receipt_path,
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
    )
    return receipt_path


def build_report(manifest_path: Path, reviews_path: Path, output_path: Optional[Path] = None) -> Path:
    if manifest_path.is_symlink():
        raise PipelineError(f"manifest must not be a symbolic link: {manifest_path}")
    manifest_path = manifest_path.resolve(strict=True)
    report_dir = manifest_path.parent.resolve(strict=True)
    reviews_path = require_path_within(report_dir, reviews_path, "reviews file")
    if not reviews_path.is_file():
        raise PipelineError(f"reviews path is not a file: {reviews_path}")
    manifest = load_json(manifest_path)
    reviews = load_json(reviews_path)
    if not isinstance(manifest, dict) or not isinstance(reviews, dict):
        raise PipelineError("manifest and reviews must be JSON objects")
    manifest = confine_manifest_paths(manifest, report_dir)
    review_map = validate_review_payload(manifest, reviews)

    report_date = validate_date(str(reviews.get("date") or manifest.get("date")))
    output_path = output_path or report_dir / f"daily_arxiv_report_{date_slug(report_date)}.html"
    output_path = require_path_within(
        report_dir,
        output_path,
        "HTML output",
        must_exist=False,
    )
    contribution_paths: Dict[str, Path] = {}

    for paper in manifest.get("papers", []):
        identifier = base_arxiv_id(paper.get("arxiv_id"))
        review = review_map[identifier]
        paper_dir = require_path_within(
            report_dir,
            paper["paper_dir"],
            f"{identifier} paper directory",
        )
        drawio_path = paper_dir / "reviewer_contribution.drawio"
        svg_path = paper_dir / "reviewer_contribution.svg"
        write_drawio_contribution(review["contribution_diagram"], drawio_path)
        write_contribution_svg(review["contribution_diagram"], svg_path)
        contribution_paths[identifier] = svg_path

    run_summary = reviews.get("run_summary") or {}
    recommended = run_summary.get("recommended")
    if not isinstance(recommended, int):
        recommended = len(manifest.get("papers", []))
    total_fetched = run_summary.get("total_fetched", "n/a")

    topic_counts = run_summary.get("topic_counts")
    if not isinstance(topic_counts, dict):
        topic_counts = {}
        for paper in manifest.get("papers", []):
            topic = str(paper.get("primary_topic") or "other")
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
    topic_counts = {
        str(topic): count
        for topic, count in topic_counts.items()
        if isinstance(count, int)
    }
    topic_labels = manifest.get("topic_labels")
    if not isinstance(topic_labels, dict):
        topic_labels = {}

    def topic_label_for(topic: str) -> str:
        return str(topic_labels.get(topic) or DEFAULT_TOPIC_LABELS.get(topic, topic))

    primary_stat_topic = next(
        (topic for topic in topic_labels if topic_counts.get(topic)), None
    )
    if primary_stat_topic is None and topic_counts:
        primary_stat_topic = max(topic_counts, key=lambda topic: topic_counts[topic])
    if primary_stat_topic is None:
        primary_stat_value, primary_stat_label = "n/a", "主题命中"
        other_stat_value, other_stat_label = "n/a", "其他主题"
    else:
        primary_stat_value = topic_counts[primary_stat_topic]
        primary_stat_label = topic_label_for(primary_stat_topic)
        other_topics = [
            topic for topic in topic_counts if topic != primary_stat_topic
        ]
        other_stat_value = sum(topic_counts[topic] for topic in other_topics)
        other_stat_label = (
            " / ".join(topic_label_for(topic) for topic in other_topics) or "其他主题"
        )

    if topic_labels:
        hero_scope = "研究主题：" + " → ".join(
            topic_label_for(topic) for topic in topic_labels
        )
    else:
        hero_scope = (
            "AI 基础设施优先，独立覆盖 HPC 并行系统、通信、调度、存储与性能建模，"
            "并保留 AI4Sci 基础设施观察。"
        )

    toc_entries = []
    articles = []
    for position, paper in enumerate(manifest.get("papers", []), start=1):
        identifier = base_arxiv_id(paper.get("arxiv_id"))
        review = review_map[identifier]
        anchor = f"paper-{safe_slug(identifier)}"
        toc_entries.append(
            f'<li><a href="#{anchor}">{position}. {escape_text(review.get("title_zh") or identifier)}'
            f'<small> — {escape_text(paper.get("title") or identifier)}</small></a></li>'
        )
        articles.append(
            render_paper(
                paper,
                review,
                contribution_paths[identifier],
                position,
                report_dir,
                topic_labels,
            )
        )

    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="referrer" content="no-referrer">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
  <title>Daily arXiv Report / 每日 arXiv 论文分析报告 — {html.escape(report_date)}</title>
  <style>{REPORT_CSS}</style>
</head>
<body>
<main class="shell">
  <header class="hero">
    <p class="kicker">每日研究情报 · Daily research intelligence</p>
    <h1>Daily arXiv Report / 每日 arXiv 论文分析报告</h1>
    <p>{html.escape(report_date)} · {escape_text(hero_scope)}</p>
    <div class="stats"><div class="stat"><strong>{escape_text(total_fetched)}</strong>抓取论文</div><div class="stat"><strong>{escape_text(recommended)}</strong>推荐论文</div><div class="stat"><strong>{escape_text(primary_stat_value)}</strong>{escape_text(primary_stat_label)}</div><div class="stat"><strong>{escape_text(other_stat_value)}</strong>{escape_text(other_stat_label)}</div></div>
  </header>
  <div class="notice"><strong>解释边界：</strong>“AI 辅助写作迹象”是结构化的序数证据审计。自动指标未经单篇论文校准，不是概率；任何分级都不能证明 AI 作者身份、学术不端、抄袭或研究无效。</div>
  <nav class="toc"><strong>目录 / Contents</strong><ol>{''.join(toc_entries)}</ol></nav>
  {''.join(articles)}
  <footer class="footer">生成时间：{html.escape(generated_at)}。Daily Paper Brief 是独立项目，与 arXiv 或 Cornell University 无隶属或背书关系。Thank you to arXiv for use of its open access interoperability. 论文原图保留来源标注；贡献图由评审者综合生成。本报告仅用于研究线索筛选，不等同于正式同行评审或录用建议。</footer>
</main>
</body>
</html>
"""
    atomic_write_text(output_path, document)
    parsed = lxml_html.fromstring(output_path.read_bytes())
    if not parsed.xpath("//article[contains(@class,'paper')]"):
        raise PipelineError("generated HTML contains no paper articles")
    write_build_receipt(output_path, manifest_path, reviews_path, report_dir)
    return output_path


def merge_review_batches(
    manifest_path: Path,
    reviews_template_path: Path,
    batch_paths: Sequence[Path],
    output_path: Path,
    run_summary: Dict[str, Any],
    wait_seconds: int = 0,
    poll_seconds: float = 15.0,
) -> Path:
    """Wait for review batch files, merge them in template order, and validate."""
    if manifest_path.is_symlink():
        raise PipelineError(f"manifest must not be a symbolic link: {manifest_path}")
    manifest_path = manifest_path.resolve(strict=True)
    report_dir = manifest_path.parent.resolve(strict=True)
    reviews_template_path = require_path_within(
        report_dir,
        reviews_template_path,
        "reviews template",
    )
    output_path = require_path_within(
        report_dir,
        output_path,
        "merged reviews output",
        must_exist=False,
    )
    resolved_batches = [
        require_path_within(
            report_dir,
            path,
            "review batch",
            must_exist=False,
        )
        for path in batch_paths
    ]
    if not resolved_batches:
        raise PipelineError("at least one review batch path is required")

    manifest = load_json(manifest_path)
    template = load_json(reviews_template_path)
    if not isinstance(manifest, dict) or not isinstance(template, dict):
        raise PipelineError("manifest and reviews template must be JSON objects")
    template_papers = template.get("papers") or []
    expected_display_order = [normalize_arxiv_id(item.get("arxiv_id")) for item in template_papers]
    expected_order = [base_arxiv_id(item.get("arxiv_id")) for item in template_papers]
    if not expected_order or any(not identifier for identifier in expected_order):
        raise PipelineError("reviews template contains no valid arXiv IDs")
    if len(set(expected_order)) != len(expected_order):
        raise PipelineError("reviews template contains duplicate arXiv IDs")

    deadline = time.monotonic() + max(0, int(wait_seconds))
    last_state = ""
    merged_records: List[Dict[str, Any]] = []
    while True:
        issues: List[str] = []
        candidates: List[Dict[str, Any]] = []
        for batch_path in resolved_batches:
            if not batch_path.exists():
                issues.append(f"missing {batch_path.name}")
                continue
            try:
                safe_batch_path = require_path_within(
                    report_dir,
                    batch_path,
                    "review batch",
                )
                value = load_json(safe_batch_path)
            except PipelineError as exc:
                issues.append(f"{batch_path.name}: {exc}")
                continue
            if not isinstance(value, list):
                issues.append(f"{batch_path.name}: expected a JSON array")
                continue
            if not all(isinstance(item, dict) for item in value):
                issues.append(f"{batch_path.name}: every review must be an object")
                continue
            candidates.extend(value)

        identifiers = [base_arxiv_id(item.get("arxiv_id")) for item in candidates]
        duplicate_ids = sorted({identifier for identifier in identifiers if identifiers.count(identifier) > 1})
        missing_ids = [identifier for identifier in expected_order if identifier not in identifiers]
        extra_ids = sorted({identifier for identifier in identifiers if identifier not in expected_order})
        if duplicate_ids:
            issues.append(f"duplicate arXiv IDs: {duplicate_ids}")
        if missing_ids:
            issues.append(f"waiting for arXiv IDs: {missing_ids}")
        if extra_ids:
            issues.append(f"unexpected arXiv IDs: {extra_ids}")

        if not issues:
            record_map = {base_arxiv_id(item.get("arxiv_id")): item for item in candidates}
            merged_records = [record_map[identifier] for identifier in expected_order]
            break
        state = " | ".join(issues)
        if state != last_state:
            print(f"[arxiv-review] waiting for batches: {state}", file=sys.stderr, flush=True)
            last_state = state
        if time.monotonic() >= deadline:
            raise PipelineError(f"review batches not ready before timeout: {state}")
        time.sleep(max(0.5, float(poll_seconds)))

    topic_counts: Dict[str, int] = {}
    for item in manifest.get("papers") or []:
        topic = str(item.get("primary_topic") or "other")
        topic_counts[topic] = topic_counts.get(topic, 0) + 1
    summary = {
        "total_fetched": int(run_summary.get("total_fetched") or 0),
        "recommended": len(expected_order),
        "topic_counts": topic_counts,
        "top3_arxiv_ids": [
            normalize_arxiv_id(value)
            for value in (run_summary.get("top3_arxiv_ids") or expected_display_order[:3])
        ][:3],
    }
    payload = {
        "schema_version": template.get("schema_version"),
        "date": template.get("date"),
        "run_summary": summary,
        "papers": merged_records,
    }
    validate_review_payload(manifest, payload)
    atomic_write_text(output_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return output_path


def command_prepare(args: argparse.Namespace) -> int:
    manifest_path = prepare_assets(
        Path(args.input).resolve(),
        validate_date(args.date),
        Path(args.output_root).resolve(),
        args.limit,
    )
    manifest = load_json(manifest_path)
    figures = sum(1 for item in manifest["papers"] if item.get("main_figure"))
    texts = sum(1 for item in manifest["papers"] if item.get("text_path"))
    writing_metrics = sum(1 for item in manifest["papers"] if item.get("ai_writing_metrics_path"))
    result = {
        "ok": True,
        "manifest": str(manifest_path),
        "reviews_template": manifest.get("reviews_template_path"),
        "papers": len(manifest["papers"]),
        "figures_ready": figures,
        "texts_ready": texts,
        "ai_writing_metrics_ready": writing_metrics,
        "papers_with_errors": sum(1 for item in manifest["papers"] if item.get("errors")),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_build(args: argparse.Namespace) -> int:
    output = build_report(
        Path(args.assets_manifest).resolve(),
        Path(args.reviews).resolve(),
        Path(args.output).resolve() if args.output else None,
    )
    result = {"ok": True, "html": str(output), "bytes": output.stat().st_size}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_merge_batches(args: argparse.Namespace) -> int:
    output = merge_review_batches(
        Path(args.assets_manifest),
        Path(args.reviews_template),
        [Path(value) for value in args.batch],
        Path(args.output),
        {
            "total_fetched": args.total_fetched,
            "top3_arxiv_ids": args.top3_arxiv_id,
        },
        wait_seconds=args.wait_seconds,
        poll_seconds=args.poll_seconds,
    )
    payload = load_json(output)
    print(
        json.dumps(
            {
                "ok": True,
                "reviews": str(output),
                "papers": len(payload.get("papers") or []),
                "run_summary": payload.get("run_summary"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="download paper assets and extract review text")
    prepare.add_argument("--input", default=str(WORKSPACE / "papers_to_expand.json"))
    prepare.add_argument("--date", required=True, help="report date as YYYY-MM-DD")
    prepare.add_argument("--output-root", default=str(DEFAULT_REPORT_ROOT))
    prepare.add_argument("--limit", type=int, help="optional paper limit for smoke tests")
    prepare.set_defaults(func=command_prepare)

    build = commands.add_parser("build", help="validate reviews and build standalone HTML")
    build.add_argument("--assets-manifest", required=True)
    build.add_argument("--reviews", required=True)
    build.add_argument("--output")
    build.set_defaults(func=command_build)

    merge_batches = commands.add_parser(
        "merge-batches",
        help="wait for sub-agent review batches, merge in template order, and validate",
    )
    merge_batches.add_argument("--assets-manifest", required=True)
    merge_batches.add_argument("--reviews-template", required=True)
    merge_batches.add_argument("--batch", action="append", required=True)
    merge_batches.add_argument("--output", required=True)
    merge_batches.add_argument("--total-fetched", type=int, required=True)
    merge_batches.add_argument("--top3-arxiv-id", action="append", default=[])
    merge_batches.add_argument("--wait-seconds", type=int, default=3600)
    merge_batches.add_argument("--poll-seconds", type=float, default=15.0)
    merge_batches.set_defaults(func=command_merge_batches)
    return root


def main(argv: Optional[Sequence[str]] = None) -> int:
    os.umask(0o077)
    arguments = parser().parse_args(argv)
    try:
        return int(arguments.func(arguments))
    except (PipelineError, requests.RequestException, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
