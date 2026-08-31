#!/usr/bin/env python3
"""Normalize multi-engine Sanskrit parser output.

Reads raw output from sanskrit_parser, Dharmamitra API, and vidyut engines,
then produces a compact normalized JSON output with:
- Deduplicated analyses across engines
- Ranked candidates (best + top alternatives)
- Separated sandhi, compound, and morphology
- Removed parser internals and debug structures
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


# ---------------------------------------------------------------------------
# Canonical tag normalization
# ---------------------------------------------------------------------------

VIBHAKTI_MAP = {
    "प्रथमा": "प्रथमा",
    "प्रथमाविभक्तिः": "प्रथमा",
    "द्वितीया": "द्वितीया",
    "द्वितीयाविभक्तिः": "द्वितीया",
    "तृतीया": "तृतीया",
    "तृतीयाविभक्तिः": "तृतीया",
    "चतुर्थी": "चतुर्थी",
    "चतुर्थीविभक्तिः": "चतुर्थी",
    "पञ्चमी": "पञ्चमी",
    "पञ्चमीविभक्तिः": "पञ्चमी",
    "षष्ठी": "षष्ठी",
    "षष्ठीविभक्तिः": "षष्ठी",
    "सप्तमी": "सप्तमी",
    "सप्तमीविभक्तिः": "सप्तमी",
    "सम्बोधनम्": "सम्बोधन",
    "संबोधनविभक्तिः": "सम्बोधन",
}

VACANA_MAP = {
    "एकवचनम्": "एकवचनम्",
    "एक": "एकवचनम्",
    "द्विवचनम्": "द्विवचनम्",
    "द्वि": "द्विवचनम्",
    "बहुवचनम्": "बहुवचनम्",
    "बहु": "बहुवचनम्",
}

LINGA_MAP = {
    "पुंल्लिङ्गम्": "पुंल्लिङ्गम्",
    "पुं": "पुंल्लिङ्गम्",
    "स्त्रीलिङ्गम्": "स्त्रीलिङ्गम्",
    "स्त्री": "स्त्रीलिङ्गम्",
    "नपुंसकलिङ्गम्": "नपुंसकलिङ्गम्",
    "नपुंसक": "नपुंसकलिङ्गम्",
}


def normalize_vibhakti(tag: str) -> str:
    """Normalize vibhakti (case) tag to canonical form."""
    return VIBHAKTI_MAP.get(tag, tag)


def normalize_vacana(tag: str) -> str:
    """Normalize vacana (number) tag to canonical form."""
    return VACANA_MAP.get(tag, tag)


def normalize_linga(tag: str) -> str:
    """Normalize linga (gender) tag to canonical form."""
    return LINGA_MAP.get(tag, tag)


def normalize_morphological_tags(
    tags: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Normalize morphological tags to canonical form.

    Returns list of dicts with keys: root, vibhakti, vacana, linga.
    Deduplicates identical canonical forms.
    """
    seen: Set[Tuple[str, ...]] = set()
    result: List[Dict[str, str]] = []

    for tag_group in tags:
        root = tag_group.get("root", "")
        tag_list = tag_group.get("tags", [])

        vibhakti = ""
        vacana = ""
        linga = ""

        for t in tag_list:
            tn = t.strip()
            if tn in VIBHAKTI_MAP or "विभक्ति" in tn or "विभक्तिः" in tn:
                vibhakti = normalize_vibhakti(tn)
            elif tn in VACANA_MAP or "वचन" in tn:
                vacana = normalize_vacana(tn)
            elif tn in LINGA_MAP or "लिङ्ग" in tn:
                linga = normalize_linga(tn)

        if not (vibhakti or vacana or linga):
            continue

        canonical = (root, vibhakti, vacana, linga)
        if canonical not in seen:
            seen.add(canonical)
            entry: Dict[str, str] = {"root": root}
            if vibhakti:
                entry["vibhakti"] = vibhakti
            if vacana:
                entry["vacana"] = vacana
            if linga:
                entry["linga"] = linga
            result.append(entry)

    return result


