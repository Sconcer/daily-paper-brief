#!/usr/bin/env python3
"""Compute descriptive, auditable writing metrics for extracted paper text.

These metrics are candidate signals for human review. They are deliberately
not an AI-authorship classifier and do not produce probabilities or labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple


METHOD_VERSION = "writing-signals-descriptive-v1.0"
WORD_RE = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")

# These lists combine patterns documented by the local humanizer-zh skill with
# lexical/discourse candidates reported in studies of LLM-assisted scientific
# writing. A hit is weak evidence by itself and is never scored automatically.
PHRASE_GROUPS: Dict[str, Tuple[str, ...]] = {
    "lexical_marker_density": (
        "delve",
        "intricate",
        "meticulous",
        "multifaceted",
        "nuanced",
        "pivotal",
        "realm",
        "tapestry",
        "underscore",
        "unwavering",
    ),
    "formulaic_transition_density": (
        "additionally",
        "furthermore",
        "in conclusion",
        "it is important to note",
        "it is worth noting",
        "it should be noted",
        "moreover",
        "overall, these results",
        "taken together",
        "this highlights",
        "this underscores",
    ),
    "promotional_claim_density": (
        "game-changing",
        "groundbreaking",
        "marks a significant advancement",
        "paradigm shift",
        "remarkable achievement",
        "revolutionary",
        "substantial leap",
        "transformative",
        "unparalleled",
        "unprecedented",
    ),
    "vague_attribution_density": (
        "experts believe",
        "it is generally accepted",
        "it is widely believed",
        "it is widely recognized",
        "prior work has demonstrated",
        "researchers have shown",
        "some studies suggest",
        "studies have shown",
    ),
    "chatbot_residue": (
        "as an ai language model",
        "certainly!",
        "feel free to ask",
        "here is a revised version",
        "i cannot assist with",
        "i hope this helps",
        "knowledge cutoff",
        "let me know if you need",
    ),
}

METRIC_NOTES = {
    "lexical_marker_density": (
        "Candidate overrepresented words per 1,000 words. Population-level "
        "vocabulary shifts do not identify authorship in one paper."
    ),
    "formulaic_transition_density": (
        "Formulaic connective candidates per 1,000 words; ordinary academic "
        "transitions and non-native writing are important confounders."
    ),
    "promotional_claim_density": (
        "Promotional or superlative claim candidates per 1,000 words. Human "
        "review must check whether each claim is supported."
    ),
    "vague_attribution_density": (
        "Vague attribution candidates per 1,000 words. Nearby citations may "
        "fully resolve an apparent hit."
    ),
    "chatbot_residue": (
        "Literal interface/prompt residue count. Verify the PDF location and "
        "exclude quoted examples before treating a hit as material."
    ),
}


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    escaped = re.escape(phrase).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![A-Za-z]){escaped}(?![A-Za-z])", re.IGNORECASE)


def _snippet(text: str, start: int, end: int, radius: int = 90) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    snippet = re.sub(r"\s+", " ", text[left:right]).strip()
    if left:
        snippet = "…" + snippet
    if right < len(text):
        snippet += "…"
    return snippet


def prepare_analysis_text(raw_text: str) -> Tuple[str, Dict[str, Any]]:
    """Normalize extraction noise while retaining paragraph boundaries."""
    original_length = len(raw_text)
    text = raw_text.replace("\u00ad", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?<=[a-z])-[ \t]*\n[ \t]*(?=[a-z])", "", text)

    bibliography_removed = False
    bibliography_match = None
    if len(text) > 3000:
        bibliography_match = re.search(
            r"(?im)^\s*(?:references|bibliography)\s*$", text[2500:]
        )
    if bibliography_match:
        cut = 2500 + bibliography_match.start()
        text = text[:cut]
        bibliography_removed = True

    cleaned_lines: List[str] = []
    for line in text.splitlines():
        compact = re.sub(r"[ \t]+", " ", line).strip()
        if re.fullmatch(r"(?:page\s+)?\d{1,4}", compact, flags=re.IGNORECASE):
            continue
        cleaned_lines.append(compact)
    text = "\n".join(cleaned_lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    paragraphs = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n\s*\n", text)]
    paragraphs = [part for part in paragraphs if part]
    normalized = "\n\n".join(paragraphs)
    return normalized, {
        "original_characters": original_length,
        "analyzed_characters": len(normalized),
        "bibliography_removed": bibliography_removed,
        "normalization": "dehyphenate line wraps; drop isolated page numbers; preserve blank-line paragraphs",
    }


def split_sentences(text: str) -> List[str]:
    flat = re.sub(r"\s+", " ", text).strip()
    if not flat:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\[(])", flat)
    return [part.strip() for part in parts if len(WORD_RE.findall(part)) >= 3]


def _density(count: int, word_count: int) -> float:
    return round((count * 1000.0 / word_count), 4) if word_count else 0.0


def phrase_metric(text: str, group_id: str, word_count: int) -> Dict[str, Any]:
    matches: List[Dict[str, Any]] = []
    examples: List[Dict[str, Any]] = []
    total = 0
    for phrase in PHRASE_GROUPS[group_id]:
        found = list(_phrase_pattern(phrase).finditer(text))
        if not found:
            continue
        total += len(found)
        matches.append({"term": phrase, "count": len(found)})
        for hit in found[:2]:
            if len(examples) >= 8:
                break
            examples.append(
                {
                    "term": phrase,
                    "character_offset": hit.start(),
                    "context": _snippet(text, hit.start(), hit.end()),
                }
            )
    unit = "literal_matches" if group_id == "chatbot_residue" else "matches_per_1000_words"
    value: float | int = total if group_id == "chatbot_residue" else _density(total, word_count)
    return {
        "value": value,
        "unit": unit,
        "count": total,
        "matches": matches,
        "examples": examples,
        "interpretation": METRIC_NOTES[group_id],
    }


def regex_metric(
    text: str,
    pattern: re.Pattern[str],
    word_count: int,
    interpretation: str,
) -> Dict[str, Any]:
    hits = list(pattern.finditer(text))
    return {
        "value": _density(len(hits), word_count),
        "unit": "matches_per_1000_words",
        "count": len(hits),
        "examples": [
            {
                "character_offset": hit.start(),
                "context": _snippet(text, hit.start(), hit.end()),
            }
            for hit in hits[:8]
        ],
        "interpretation": interpretation,
    }


def coefficient_of_variation(values: Sequence[int]) -> float | None:
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    return round(statistics.pstdev(values) / mean, 4) if mean else None


def moving_average_type_token_ratio(words: Sequence[str], window: int = 100) -> float | None:
    if len(words) < 20:
        return None
    actual_window = min(window, len(words))
    counts = Counter(words[:actual_window])
    total = len(counts) / actual_window
    samples = 1
    for index in range(actual_window, len(words)):
        leaving = words[index - actual_window]
        counts[leaving] -= 1
        if counts[leaving] == 0:
            del counts[leaving]
        counts[words[index]] += 1
        total += len(counts) / actual_window
        samples += 1
    return round(total / samples, 4)


def repeated_sentence_openings(sentences: Sequence[str]) -> Dict[str, Any]:
    openings: List[str] = []
    for sentence in sentences:
        tokens = [token.lower() for token in WORD_RE.findall(sentence)]
        if len(tokens) >= 6:
            openings.append(" ".join(tokens[:3]))
    counts = Counter(openings)
    repeated = [(opening, count) for opening, count in counts.items() if count >= 2]
    repeated.sort(key=lambda item: (-item[1], item[0]))
    excess = sum(count - 1 for _, count in repeated)
    rate = round(100.0 * excess / len(openings), 4) if openings else 0.0
    return {
        "value": rate,
        "unit": "percent_excess_repeated_three_word_openings",
        "eligible_sentences": len(openings),
        "repeated_openings": [
            {"opening": opening, "count": count} for opening, count in repeated[:12]
        ],
        "interpretation": (
            "Repeated three-word sentence openings are a template-repetition candidate. "
            "Section conventions and repeated technical subjects are confounders."
        ),
    }


def analyze_text(raw_text: str, source_path: str = "") -> Dict[str, Any]:
    text, scope = prepare_analysis_text(raw_text)
    words_original = WORD_RE.findall(text)
    words = [word.lower() for word in words_original]
    sentences = split_sentences(text)
    paragraphs = [part for part in text.split("\n\n") if len(WORD_RE.findall(part)) >= 5]
    sentence_lengths = [len(WORD_RE.findall(sentence)) for sentence in sentences]
    paragraph_lengths = [len(WORD_RE.findall(paragraph)) for paragraph in paragraphs]
    word_count = len(words)

    negative_parallelism = re.compile(
        r"\bnot\s+(?:only|merely|just)\b.{0,120}?\bbut\s+(?:also|rather)\b",
        re.IGNORECASE | re.DOTALL,
    )
    tricolon = re.compile(
        r"\b[A-Za-z][A-Za-z-]*(?:\s+[A-Za-z][A-Za-z-]*){0,4},\s+"
        r"[A-Za-z][A-Za-z-]*(?:\s+[A-Za-z][A-Za-z-]*){0,4},\s+(?:and|or)\s+"
        r"[A-Za-z][A-Za-z-]*(?:\s+[A-Za-z][A-Za-z-]*){0,4}\b",
        re.IGNORECASE,
    )

    metrics = {
        group_id: phrase_metric(text, group_id, word_count)
        for group_id in PHRASE_GROUPS
    }
    metrics["negative_parallelism_density"] = regex_metric(
        text,
        negative_parallelism,
        word_count,
        "Formulaic 'not only ... but also' candidates; rhetorical use is common in human prose.",
    )
    metrics["tricolon_candidate_density"] = regex_metric(
        text,
        tricolon,
        word_count,
        "Three-part list candidates; this is a noisy structural count, not an AI signature.",
    )
    em_dash_count = text.count("—") + text.count("–")
    metrics["em_dash_density"] = {
        "value": _density(em_dash_count, word_count),
        "unit": "dashes_per_1000_words",
        "count": em_dash_count,
        "interpretation": "Dash density is a style descriptor only; typography and publisher conversion vary.",
    }
    metrics["repeated_sentence_opening_rate"] = repeated_sentence_openings(sentences)
    metrics["sentence_length_variation"] = {
        "mean_words": round(statistics.fmean(sentence_lengths), 4) if sentence_lengths else None,
        "median_words": round(statistics.median(sentence_lengths), 4) if sentence_lengths else None,
        "coefficient_of_variation": coefficient_of_variation(sentence_lengths),
        "sample_size": len(sentence_lengths),
        "interpretation": (
            "Sentence-length variation is descriptive. Low variation may reflect templates, "
            "but venue style, equations, and extraction quality can dominate it."
        ),
    }
    metrics["paragraph_length_variation"] = {
        "mean_words": round(statistics.fmean(paragraph_lengths), 4) if paragraph_lengths else None,
        "median_words": round(statistics.median(paragraph_lengths), 4) if paragraph_lengths else None,
        "coefficient_of_variation": coefficient_of_variation(paragraph_lengths),
        "sample_size": len(paragraph_lengths),
        "interpretation": "Paragraph-length variation is descriptive and sensitive to PDF extraction.",
    }
    metrics["moving_average_type_token_ratio"] = {
        "value": moving_average_type_token_ratio(words),
        "window_words": min(100, word_count) if word_count else 0,
        "unit": "MATTR",
        "interpretation": (
            "Length-adjusted lexical diversity is context only. It must not be converted into "
            "an authorship score, especially for non-native English writing."
        ),
    }

    return {
        "schema_version": 1,
        "method_version": METHOD_VERSION,
        "source_path": source_path,
        "source_sha256": hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest(),
        "analysis_scope": scope,
        "document_counts": {
            "words": word_count,
            "sentences": len(sentences),
            "paragraphs": len(paragraphs),
        },
        "metrics": metrics,
        "method_notes": [
            "All values are descriptive candidates for manual review; no probability or AI-authorship label is computed.",
            "Quoted material, bibliography leakage, PDF column order, equations, venue templates, and copy editing can change the values.",
            "Population-level lexical findings are not calibrated for individual-paper classification.",
        ],
    }


def analyze_file(path: Path) -> Dict[str, Any]:
    raw_text = path.read_text(encoding="utf-8", errors="replace")
    return analyze_text(raw_text, str(path.resolve()))


def write_metrics_json(metrics: Dict[str, Any], output_path: Path) -> Path:
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(metrics, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=output_path.parent, delete=False
    ) as handle:
        handle.write(payload)
        temporary_path = Path(handle.name)
    os.replace(temporary_path, output_path)
    return output_path


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--input", required=True, help="extracted UTF-8 paper text")
    root.add_argument("--output", required=True, help="output JSON path")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    output = write_metrics_json(analyze_file(Path(args.input)), Path(args.output))
    print(json.dumps({"ok": True, "metrics": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
