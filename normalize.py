#!/usr/bin/env python3
"""Normalize multi-engine Sanskrit parser output.

Produces a compact, linguistically meaningful normalized JSON output
using source priority: Dharmamitra > sanskrit_parser > Vidyut.

Pipeline: surface -> segmentation -> sandhi restoration -> compound decomposition -> morphology

Key design principles:
- Normalize by actual surface pada, not parser fragments
- Never leak substring analyses (e.g., don't expose "इ" from "इव")
- Determine segmentation before morphology
- Separate sandhi from compounds (वागर्थाविव → वागर्थौ + इव → वाक् + अर्थौ + इव)
- Prefer simplest linguistically justified analysis
- Deduplicate cross-engine results
- Keep only best morphology (one selected analysis, not arrays of candidates)
- Remove unnecessary fields (parser indexes, costs, API endpoints, etc.)

Usage:
    uv run python normalize.py

Input: output.json (raw multi-engine output)
Output: output.normalized.json (compact, normalized output)

Example reduction: ~100KB raw -> ~3KB normalized (97% reduction)
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Canonical tag normalization maps
# ---------------------------------------------------------------------------
# These maps normalize various tag formats to canonical Devanagari forms.
# Different engines may use different representations (e.g., "प्रथमा" vs "प्रथमाविभक्तिः")
# We normalize to a single canonical form for consistency.

VIBHAKTI_MAP = {
    "प्रथमा": "प्रथमा", "प्रथमाविभक्तिः": "प्रथमा",
    "द्वितीया": "द्वितीया", "द्वितीयाविभक्तिः": "द्वितीया",
    "तृतीया": "तृतीया", "तृतीयाविभक्तिः": "तृतीया",
    "चतुर्थी": "चतुर्थी", "चतुर्थीविभक्तिः": "चतुर्थी",
    "पञ्चमी": "पञ्चमी", "पञ्चमीविभक्तिः": "पञ्चमी",
    "षष्ठी": "षष्ठी", "षष्ठीविभक्तिः": "षष्ठी",
    "सप्तमी": "सप्तमी", "सप्तमीविभक्तिः": "सप्तमी",
    "सम्बोधनम्": "सम्बोधन", "संबोधनविभक्तिः": "सम्बोधन",
}

VACANA_MAP = {
    "एकवचनम्": "एकवचनम्", "एक": "एकवचनम्",
    "द्विवचनम्": "द्विवचनम्", "द्वि": "द्विवचनम्",
    "बहुवचनम्": "बहुवचनम्", "बहु": "बहुवचनम्",
}

LINGA_MAP = {
    "पुंल्लिङ्गम्": "पुंल्लिङ्गम्", "पुं": "पुंल्लिङ्गम्",
    "स्त्रीलिङ्गम्": "स्त्रीलिङ्गम्", "स्त्री": "स्त्रीलिङ्गम्",
    "नपुंसकलिङ्गम्": "नपुंसकलिङ्गम्", "नपुंसक": "नपुंसकलिङ्गम्",
}


def norm_vibhakti(tag: str) -> str:
    """Normalize vibhakti (case/सम्बोधन) tag to canonical Devanagari form.
    
    Args:
        tag: Raw vibhakti tag from any engine (e.g., "प्रथमा", "प्रथमाविभक्तिः")
    
    Returns:
        Canonical vibhakti form (e.g., "प्रथमा")
    """
    return VIBHAKTI_MAP.get(tag, tag)


def norm_vacana(tag: str) -> str:
    """Normalize vacana (number) tag to canonical Devanagari form.
    
    Args:
        tag: Raw vacana tag from any engine (e.g., "एक", "एकवचनम्")
    
    Returns:
        Canonical vacana form (e.g., "एकवचनम्")
    """
    return VACANA_MAP.get(tag, tag)


def norm_linga(tag: str) -> str:
    """Normalize linga (gender) tag to canonical Devanagari form.
    
    Args:
        tag: Raw linga tag from any engine (e.g., "पुं", "पुंल्लिङ्गम्")
    
    Returns:
        Canonical linga form (e.g., "पुंल्लिङ्गम्")
    """
    return LINGA_MAP.get(tag, tag)


# ---------------------------------------------------------------------------
# Morphology extraction
# ---------------------------------------------------------------------------
# These functions extract canonical morphological information from raw engine output.
# They produce a minimal dict with keys: lemma, vibhakti, vacana, linga (for nouns)
# or lemma, lakara, purusa, vacana (for verbs).

def extract_morph_from_tags(tags: List[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """Extract canonical morphology from sanskrit_parser morphological tags.
    
    Parses the morphological_tags structure from sanskrit_parser output and
    extracts lemma, vibhakti, vacana, and linga into a minimal dict.
    
    Args:
        tags: List of morphological tag dicts from sanskrit_parser, e.g.:
              [{"root": "वच्", "tags": ["द्विवचनम्", "प्रथमा", "स्त्रीलिङ्गम्"]}]
    
    Returns:
        Dict with keys lemma/vibhakti/vacana/linga, or None if no valid morphology found.
        Example: {"lemma": "वच्", "vibhakti": "प्रथमा", "vacana": "द्विवचनम्", "linga": "स्त्रीलिङ्गम्"}
    """
    if not tags:
        return None

    lemma = vibhakti = vacana = linga = ""

    for tg in tags:
        root = tg.get("root", "")
        if not lemma and root:
            lemma = root
        for t in tg.get("tags", []):
            tn = t.strip()
            if "विभक्ति" in tn:
                vibhakti = norm_vibhakti(tn)
            elif "वचन" in tn:
                vacana = norm_vacana(tn)
            elif "लिङ्ग" in tn:
                linga = norm_linga(tn)

    if not lemma and not (vibhakti or vacana or linga):
        return None

    r: Dict[str, str] = {}
    if lemma: r["lemma"] = lemma
    if vibhakti: r["vibhakti"] = vibhakti
    if vacana: r["vacana"] = vacana
    if linga: r["linga"] = linga
    return r if r else None


def extract_morph_from_dm_token(token: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Extract morphology from a Dharmamitra API token.
    
    Dharmamitra tokens have format: "lemma|POS|vibhakti|vacana|linga|..."
    For nouns: extracts lemma, vibhakti, vacana, linga
    For verbs: extracts lemma, lakara, purusa, vacana
    
    Args:
        token: Dharmamitra token dict with "form" key, e.g.:
               {"form": "vāc|noun|द्वितीया|द्विवचनम्|पुंल्लिङ्गम्"}
    
    Returns:
        Dict with morphological fields, or None if token format is invalid.
    """
    form = token.get("form", "")
    if "|" not in form:
        return None

    parts = form.split("|")
    lemma = parts[0]
    pos = parts[1] if len(parts) > 1 else ""

    r: Dict[str, str] = {"lemma": lemma}

    if pos in ("noun", "n", "adj", "a"):
        if len(parts) > 2: r["vibhakti"] = parts[2]
        if len(parts) > 3: r["vacana"] = parts[3]
        if len(parts) > 4: r["linga"] = parts[4]
    elif pos in ("verb", "v"):
        if len(parts) > 2: r["lakara"] = parts[2]
        if len(parts) > 3: r["purusa"] = parts[3]
        if len(parts) > 4: r["vacana"] = parts[4]

    return r if r else None


