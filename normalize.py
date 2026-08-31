#!/usr/bin/env python3
"""Normalize multi-engine Sanskrit parser output.

Produces a compact, linguistically meaningful normalized JSON output
using source priority: Dharmamitra > sanskrit_parser > Vidyut.

Key principles:
- Normalize by actual surface pada, not parser fragments
- Never leak substring analyses
- Determine segmentation before morphology
- Separate sandhi from compounds
- Prefer simplest linguistically justified analysis
- Deduplicate cross-engine results
- Keep only best morphology
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


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


def extract_morphology_from_tags(
    tags: List[Dict[str, Any]],
) -> Optional[Dict[str, str]]:
    """Extract canonical morphology from morphological tags.

    Returns a single dict with keys: lemma, vibhakti, vacana, linga.
    Returns None if no valid morphology found.
    """
    if not tags:
        return None

    lemma = ""
    vibhakti = ""
    vacana = ""
    linga = ""

    for tag_group in tags:
        root = tag_group.get("root", "")
        tag_list = tag_group.get("tags", [])

        if not lemma and root:
            lemma = root

        for t in tag_list:
            tn = t.strip()
            if tn in VIBHAKTI_MAP or "विभक्ति" in tn or "विभक्तिः" in tn:
                vibhakti = normalize_vibhakti(tn)
            elif tn in VACANA_MAP or "वचन" in tn:
                vacana = normalize_vacana(tn)
            elif tn in LINGA_MAP or "लिङ्ग" in tn:
                linga = normalize_linga(tn)

    if not lemma and not (vibhakti or vacana or linga):
        return None

    result: Dict[str, str] = {}
    if lemma:
        result["lemma"] = lemma
    if vibhakti:
        result["vibhakti"] = vibhakti
    if vacana:
        result["vacana"] = vacana
    if linga:
        result["linga"] = linga

    return result if result else None


# ---------------------------------------------------------------------------
# Candidate extraction with source priority
def extract_dharmamitra_lemmas(
    dm_output: Dict[str, Any],
) -> Dict[str, str]:
    """Extract lemma mapping from Dharmamitra API output.

    Returns dict mapping Devanagari form to lemma.
    Dharmamitra is primary authority for morphology/lemma.
    """
    lemma_map: Dict[str, str] = {}

    try:
        from vidyut.lipi import transliterate, Scheme

        tokens = dm_output.get("tokens", [])
        for token in tokens:
            form = token.get("form", "")
            if "|" in form:
                parts = form.split("|")
                iast_form = parts[0]
                # Transliterate IAST to Devanagari
                try:
                    devanagari = transliterate(iast_form, Scheme.Iast, Scheme.Devanagari)
                except Exception:
                    devanagari = iast_form
                # Dharmamitra format: lemma|POS|...
                # Use first part as lemma (simplified)
                lemma = iast_form
                lemma_map[devanagari] = lemma
    except ImportError:
        pass

    return lemma_map

def extract_sanskrit_parser_surface_forms(
    sp_output: Dict[str, Any],
) -> List[List[str]]:
    """Extract surface forms from sanskrit_parser sandhi splits.

    Returns list of split word lists, preferring first split.
    sanskrit_parser is secondary authority for surface forms.
    """
    sandhi_splits = sp_output.get("sandhi_splits", [])
    if not sandhi_splits:
        return []

    # Return first 3 splits (best to worst)
    return [split.get("split", []) for split in sandhi_splits[:3]]


def extract_vidyut_compound_evidence(
    v_output: Dict[str, Any],
) -> Dict[str, List[str]]:
    """Extract compound evidence from vidyut output.

    Returns dict keyed by compound surface form with sandhi splits.
    Vidyut is supporting/fallback evidence only.
    """
    compounds: Dict[str, List[str]] = {}

    for word_entry in v_output.get("kosha", []):
        devanagari = word_entry.get("devanagari", "")
        is_compound = word_entry.get("is_compound", False)
        sandhi_splits_v = word_entry.get("sandhi_splits", [])

        if is_compound and sandhi_splits_v:
            compounds[devanagari] = sandhi_splits_v[:2]  # Limit to 2 splits

    return compounds


# ---------------------------------------------------------------------------
# Normalization and adjudication
# ---------------------------------------------------------------------------

def normalize_surface_padas(
    raw_output: Dict[str, Any],
) -> Dict[str, Any]:
    """Normalize output around actual surface pada.

    Uses source priority: Dharmamitra > sanskrit_parser > Vidyut.
    Never exposes parser fragments as words.
    """
    engine_outputs = raw_output.get("engine_outputs", {})

    # Extract data from each engine
    dm_lemmas = extract_dharmamitra_lemmas(
        engine_outputs.get("dharmamitra", {})
    )
    sp_surface_forms = extract_sanskrit_parser_surface_forms(
        engine_outputs.get("sanskrit_parser", {})
    )
    v_compounds = extract_vidyut_compound_evidence(
        engine_outputs.get("vidyut", {})
    )

    # Use first sandhi split as primary surface forms
    if not sp_surface_forms:
        return {
            "input": raw_output.get("input", {}),
            "mode": raw_output.get("mode", ""),
            "padas": [],
        }

    primary_split = sp_surface_forms[0]

    # Build normalized output around surface forms from sanskrit_parser
    # (since they're in Devanagari and represent actual sandhi splits)
    padas: List[Dict[str, Any]] = []
    seen_surfaces: Set[str] = set()

    for surface in primary_split:
        if surface in seen_surfaces:
            continue
        seen_surfaces.add(surface)

        pada_entry: Dict[str, Any] = {"surface": surface}

        # Check if this surface form has Dharmamitra lemma support
        # (compare IAST forms - simplified matching)
        if surface in dm_lemmas:
            pada_entry["lemma"] = dm_lemmas[surface]

        # Check if this is a compound (from Vidyut evidence)
        if surface in v_compounds:
            pada_entry["sandhi"] = v_compounds[surface]

        # Add morphology from sanskrit_parser (secondary authority)
        sp_output = engine_outputs.get("sanskrit_parser", {})
        for split_entry in sp_output.get("sandhi_splits", []):
            split_words = split_entry.get("split", [])
            items = split_entry.get("items", [])

            for i, item in enumerate(items):
                if i >= len(split_words):
                    break

                word = item.get("pada", "")
                if word == surface:
                    morph_tags = item.get("morphological_tags", [])
                    morphology = extract_morphology_from_tags(morph_tags)
                    if morphology:
                        pada_entry["analysis"] = morphology
                    break

        padas.append(pada_entry)

    # Build final output
    normalized: Dict[str, Any] = {
        "input": raw_output.get("input", {}),
        "mode": raw_output.get("mode", ""),
        "padas": padas,
    }

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

    normalized = normalize_surface_padas(raw_output)

    try:
        with open(normalized_output_path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"Error writing output: {e}", file=sys.stderr)
        return 1

    # Print summary
    raw_size = raw_output_path.stat().st_size
    norm_size = normalized_output_path.stat().st_size
    reduction = (1 - norm_size / raw_size) * 100

    print(f"Raw output: {raw_size:,} bytes")
    print(f"Normalized output: {norm_size:,} bytes")
    print(f"Reduction: {reduction:.1f}%")
    print(f"Surface padas: {len(normalized.get('padas', []))}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