# ---------------------------------------------------------------------------
# Candidate extraction and scoring
# ---------------------------------------------------------------------------

def extract_candidates_from_sanskrit_parser(
    sp_output: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Extract word-level candidates from sanskrit_parser output."""
    candidates: List[Dict[str, Any]] = []

    for split_entry in sp_output.get("sandhi_splits", []):
        split_words = split_entry.get("split", [])
        items = split_entry.get("items", [])

        for i, item in enumerate(items):
            if i >= len(split_words):
                break

            word = item.get("pada", "")
            morph_tags = item.get("morphological_tags", [])
            normalized = normalize_morphological_tags(morph_tags)

            if normalized:
                candidates.append({
                    "surface": word,
                    "sources": ["sanskrit_parser"],
                    "morphology": normalized,
                    "score": 3,
                })

    return candidates


def extract_candidates_from_dharmamitra(
    dm_output: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Extract word-level candidates from Dharmamitra API output."""
    candidates: List[Dict[str, Any]] = []

    tokens = dm_output.get("tokens", [])
    for token in tokens:
        form = token.get("form", "")
        if "|" in form:
            parts = form.split("|")
            surface = parts[0]
            candidates.append({
                "surface": surface,
                "sources": ["dharmamitra"],
                "morphology": [{"root": surface}],
                "score": 3,
            })

    return candidates


def extract_candidates_from_vidyut(
    v_output: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Extract word-level candidates from vidyut output."""
    candidates: List[Dict[str, Any]] = []

    for word_entry in v_output.get("kosha", []):
        devanagari = word_entry.get("devanagari", "")
        grammatical_entries = word_entry.get("grammatical_entries", [])
        is_compound = word_entry.get("is_compound", False)

        if grammatical_entries:
            morphology: List[Dict[str, str]] = []
            for entry in grammatical_entries:
                morph: Dict[str, str] = {}
                if "pratipadika" in entry:
                    morph["root"] = entry["pratipadika"]
                if "linga" in entry:
                    morph["linga"] = entry["linga"]
                if "vibhakti" in entry:
                    morph["vibhakti"] = entry["vibhakti"]
                if "vacana" in entry:
                    morph["vacana"] = entry["vacana"]
                if morph:
                    morphology.append(morph)

            if morphology:
                candidates.append({
                    "surface": devanagari,
                    "sources": ["vidyut"],
                    "morphology": morphology,
                    "score": 2,
                    "is_compound": is_compound,
                })

    return candidates


def deduplicate_candidates(
    candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Deduplicate candidates by surface form + canonical morphology."""
    seen: Dict[str, List[Dict[str, Any]]] = {}

    for cand in candidates:
        surface = cand.get("surface", "")
        morph_key = tuple(
            sorted(tuple(sorted(m.items())) for m in cand.get("morphology", []))
        )
        key = f"{surface}::{morph_key}"

        if key not in seen:
            seen[key] = []
        seen[key].append(cand)

    result: List[Dict[str, Any]] = []
    for key, group in seen.items():
        all_sources: Set[str] = set()
        for c in group:
            all_sources.update(c.get("sources", []))

        morphology = group[0].get("morphology", [])

        total_score = sum(c.get("score", 0) for c in group)
        if len(all_sources) > 1:
            total_score += len(all_sources) * 2

        result.append({
            "surface": group[0].get("surface", ""),
            "sources": sorted(all_sources),
            "morphology": morphology,
            "score": total_score,
            "is_compound": any(c.get("is_compound", False) for c in group),
        })

    return result


def rank_candidates(
    candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Rank candidates by score, keeping only top candidates."""
    ranked = sorted(candidates, key=lambda c: c.get("score", 0), reverse=True)

    by_surface: Dict[str, List[Dict[str, Any]]] = {}
    for cand in ranked:
        surface = cand.get("surface", "")
        if surface not in by_surface:
            by_surface[surface] = []
        if len(by_surface[surface]) < 3:
            by_surface[surface].append(cand)

    result: List[Dict[str, Any]] = []
    for surface in sorted(by_surface.keys()):
        for cand in by_surface[surface]:
            output_cand = {k: v for k, v in cand.items() if k != "score"}
            result.append(output_cand)

    return result


def limit_morphological_alternatives(
    normalized: Dict[str, Any],
    max_alternatives: int = 3,
    max_tags_per_analysis: int = 4,
) -> Dict[str, Any]:
    """Limit the number of morphological alternatives per word.

    Keeps only the top N alternatives based on specificity
    (more morphological tags = higher confidence).
    Uses source count as tiebreaker.
    Also limits the number of morph tags within each analysis.
    """
    for pada in normalized.get("padas", []):
        analyses = pada.get("analysis", [])
        if len(analyses) > max_alternatives:
            # Sort by number of morphological tags (more = more specific),
            # then by number of sources (more = higher confidence)
            analyses.sort(
                key=lambda a: (
                    sum(len(m) for m in a.get("morphology", [])),
                    len(a.get("sources", [])),
                ),
                reverse=True,
            )
            pada["analysis"] = analyses[:max_alternatives]

        # Also limit morph tags within each analysis
        for analysis in pada.get("analysis", []):
            morphology = analysis.get("morphology", [])
            if len(morphology) > max_tags_per_analysis:
                # Sort by number of keys (more = more specific)
                morphology.sort(
                    key=lambda m: len(m),
                    reverse=True,
                )
                analysis["morphology"] = morphology[:max_tags_per_analysis]

    return normalized


# ---------------------------------------------------------------------------
# Output generation
# ---------------------------------------------------------------------------

def generate_normalized_output(
    raw_output: Dict[str, Any],
) -> Dict[str, Any]:
    """Generate normalized output from raw multi-engine output."""
    engine_outputs = raw_output.get("engine_outputs", {})

    sp_candidates = extract_candidates_from_sanskrit_parser(
        engine_outputs.get("sanskrit_parser", {})
    )
    dm_candidates = extract_candidates_from_dharmamitra(
        engine_outputs.get("dharmamitra", {})
    )
    v_candidates = extract_candidates_from_vidyut(
        engine_outputs.get("vidyut", {})
    )

    all_candidates = sp_candidates + dm_candidates + v_candidates

    deduped = deduplicate_candidates(all_candidates)
    ranked = rank_candidates(deduped)

    normalized: Dict[str, Any] = {
        "input": raw_output.get("input", {}),
        "mode": raw_output.get("mode", ""),
        "padas": [],
    }

    by_surface: Dict[str, Dict[str, Any]] = {}
    for cand in ranked:
        surface = cand.get("surface", "")
        if surface not in by_surface:
            by_surface[surface] = {
                "surface": surface,
                "analysis": [],
            }
        by_surface[surface]["analysis"].append({
            "morphology": cand.get("morphology", []),
            "sources": cand.get("sources", []),
            "is_compound": cand.get("is_compound", False),
        })

    for surface in sorted(by_surface.keys()):
        normalized["padas"].append(by_surface[surface])

    # Limit morphological alternatives
    normalized = limit_morphological_alternatives(normalized)

    return normalized


def main() -> int:
    """Main entry point for normalization."""
    script_dir = Path(__file__).resolve().parent
    raw_output_path = script_dir / "output.json"
    normalized_output_path = script_dir / "output.normalized.json"

    try:
        with open(raw_output_path, "r", encoding="utf-8") as f:
            raw_output = json.load(f)
    except FileNotFoundError:
        print(f"Error: Raw output not found: {raw_output_path}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {raw_output_path}: {e}", file=sys.stderr)
        return 1

    normalized = generate_normalized_output(raw_output)

    try:
        with open(normalized_output_path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error writing output: {e}", file=sys.stderr)
        return 1

    raw_size = raw_output_path.stat().st_size
    norm_size = normalized_output_path.stat().st_size
    reduction = (1 - norm_size / raw_size) * 100

    print(f"Raw output: {raw_size:,} bytes")
    print(f"Normalized output: {norm_size:,} bytes")
    print(f"Reduction: {reduction:.1f}%")
    print(f"Words analyzed: {len(normalized.get('padas', []))}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