# ---------------------------------------------------------------------------
# Engine data extraction
# ---------------------------------------------------------------------------
# These functions extract structured data from each engine's raw output.
# They normalize the data into a common format for the alignment step.

def extract_dm_sequence(dm_output: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract Dharmamitra lexical sequence with IAST and Devanagari forms.
    
    Dharmamitra tokens may be plain IAST ("vāc") or pipe-separated
    ("vāc|noun|द्वितीया|द्विवचनम्|पुंल्लिङ्गम्"). This function handles both formats.
    
    The sequence represents the sandhi-split tokens from Dharmamitra, which is
    the primary authority for lexical identity and segmentation.
    
    Args:
        dm_output: Dharmamitra API response with "tokens" list
    
    Returns:
        List of dicts with keys: iast, devanagari, morphology
        Example: [{"iast": "vāc", "devanagari": "वाच्", "morphology": {"lemma": "vāc"}}]
    """
    sequence: List[Dict[str, str]] = []
    try:
        from vidyut.lipi import transliterate, Scheme
    except ImportError:
        return sequence

    for token in dm_output.get("tokens", []):
        form = token.get("form", "")
        if not form:
            continue

        # Handle both plain IAST and pipe-separated formats
        if "|" in form:
            parts = form.split("|")
            iast = parts[0].replace("\u1e43", "m")  # Normalize anusvara for consistent matching
            morph = extract_morph_from_dm_token(token)
        else:
            iast = form.replace("\u1e43", "m")
            morph = None

        try:
            dev = transliterate(iast, Scheme.Iast, Scheme.Devanagari)
        except Exception:
            dev = iast

        if morph:
            morph["lemma"] = iast

        sequence.append({
            "iast": iast, "devanagari": dev,
            "morphology": morph,
        })
    return sequence


def extract_sp_splits(sp_output: Dict[str, Any]) -> List[Tuple[List[str], List[Dict[str, Any]]]]:
    """Extract sanskrit_parser sandhi splits with morphology.
    
    Returns the first (best) sandhi split as a list of (surface_forms, morphological_data) tuples.
    sanskrit_parser provides the surface forms (sandhi-split words) and their morphological tags.
    
    Args:
        sp_output: sanskrit_parser response with "sandhi_splits" list
    
    Returns:
        List of (split_words, items) tuples, where split_words is a list of Devanagari strings
        and items is a list of dicts containing morphological_tags for each word.
    """
    return [
        (s.get("split", []), s.get("items", []))
        for s in sp_output.get("sandhi_splits", [])
    ]


def extract_vidyut_compounds(v_output: Dict[str, Any]) -> Dict[str, List[str]]:
    """Extract Vidyut compound evidence (supporting only).
    
    Vidyut provides recursive compound decomposition via DFS through the kosha dictionary.
    This is used as supporting/fallback evidence only, never to override Dharmamitra analysis.
    
    Args:
        v_output: Vidyut response with "kosha" list
    
    Returns:
        Dict mapping compound surface form to list of sandhi split strings.
        Example: {"वागर्थाविव": ["वागर्थ + अविव"]}
    """
    compounds: Dict[str, List[str]] = {}
    for w in v_output.get("kosha", []):
        dev = w.get("devanagari", "")
        if w.get("is_compound") and w.get("sandhi_splits"):
            compounds[dev] = w["sandhi_splits"][:2]
    return compounds


# ---------------------------------------------------------------------------
# Alignment: Dharmamitra tokens to sanskrit_parser surface forms
# ---------------------------------------------------------------------------
# This is the core adjudication logic. It matches Dharmamitra's lexical sequence
# to sanskrit_parser's surface forms, identifying compounds where multiple
# Dharmamitra tokens map to a single surface form.
#
# Key challenge: Dharmamitra gives sandhi-split tokens (vāc, arthau, iva)
# while sanskrit_parser gives surface forms (vāgarthau, iva).
# vāgarthau = vāc + arthau (sandhi combination), so vāc is NOT a substring of vāgarthau.
# We must check if DM tokens can be combined via sandhi rules to form SP forms.

def align_dm_to_sp(
    dm_seq: List[Dict[str, str]],
    sp_splits: List[Tuple[List[str], List[Dict[str, Any]]]],
) -> List[Dict[str, Any]]:
    """Align Dharmamitra tokens to sanskrit_parser surface forms.
    
    Uses Dharmamitra as primary segmentation authority. Matches IAST forms
    to Devanagari surface forms via transliteration. Identifies compounds
    where multiple Dharmamitra tokens map to one surface form.
    
    Matching algorithm (in priority order):
    1. Sandhi combination: Check if two consecutive DM tokens combine to form SP form
       (e.g., vāc + arthau -> vāgarthau via visarga sandhi)
    2. Direct match: DM token IAST equals SP word IAST (e.g., iva == iva)
    3. Substring match: DM token is substring of SP word (e.g., arthau in vāgarthau)
    
    Args:
        dm_seq: Dharmamitra lexical sequence from extract_dm_sequence()
        sp_splits: sanskrit_parser sandhi splits from extract_sp_splits()
    
    Returns:
        List of normalized pada entries, each with:
        - surface: Devanagari surface form
        - analysis: Morphological dict (lemma, vibhakti, vacana, linga)
        - compound: Optional dict with parts list (for compounds)
    """
    if not dm_seq:
        return []

    try:
        from vidyut.lipi import transliterate, Scheme
    except ImportError:
        return _align_dm_to_sp_devanagari(dm_seq, sp_splits)

    best_words, best_items = sp_splits[0]

    # Transliterate sanskrit_parser words to IAST for matching
    # Normalize anusvara: ṃ (U+1E43) -> m for consistent matching
    sp_iast: Dict[str, str] = {}
    for w in best_words:
        try:
            sp_iast[w] = transliterate(w, Scheme.Devanagari, Scheme.Iast).replace("\u1e43", "m")
        except Exception:
            sp_iast[w] = w

    # Build sanskrit_parser morphology lookup for fallback
    sp_morph: Dict[str, Dict[str, str]] = {}
    for i, word in enumerate(best_words):
        if i < len(best_items):
            m = extract_morph_from_tags(best_items[i].get("morphological_tags", []))
            if m:
                sp_morph[word] = m

    padas: List[Dict[str, Any]] = []
    di = 0  # Dharmamitra sequence index

    for sp_word in best_words:
        entry: Dict[str, Any] = {"surface": sp_word}
        sp_i = sp_iast.get(sp_word, sp_word)

        # Collect Dharmamitra tokens that match this surface form
        matched: List[Dict[str, str]] = []

        if di < len(dm_seq):
            dm_tok = dm_seq[di]
            dm_i = dm_tok["iast"]

            # Priority 1: Check sandhi combination first (vāc + arthau -> vāgarthau)
            # This handles cases where DM tokens combine via sandhi rules
            if di + 1 < len(dm_seq):
                next_tok = dm_seq[di + 1]
                combined = dm_i + next_tok["iast"]
                if combined == sp_i or _sandhi_combine(dm_i, next_tok["iast"]) == sp_i:
                    matched.append(dm_tok)
                    matched.append(next_tok)
                    di += 2
            # Priority 2: Direct match (iva == iva)
            elif dm_i == sp_i:
                matched.append(dm_tok)
                di += 1
            # Priority 3: Substring match (arthau in vāgarthau)
            elif dm_i in sp_i:
                matched.append(dm_tok)
                di += 1
                # Check if next DM token also fits this surface form
                while di < len(dm_seq):
                    nxt = dm_seq[di]
                    if nxt["iast"] in sp_i:
                        matched.append(nxt)
                        di += 1
                    else:
                        break

        # Build entry with morphology and compound info
        if matched:
            if len(matched) == 1:
                # Single DM token match - use its morphology
                entry["analysis"] = matched[0]["morphology"]
            else:
                # Compound: multiple DM tokens -> one surface form
                # Use first DM token's morphology if available, else fall back to SP
                if matched[0]["morphology"]:
                    entry["analysis"] = matched[0]["morphology"]
                elif sp_word in sp_morph:
                    entry["analysis"] = sp_morph[sp_word]
                entry["compound"] = {
                    "surface": sp_word,
                    "parts": [t["devanagari"] for t in matched],
                }
        elif sp_word in sp_morph:
            # No DM match - fall back to sanskrit_parser morphology
            entry["analysis"] = sp_morph[sp_word]
        
        padas.append(entry)

    return padas


def _sandhi_combine(a: str, b: str) -> str:
    """Combine two IAST tokens as sandhi would, for matching purposes.
    
    Implements simplified sandhi rules to check if two DM tokens could combine
    to form an SP surface form. This is used for matching, not for generating
    correct sandhi output.
    
    Implemented rules:
    - Visarga sandhi: c/h + a -> g/j + a (e.g., vāc + arthau -> vāgarthau)
    - au + a -> ao, i + a -> ea, u + a -> oa
    
    Args:
        a: First IAST token (e.g., "vāc")
        b: Second IAST token (e.g., "arthau")
    
    Returns:
        Combined IAST string after applying sandhi rules.
        Example: _sandhi_combine("vāc", "arthau") -> "vāgarthau"
    """
    combined = a + b

    # Visarga sandhi: c/h + a -> g/j + a
    if a.endswith("c") and b.startswith("a"):
        combined = a[:-1] + "g" + b  # c + a -> ga
    elif a.endswith("h") and b.startswith("a"):
        combined = a[:-1] + "j" + b  # h + a -> ja
    # Other common sandhi rules
    elif combined.endswith("au") and b.startswith("a"):
        combined = combined[:-1] + "o"  # au + a -> ao
    elif combined.endswith("i") and b.startswith("a"):
        combined = combined[:-1] + "e"  # i + a -> ea
    elif combined.endswith("u") and b.startswith("a"):
        combined = combined[:-1] + "o"  # u + a -> oa

    return combined


def _align_dm_to_sp_devanagari(
    dm_seq: List[Dict[str, str]],
    sp_splits: List[Tuple[List[str], List[Dict[str, Any]]]],
) -> List[Dict[str, Any]]:
    """Fallback alignment using Devanagari matching (no IAST transliteration).
    
    Used when vidyut is not available for transliteration. Falls back to
    Devanagari substring matching, which is less accurate but still functional.
    
    Args:
        dm_seq: Dharmamitra lexical sequence
        sp_splits: sanskrit_parser sandhi splits
    
    Returns:
        List of normalized pada entries (same format as align_dm_to_sp)
    """
    if not dm_seq:
        return []

    best_words, best_items = sp_splits[0]

    sp_morph: Dict[str, Dict[str, str]] = {}
    for i, word in enumerate(best_words):
        if i < len(best_items):
            m = extract_morph_from_tags(best_items[i].get("morphological_tags", []))
            if m:
                sp_morph[word] = m

    padas: List[Dict[str, Any]] = []
    di = 0

    for sp_word in best_words:
        entry: Dict[str, Any] = {"surface": sp_word}
        matched: List[Dict[str, str]] = []

        if di < len(dm_seq):
            dm_tok = dm_seq[di]
            dm_dev = dm_tok["devanagari"]

            if dm_dev == sp_word:
                matched.append(dm_tok)
                di += 1
            elif dm_dev in sp_word:
                matched.append(dm_tok)
                di += 1
                while di < len(dm_seq):
                    nxt = dm_seq[di]
                    if nxt["devanagari"] in sp_word:
                        matched.append(nxt)
                        di += 1
                    else:
                        break

        if matched:
            if len(matched) == 1:
                entry["analysis"] = matched[0]["morphology"]
            else:
                entry["analysis"] = matched[0]["morphology"]
                entry["compound"] = {
                    "surface": sp_word,
                    "parts": [t["devanagari"] for t in matched],
                }
        elif sp_word in sp_morph:
            entry["analysis"] = sp_morph[sp_word]

        padas.append(entry)

    return padas


# ---------------------------------------------------------------------------
# Main normalization
# ---------------------------------------------------------------------------
# This is the top-level normalization function that orchestrates the pipeline:
# 1. Extract data from all three engines
# 2. Align Dharmamitra tokens to sanskrit_parser surface forms
# 3. Build normalized output with surface forms, analysis, and compound info

def normalize_raw(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize raw multi-engine output.
    
    Pipeline: surface -> segmentation -> sandhi -> compound -> morphology
    Source priority: Dharmamitra > sanskrit_parser > Vidyut
    
    Args:
        raw: Raw output from app.py with "engine_outputs" containing
             sanskrit_parser, dharmamitra, and vidyut results
    
    Returns:
        Normalized output dict with:
        - input: Original input (devanagari and iast)
        - mode: "pada" or "shloka"
        - padas: List of normalized pada entries
    """
    engines = raw.get("engine_outputs", {})

    # Extract data from each engine in priority order
    dm_seq = extract_dm_sequence(engines.get("dharmamitra", {}))
    sp_splits = extract_sp_splits(engines.get("sanskrit_parser", {}))
    v_compounds = extract_vidyut_compounds(engines.get("vidyut", {}))

    # Primary segmentation from Dharmamitra if available
    if dm_seq and sp_splits:
        padas = align_dm_to_sp(dm_seq, sp_splits)
    elif sp_splits:
        # Fallback to sanskrit_parser surface forms if no Dharmamitra data
        words, items = sp_splits[0]
        padas = []
        for i, w in enumerate(words):
            e: Dict[str, Any] = {"surface": w}
            if i < len(items):
                m = extract_morph_from_tags(
                    items[i].get("morphological_tags", [])
                )
                if m:
                    e["analysis"] = m
            padas.append(e)
    else:
        return {"input": raw.get("input", {}), "mode": raw.get("mode", ""), "padas": []}

    return {
        "input": raw.get("input", {}),
        "mode": raw.get("mode", ""),
        "padas": padas,
    }


def main() -> int:
    """Main entry point for normalization.
    
    Reads output.json, runs normalization, writes output.normalized.json.
    Prints summary statistics (raw size, normalized size, reduction percentage).
    
    Returns:
        0 on success, 1 on error
    """
    script_dir = Path(__file__).resolve().parent
    raw_path = script_dir / "output.json"
    norm_path = script_dir / "output.normalized.json"

    try:
        with open(raw_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        print(f"Error: {raw_path} not found", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    normalized = normalize_raw(raw)

    with open(norm_path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2, ensure_ascii=False)

    raw_size = raw_path.stat().st_size
    norm_size = norm_path.stat().st_size
    reduction = (1 - norm_size / raw_size) * 100

    print(f"Raw output: {raw_size:,} bytes")
    print(f"Normalized output: {norm_size:,} bytes")
    print(f"Reduction: {reduction:.1f}%")
    print(f"Surface padas: {len(normalized.get('padas', []))}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
