#!/usr/bin/env python3
"""samskrta-multi-parser-raw — Unified multi-engine Sanskrit analyzer.

Runs three independent engines (sanskrit_parser, Dharmamitra API, vidyut)
on the same Devanagari input and produces structured JSON results under
distinct top-level keys in a single output file.

Usage:
    python app.py pada -i input.txt          # single-word analysis
    python app.py shloka -i input.txt        # full-line analysis
    python app.py shloka -i -                # read from stdin
"""

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# Suppress sanskrit_parser debug logging
import logging
logging.getLogger("sanskrit_parser").setLevel(logging.WARNING)
logging.getLogger("sanskrit_util").setLevel(logging.WARNING)



# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_URL = "https://dharmamitra.org/api/tagging/"
API_HEADERS = {
    "Authorization": "Basic b2xkc3R1ZGVudDpiZWhhcHB5",
    "Content-Type": "application/json",
}
API_MODE = "unsandhied-lemma-morphosyntax"

# Vidyut data directory
DATA_DIR = os.environ.get(
    "VIDYUT_DATA_DIR",
    str(Path(__file__).resolve().parent / "data-0.4.0"),
)


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------

def preprocess_devanagari(text: str) -> str:
    """Clean Devanagari text: remove classical punctuation (।, ॥)."""
    cleaned = text.replace("।", "").replace("॥", "").strip()
    return cleaned


def devanagari_to_iast(devanagari_text: str) -> str:
    """Convert cleaned Devanagari text to IAST using vidyut lipi."""
    from vidyut.lipi import transliterate, Scheme
    return transliterate(devanagari_text, Scheme.Devanagari, Scheme.Iast)


def read_input(filename: str) -> str:
    """Read and strip trailing whitespace from input file."""
    if filename == "-":
        return sys.stdin.read().strip()
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        raise FileNotFoundError(f"Input file not found: {filename}")


# ---------------------------------------------------------------------------
# Devanagari label maps — PyO3 enum values → Devanagari
# ---------------------------------------------------------------------------

VIBHAKTI_DEVANAGARI = {
    "praTamA": "प्रथमा",
    "dvitIyA": "द्वितीया",
    "tftIyA": "तृतीया",
    "caturTI": "चतुर्थी",
    "paYcamI": "पञ्चमी",
    "zazWI": "षष्ठी",
    "saptamI": "सप्तमी",
    "samboDanam": "सम्बोधनम्",
}

LAKARA_DEVANAGARI = {
    "la~w": "लट्",
    "li~w": "लिट्",
    "lu~w": "लुट्",
    "lf~w": "लृट्",
    "le~w": "लेट्",
    "lo~w": "लोट्",
    "la~N": "लङ्",
    "viDili~N": "विधिलिङ्",
    "ASIrli~N": "आशीर्लिङ्",
    "lu~N": "लुङ्",
    "lf~N": "लुङ्",
}

PURUSHA_DEVANAGARI = {
    "praTama": "प्रथमपुरुषः",
    "maDyama": "मध्यमपुरुषः",
    "uttama": "उत्तमपुरुषः",
}

VACANA_DEVANAGARI = {
    "eka": "एकवचनम्",
    "dvi": "द्विवचनम्",
    "bahu": "बहुवचनम्",
}

LINGA_DEVANAGARI = {
    "puM": "पुंलिङ्गम्",
    "strI": "स्त्रीलिङ्गम्",
    "napuMsaka": "नपुंसकलिङ्गम्",
}

GANA_DEVANAGARI = {
    "BvAdi": "भ्वादिः",
    "adAdi": "आदादिः",
    "juhotyAdi": "जुहोत्यादिः",
    "divAdi": "दिवादिः",
    "svAdi": "स्वदिः",
    "tudAdi": "तुदादिः",
    "ruDAdi": "रुधादिः",
    "tanAdi": "तनादिः",
    "kryAdi": "क्रीयादिः",
    "curAdi": "चुरादिः",
    "kaRqvAdi": "कण्ड्वादिः",
}

PRAYOGA_DEVANAGARI = {
    "kartari": "कर्तरी",
    "karmaRi": "कर्मणि",
    "BAve": "भावे",
}


# ---------------------------------------------------------------------------
# sanskrit_parser engine adapter
# ---------------------------------------------------------------------------

def _slp1_to_devanagari(slp1: str) -> str:
    """Transliterate an SLP1 Sanskrit string to Devanagari."""
    from indic_transliteration import sanscript
    try:
        return sanscript.transliterate(slp1, sanscript.SLP1, sanscript.DEVANAGARI)
    except Exception:
        return slp1


def _morphological_tags_to_json(
    tags,
) -> List[Dict[str, Any]]:
    """Convert morphological tags to JSON-serializable list of dicts."""
    if tags is None:
        return []
    result = []
    for root, tag_set in tags:
        result.append(
            {
                "root": _slp1_to_devanagari(str(root)),
                "tags": sorted(_slp1_to_devanagari(str(t)) for t in tag_set),
            }
        )
    return result


def _parse_node_to_json(node) -> Dict[str, Any]:
    """Convert a ParseNode to JSON-serializable dict."""
    return {
        "pada": node.pada,
        "root": _slp1_to_devanagari(str(node.parse_tag.root)),
        "tags": node.parse_tag.tags,
    }


def _parse_edge_to_json(edge) -> Optional[Dict[str, Any]]:
    """Convert a ParseEdge to JSON-serializable dict with predecessor info."""
    pred = edge.predecessor
    node = edge.node
    return {
        "pada": node.pada,
        "root": _slp1_to_devanagari(str(node.parse_tag.root)),
        "tags": node.parse_tag.tags,
        "predecessor": {
            "pada": pred.pada,
            "root": _slp1_to_devanagari(str(pred.parse_tag.root)),
            "tags": pred.parse_tag.tags,
        },
        "sambandha": edge.label,
    }


def _build_vakya_graph(graph: List[Any]) -> List[Dict[str, Any]]:
    """Build vakya parse graph from interleaved ParseNode/ParseEdge list."""
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []

    for item in graph:
        item_type = type(item).__name__
        if item_type == "ParseNode":
            key = item.pada
            nodes[key] = _parse_node_to_json(item)
        elif item_type == "ParseEdge":
            edge_json = _parse_edge_to_json(item)
            if edge_json:
                edges.append(edge_json)

    # Attach predecessor info to successor nodes
    for edge in edges:
        successor_pada = edge["pada"]
        if successor_pada in nodes:
            nodes[successor_pada]["predecessor"] = edge["predecessor"]
            nodes[successor_pada]["sambandha"] = edge["sambandha"]

    # Build ordered graph list
    ordered_graph: List[Dict[str, Any]] = []
    seen: set = set()
    for item in graph:
        if type(item).__name__ == "ParseNode":
            key = item.pada
            if key not in seen:
                ordered_graph.append(nodes[key])
                seen.add(key)

    return ordered_graph


def run_sanskrit_parser(input_text: str, mode: str) -> Dict[str, Any]:
    """Run sanskrit_parser engine on the input text.

    Returns structured results with sandhi splits, morphological tags,
    and (for shloka mode) vakya parses.
    """
    from sanskrit_parser.api import Parser
    from indic_transliteration import sanscript

    parser = Parser(output_encoding=sanscript.DEVANAGARI)

    limit = 10 if mode == "pada" else 5
    splits = parser.split(input_text, limit=limit)

    sandhi_splits = []
    for split_idx, split in enumerate(splits):
        items = split.split
        items_json = []
        for item in items:
            tags = parser.sandhi_analyzer.getMorphologicalTags(item, tmap=True)
            items_json.append(
                {
                    "pada": item.devanagari(),
                    "morphological_tags": _morphological_tags_to_json(tags),
                }
            )

        # Vakya parsing for shloka mode only
        vakya_parses = []
        if mode == "shloka":
            def _parse_timeout_handler(signum, frame):
                raise TimeoutError("Vakya parsing timed out")

            old_handler = signal.signal(signal.SIGALRM, _parse_timeout_handler)
            signal.alarm(5)
            try:
                parses = split.parse(limit=3)
                signal.alarm(0)
                for parse_idx, parse in enumerate(parses):
                    graph = _build_vakya_graph(parse.graph)
                    vakya_parses.append(
                        {
                            "parse_index": parse_idx,
                            "cost": parse.cost,
                            "graph": graph,
                        }
                    )
            except (TimeoutError, Exception):
                signal.alarm(0)
            finally:
                signal.signal(signal.SIGALRM, old_handler)

        split_entry: Dict[str, Any] = {
            "split_index": split_idx,
            "split": [item.devanagari() for item in items],
            "items": items_json,
            "vakya_parses": vakya_parses,
        }
        sandhi_splits.append(split_entry)

    return {
        "mode": mode,
        "input": input_text,
        "sandhi_splits": sandhi_splits,
    }


# ---------------------------------------------------------------------------
# Dharmamitra API engine adapter
# ---------------------------------------------------------------------------

def _parse_tokens(raw_output: str) -> List[Dict[str, Any]]:
    """Parse underscore-separated API output into structured tokens."""
    return [{"form": seg, "tagged": True} for seg in raw_output.split("_") if seg]


def run_dharmamitra(iast_text: str, iast_lines: List[str]) -> Dict[str, Any]:
    """Send IAST text to Dharmamitra API and return structured JSON."""
    import requests

    data = {
        "texts": [iast_text],
        "mode": API_MODE,
        "input_encoding": "auto",
        "human_readable_tags": True,
        "output_format": "dict",
    }

    try:
        response = requests.post(API_URL, headers=API_HEADERS, json=data, timeout=30)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        return {"error": "Dharmamitra API request timed out"}
    except requests.exceptions.RequestException as exc:
        return {"error": f"Dharmamitra API unavailable: {exc}"}

    result = response.json()
    raw_output = result.get("results", [""])[0]

    return {
        "api_endpoint": API_URL,
        "mode": API_MODE,
        "input_lines": iast_lines,
        "raw_output": raw_output,
        "tokens": _parse_tokens(raw_output),
    }


# ---------------------------------------------------------------------------
# vidyut engine adapter
# ---------------------------------------------------------------------------

def _slp1_to_devanagari_vidyut(slp1_text: str) -> str:
    """Convert SLP1 text to Devanagari via vidyut lipi."""
    from vidyut.lipi import transliterate, Scheme
    if not slp1_text:
        return ""
    return transliterate(slp1_text, Scheme.Slp1, Scheme.Devanagari)


def kosha_lookup(kosha, word: str) -> list:
    """Look up a word in the kosha dictionary, with stem normalization fallback.

    First tries exact match, then progressively shorter stems (stripping 1-3
    SLP1 characters) to handle surface forms like nominative ``vAco`` → stem
    ``vAca``.
    """
    try:
        entries = kosha.get(word)
        if entries:
            return entries
    except KeyError:
        pass

    # Fallback: try stripping SLP1 case endings to find stem form.
    for length in range(1, 4):
        stem = word[:-length] if len(word) > length else word
        if not stem:
            continue
        try:
            entries = kosha.get(stem)
            if entries:
                return entries
        except KeyError:
            pass

    return []
def _is_debug_text(text: str) -> bool:
    """Check if a prakriya step text is an English debug/logging string."""
    if not text:
        return False
    if '==' in text or '::' in text:
        return True
    if 'trying' in text or 'run_' in text:
        return True
    if '(' in text and text.split('(')[-1].strip().islower():
        return True
    return False


def _format_pada_entry_json(entry) -> Dict[str, Any]:
    """Format a PadaEntry as a JSON-serializable dict."""
    from vidyut.lipi import transliterate, Scheme
    result = {}

    if entry.is_avyaya:
        result["type"] = "अव्ययम्"
    else:
        entry_repr = repr(entry)
        if "Tinanta" in entry_repr:
            result["type"] = "तिन्तन्तः"
        else:
            result["type"] = "सुन्तन्तः"

    if hasattr(entry, 'pratipadika_entry') and hasattr(entry, 'linga'):
        pe = entry.pratipadika_entry
        if pe:
            result["pratipadika"] = _slp1_to_devanagari_vidyut(pe.lemma)
            if not pe.is_avyaya and hasattr(pe, 'artha_sa') and pe.artha_sa:
                result["artha"] = _slp1_to_devanagari_vidyut(pe.artha_sa)

        if entry.linga:
            result["linga"] = LINGA_DEVANAGARI.get(str(entry.linga), str(entry.linga))
        if entry.vibhakti:
            result["vibhakti"] = VIBHAKTI_DEVANAGARI.get(str(entry.vibhakti), str(entry.vibhakti))
        if entry.vacana:
            result["vacana"] = VACANA_DEVANAGARI.get(str(entry.vacana), str(entry.vacana))

    if hasattr(entry, 'dhatu_entry') and hasattr(entry, 'prayoga'):
        de = entry.dhatu_entry
        if de:
            result["dhatu"] = _slp1_to_devanagari_vidyut(de.clean_text)
            if de.dhatu and de.dhatu.gana:
                result["gana"] = GANA_DEVANAGARI.get(str(de.dhatu.gana), str(de.dhatu.gana))
            if de.artha_sa:
                result["artha"] = _slp1_to_devanagari_vidyut(de.artha_sa)

            result["prayoga"] = PRAYOGA_DEVANAGARI.get(str(entry.prayoga), str(entry.prayoga))
            result["lakara"] = LAKARA_DEVANAGARI.get(str(entry.lakara), str(entry.lakara))
            result["purusha"] = PURUSHA_DEVANAGARI.get(str(entry.purusha), str(entry.purusha))
            result["vacana"] = VACANA_DEVANAGARI.get(str(entry.vacana), str(entry.vacana))

    return result


def _format_prakriya_steps(prakriya) -> List[Dict[str, Any]]:
    """Format prakriya derivation steps as a list of JSON-serializable dicts."""
    steps = []
    history = prakriya.history
    if not history:
        return steps

    for step_idx, step in enumerate(history, 1):
        sutra = step.code if step.code else ""
        source_repr = repr(step.source) if hasattr(step, 'source') else ""
        source = source_repr.replace("Source.", "") if source_repr else ""

        if hasattr(step, 'result') and step.result:
            terms = step.result
            if terms and isinstance(terms[0], str):
                filtered = [t for t in terms if not _is_debug_text(t)]
            else:
                filtered = [t for t in terms if not _is_debug_text(t.text)]
                filtered = [t for t in filtered if t.text]

            if not filtered:
                continue

            if isinstance(terms[0], str):
                term_texts = ' '.join(filtered)
                changed_dev = [_slp1_to_devanagari_vidyut(t) for t in filtered]
            else:
                term_texts = ' '.join(t.text for t in filtered)
                changed_dev = [_slp1_to_devanagari_vidyut(t.text) for t in filtered if t.was_changed]

            if term_texts:
                steps.append({
                    "step": step_idx,
                    "sutra": sutra,
                    "source": source,
                    "terms_dev": _slp1_to_devanagari_vidyut(term_texts),
                    "changed_dev": changed_dev,
                })
            else:
                steps.append({
                    "step": step_idx,
                    "sutra": sutra,
                    "source": source,
                    "terms_dev": "",
                    "changed_dev": [],
                })
        else:
            steps.append({
                "step": step_idx,
                "sutra": sutra,
                "source": source,
                "terms_dev": "",
                "changed_dev": [],
            })

    return steps


def _split_into_padas(slp1_line: str) -> List[str]:
    """Split a SLP1 line into 8-syllable quarter-verses (padas)."""
    from vidyut.lipi import transliterate, Scheme

    tokens = slp1_line.split()
    if not tokens:
        return [slp1_line]

    meaningful_tokens = [t for t in tokens if t not in ('.', '..', '.')]
    if len(meaningful_tokens) < 3:
        return [slp1_line]

    total_syllables = sum(len(t) for t in meaningful_tokens)
    if total_syllables <= 12:
        return [slp1_line]

    half_syllables = total_syllables // 2
    cumulative = 0
    split_idx = len(meaningful_tokens)

    for i, t in enumerate(meaningful_tokens):
        cumulative += len(t)
        if cumulative >= half_syllables:
            split_idx = i + 1
            break

    first_tokens = []
    meaningful_count = 0
    for t in tokens:
        if t not in ('.', '..', '.'):
            if meaningful_count < split_idx:
                first_tokens.append(t)
                meaningful_count += 1
            else:
                break
        else:
            first_tokens.append(t)

    first_half = ' '.join(first_tokens)
    second_half = ' '.join(tokens[len(first_tokens):])

    results = []
    if first_half:
        results.append(first_half)
    if second_half:
        results.append(second_half)

    return results


def _is_terminal(kosha, word: str) -> bool:
    """Check if a word is a known dictionary entry (terminal node)."""
    return len(kosha_lookup(kosha, word)) > 0


def _get_kosha_info(kosha, word: str) -> Optional[Dict[str, Any]]:
    """Get kosha info for a word: type and lemma.

    Returns dict with 'type' (सुन्तन्तः/तिन्तन्तः/अव्ययम्) and
    'lemma' (Devanagari), or None if not found.
    """
    entries = kosha_lookup(kosha, word)
    if not entries:
        return None
    entry = entries[0]
    entry_repr = repr(entry)
    if "Tinanta" in entry_repr:
        word_type = "तिन्तन्तः"
    elif hasattr(entry, 'is_avyaya') and entry.is_avyaya:
        word_type = "अव्ययम्"
    else:
        word_type = "सुन्तन्तः"
    lemma = getattr(entry, 'lemma', '') or (getattr(entry, 'pratipadika_entry', None) and getattr(entry.pratipadika_entry, 'lemma', ''))
    return {"type": word_type, "lemma": _slp1_to_devanagari_vidyut(lemma)}


def _is_quality_split(splitter, kosha, word: str) -> List[Any]:
    """Check if a binary split is semantically valid.

    Filters out spurious matches where very short strings match verb roots.
    Criteria:
      - Both parts must be ≥ 3 characters
      - Both parts must have kosha entries
      - Both parts must have at least 2 kosha entries (filters spurious matches)
      - Both parts must have at least one non-derived (standalone) entry
    """
    def _has_standalone_entry(entries):
        """Check if entries include at least one standalone word (not all derived)."""
        for e in entries:
            entry_repr = repr(e)
            # Filter out entries that are purely derived forms (Krdanta, Tinanta, etc.)
            if "Krdanta" not in entry_repr and "Tinanta" not in entry_repr:
                return True
        return False

    results = []
    for i in range(1, len(word)):
        try:
            splits = list(splitter.split_at(word, i))
        except Exception:
            continue
        for split in splits:
            if not split.is_valid:
                continue
            first_info = _get_kosha_info(kosha, split.first)
            second_info = _get_kosha_info(kosha, split.second)
            if not first_info or not second_info:
                continue
            if len(split.first) < 4 or len(split.second) < 4:
                continue
            first_count = len(kosha_lookup(kosha, split.first))
            second_count = len(kosha_lookup(kosha, split.second))
            if first_count < 2 or second_count < 2:
                continue
            # Require both parts to have at least one standalone entry
            if not _has_standalone_entry(kosha_lookup(kosha, split.first)):
                continue
            if not _has_standalone_entry(kosha_lookup(kosha, split.second)):
                continue
            results.append((split, first_info, second_info))
    return results


def recursive_split(splitter, kosha, word: str, max_depth: int = 4, max_parts: int = 4) -> List[List[Dict[str, Any]]]:
    """Recursively resolve multi-part compounds using DFS.

    Records intact dictionary matches as leaf chains (macro-compound level)
    and continues exploring deeper splits (micro-phonetic level).

    Recurses into both first and second halves to produce chains like:
      - [वाक्, अर्थ, इव] (deepest)
      - [वागर्थ, इव] (medium)
      - [वागर्था, विव] (shallowest)

    Args:
        splitter: Splitter instance for sandhi rules
        kosha: Kosha instance for dictionary lookup
        word: SLP1-encoded word to split
        max_depth: Maximum recursion depth
        max_parts: Maximum number of parts in a chain (prevents over-splitting)

    Returns list of split chains, each chain is a list of dicts:
    [{"part": slp1_text, "dev": devanagari_text, "kosha": {...}|None}, ...]
    """
    results = []

    # Record intact word as leaf chain (macro-compound level)
    if _is_terminal(kosha, word):
        kosha_info = _get_kosha_info(kosha, word)
        results.append([{
            "part": word,
            "dev": _slp1_to_devanagari_vidyut(word),
            "kosha": kosha_info,
            "depth": 0,
        }])

    if max_depth <= 0:
        return results

    # Explore deeper splits (micro-phonetic level)
    quality_splits = _is_quality_split(splitter, kosha, word)

    for split, first_info, second_info in quality_splits:
        # Recursively resolve the first half
        first_chains = recursive_split(splitter, kosha, split.first, max_depth - 1, max_parts)
        # Recursively resolve the second half
        second_chains = recursive_split(splitter, kosha, split.second, max_depth - 1, max_parts)

        # If first half has deeper splits, combine each first chain with each second chain
        if first_chains and second_chains:
            for fc in first_chains:
                for sc in second_chains:
                    full_chain = fc + sc
                    if len(full_chain) <= max_parts:
                        results.append(full_chain)
        elif first_chains:
            # Only first half has deeper splits
            for fc in first_chains:
                full_chain = fc + [{"part": split.second, "dev": _slp1_to_devanagari_vidyut(split.second), "kosha": second_info, "depth": 1}]
                if len(full_chain) <= max_parts:
                    results.append(full_chain)
        elif second_chains:
            # Only second half has deeper splits (original behavior)
            for sc in second_chains:
                full_chain = [{"part": split.first, "dev": _slp1_to_devanagari_vidyut(split.first), "kosha": first_info, "depth": 1}] + sc
                if len(full_chain) <= max_parts:
                    results.append(full_chain)
        else:
            # Neither half has deeper splits — record this split as a leaf
            results.append([
                {"part": split.first, "dev": _slp1_to_devanagari_vidyut(split.first), "kosha": first_info, "depth": 1},
                {"part": split.second, "dev": _slp1_to_devanagari_vidyut(split.second), "kosha": second_info, "depth": 1},
            ])

    # Sort results: prefer chains with more kosha matches, fewer parts
    def chain_score(chain):
        kosha_count = sum(1 for p in chain if p.get("kosha"))
        part_count = len(chain)
        return (kosha_count, -part_count)

    results.sort(key=chain_score, reverse=True)

    return results
def run_vidyut(devanagari_text: str, iast_text: str, iast_lines: List[str], mode: str) -> Dict[str, Any]:
    """Run vidyut engine: cheda segmentation, kosha lookup, sandhi splitting, prakriya, meter.

    Uses input text directly — cheda for word segmentation, sandhi (Splitter)
    for recursive compound splitting, kosha for dictionary lookup.
    """
    from pathlib import Path
    from vidyut.lipi import transliterate, Scheme
    from vidyut.kosha import Kosha
    from vidyut.prakriya import (
        Vyakarana, Dhatu, Pratipadika, Pada,
        Gana, Lakara, Purusha, Vacana, Linga, Vibhakti, Prayoga
    )
    from vidyut.chandas import Chandas
    from vidyut.sandhi import Splitter

    # Initialize vidyut modules
    try:
        if not Path(DATA_DIR).exists():
            return {"error": "Vidyut data directory not found"}
        kosha = Kosha(Path(DATA_DIR) / "kosha")
        vyakarana = Vyakarana()
        chandas = Chandas(Path(DATA_DIR) / "chandas" / "meters.tsv")
        splitter = Splitter.from_csv(Path(DATA_DIR) / "sandhi" / "rules.csv")
    except Exception as exc:
        return {"error": f"Vidyut initialization failed: {exc}"}

    # Convert Devanagari input to SLP1 for processing
    slp1_text = transliterate(devanagari_text, Scheme.Devanagari, Scheme.Slp1)

    # Tokenize by whitespace, stripping line-ending punctuation
    tokens = []
    for line in slp1_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        raw_tokens = line.split()
        for raw in raw_tokens:
            punct = ""
            stripped = raw
            while stripped and stripped[-1] in "।॥.":
                punct = stripped[-1] + punct
                stripped = stripped[:-1]
            if stripped:
                tokens.append((stripped, punct))

    all_dhatus = set()
    all_pratipadikas = set()
    words = []

    for slp1_token, punct in tokens:
        devanagari = _slp1_to_devanagari_vidyut(slp1_token)

        entries = kosha_lookup(kosha, slp1_token)

        word_entry = {
            "devanagari": devanagari,
            "punctuation": punct,
            "is_compound": False,
        }

        if entries:
            # Single word — full grammatical breakdown
            is_verb = any("Tinanta" in repr(e) for e in entries)
            if is_verb:
                entries = [e for e in entries if "Tinanta" in repr(e)]

            # Format entries and deduplicate
            seen_entries = set()
            unique_entries = []
            for e in entries[:8]:
                entry_dict = _format_pada_entry_json(e)
                key = (
                    entry_dict.get("pratipadika", ""),
                    entry_dict.get("linga", ""),
                    entry_dict.get("vibhakti", ""),
                    entry_dict.get("vacana", ""),
                )
                if key not in seen_entries:
                    seen_entries.add(key)
                    unique_entries.append(entry_dict)

            word_entry["grammatical_entries"] = unique_entries
            word_entry["is_verb"] = is_verb

            # Extract dhatu/pratipadika
            for entry in entries:
                entry_repr = repr(entry)
                if "Tinanta" in entry_repr:
                    de = entry.dhatu_entry
                    if de and de.dhatu and de.dhatu.aupadeshika:
                        d = de.dhatu
                if hasattr(entry, 'pratipadika_entry'):
                    pe = entry.pratipadika_entry
                    if pe and pe.lemma:
                        all_pratipadikas.add(pe.lemma)
        else:
            chains = recursive_split(splitter, kosha, slp1_token, max_depth=2, max_parts=4)

            if chains:
                word_entry["is_compound"] = True

                # Score chains: prefer more kosha entries, fewer parts
                def chain_score(chain):
                    kosha_count = sum(1 for p in chain if p.get("kosha"))
                    part_count = len(chain)
                    return (kosha_count, -part_count)

                chains.sort(key=chain_score, reverse=True)

                # Build flat sandhi_splits list
                word_entry["sandhi_splits"] = []
                seen = set()
                for chain in chains:
                    parts_dev = [part["dev"] for part in chain]
                    split_str = " + ".join(parts_dev)
                    if split_str not in seen:
                        seen.add(split_str)
                        word_entry["sandhi_splits"].append(split_str)
                    if len(word_entry["sandhi_splits"]) >= 5:
                        break
            else:
                word_entry["unknown"] = True

        words.append(word_entry)

    # Dhatu prakriyas
    gana_map = {
        "BvAdi": Gana.Bhvadi,
        "adAdi": Gana.Adadi,
        "juhotyAdi": Gana.Juhotyadi,
        "divAdi": Gana.Divadi,
        "svAdi": Gana.Svadi,
        "tudAdi": Gana.Tudadi,
        "ruDAdi": Gana.Rudhadi,
        "tanAdi": Gana.Tanadi,
        "kryAdi": Gana.Kryadi,
        "curAdi": Gana.Curadi,
        "kaRqvAdi": Gana.Kryadi,
    }

    dhatus_prakriya = []
    for aupadeshika, gana_name in sorted(all_dhatus):
        gana = gana_map.get(gana_name, Gana.Bhvadi)
        dhatu = Dhatu.mula(aupadeshika, gana)
        dhatu_dev = _slp1_to_devanagari_vidyut(aupadeshika)

        dhatu_entry = {
            "dhatu": dhatu_dev,
            "krdantas": [],
            "tinantas": [],
        }

        try:
            prakriyas = vyakarana.derive(dhatu)
            if prakriyas:
                dhatu_entry["krdantas"] = _format_prakriya_steps(prakriyas[0])
        except Exception:
            pass

        combos = [
            (Lakara.Lat, Purusha.Madhyama, Vacana.Eka),
            (Lakara.Lan, Purusha.Madhyama, Vacana.Eka),
            (Lakara.Lot, Purusha.Madhyama, Vacana.Eka),
        ]
        for lakara, purusha, vacana in combos:
            try:
                pada = Pada.Tinanta(
                    dhatu=dhatu,
                    prayoga=Prayoga.Kartari,
                    lakara=lakara,
                    purusha=purusha,
                    vacana=vacana,
                )
                tinantas = vyakarana.derive(pada)
                if tinantas and tinantas[0].history and tinantas[0].history[-1].result:
                    result = tinantas[0].history[-1].result
                    if result and isinstance(result[0], str):
                        final = ' '.join(result)
                    else:
                        final = ' '.join(t.text for t in result if t.text)
                    dev = _slp1_to_devanagari_vidyut(final)
                    lakara_dev = LAKARA_DEVANAGARI.get(str(lakara), str(lakara))
                    purusha_dev = PURUSHA_DEVANAGARI.get(str(purusha), str(purusha))
                    vacana_dev = VACANA_DEVANAGARI.get(str(vacana), str(vacana))
                    dhatu_entry["tinantas"].append({
                        "label": f"{lakara_dev}/{purusha_dev}/{vacana_dev}",
                        "form": dev,
                    })
            except Exception:
                pass

        dhatus_prakriya.append(dhatu_entry)

    # Pratipadika prakriyas
    pratipadikas_prakriya = []
    for lemma in sorted(all_pratipadikas)[:5]:
        try:
            pratipadika = Pratipadika.basic(lemma)
            prakriyas = vyakarana.derive(pratipadika)
            if prakriyas:
                pratipadikas_prakriya.append({
                    "lemma": _slp1_to_devanagari_vidyut(lemma),
                    "steps": _format_prakriya_steps(prakriyas[0]),
                })
        except Exception:
            pass

    # Meter classification
    meter_results = []
    slp1_lines = slp1_text.strip().split("\n")
    known_vrtta = None

    for slp1_line in slp1_lines:
        slp1_line = slp1_line.strip()
        if not slp1_line:
            continue
        dev_line = _slp1_to_devanagari_vidyut(slp1_line)
        padas = _split_into_padas(slp1_line)
        pada_results = []

        for pada_slp1 in padas:
            match = chandas.classify(pada_slp1)
            pada_result = {
                "devanagari": _slp1_to_devanagari_vidyut(pada_slp1),
                "meter": None,
                "akshara_count": 0,
                "weight_pattern": "",
            }

            if match.padya:
                vrtta_dev = _slp1_to_devanagari_vidyut(match.padya)
                pada_result["meter"] = vrtta_dev
                known_vrtta = vrtta_dev

            if match.aksharas:
                for pada_aksharas in match.aksharas:
                    weight_pattern = ''.join(a.weight for a in pada_aksharas)
                    akshara_count = len(pada_aksharas)
                    pada_result["akshara_count"] = akshara_count
                    pada_result["weight_pattern"] = weight_pattern

            pada_results.append(pada_result)

        meter_results.append({
            "line_devanagari": dev_line,
            "padas": pada_results,
        })

    return {
        "kosha": words,
        "prakriya": {
            "dhatus": dhatus_prakriya,
            "pratipadikas": pratipadikas_prakriya,
        },
        "meter": meter_results,
        "cheda": [],
        "sandhi": [],
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="samskrta-multi-parser-raw — Unified multi-engine Sanskrit analyzer"
    )
    parser.add_argument(
        "mode",
        choices=["pada", "shloka"],
        help="Analysis mode: 'pada' for single-word, 'shloka' for full-line analysis",
    )
    parser.add_argument(
        "-i", "--input",
        default=None,
        help="Path to input file (use '-' for stdin); falls back to input.txt",
    )
    parser.add_argument(
        "-o", "--output",
        default="output.json",
        help="Path to output file (default: output.json)",
    )
    parser.add_argument(
        "-f", "--format",
        choices=["json", "pretty"],
        default="pretty",
        help="Output format: 'json' (compact) or 'pretty' (indented, default)",
    )

    args = parser.parse_args()

    # Determine input file
    input_file = args.input if args.input else "input.txt"
    if not args.input and not Path(input_file).exists():
        # Try shloka/pada specific files
        if args.mode == "shloka" and Path("shloka_input.txt").exists():
            input_file = "shloka_input.txt"
        elif args.mode == "pada" and Path("pada_input.txt").exists():
            input_file = "pada_input.txt"

    # Read input
    try:
        devanagari_text = read_input(input_file)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error reading input: {e}", file=sys.stderr)
        return 1

    # Preprocess
    cleaned = preprocess_devanagari(devanagari_text)
    lines = [preprocess_devanagari(line) for line in cleaned.split("\n") if line.strip()]
    iast_text = devanagari_to_iast(cleaned)
    iast_lines = [devanagari_to_iast(line) for line in lines]

    # Build output structure
    output = {
        "input": {
            "devanagari": devanagari_text,
            "iast": iast_text,
        },
        "mode": args.mode,
        "engine_outputs": {},
    }

    # Run sanskrit_parser engine
    try:
        output["engine_outputs"]["sanskrit_parser"] = run_sanskrit_parser(cleaned, args.mode)
    except Exception as exc:
        output["engine_outputs"]["sanskrit_parser"] = {"error": f"sanskrit_parser unavailable: {exc}"}

    # Run Dharmamitra engine
    dharmamitra_results = {}
    try:
        dharmamitra_results = run_dharmamitra(iast_text, iast_lines)
        output["engine_outputs"]["dharmamitra"] = dharmamitra_results
    except Exception as exc:
        output["engine_outputs"]["dharmamitra"] = {"error": f"Dharmamitra engine failed: {exc}"}

    # Run vidyut engine (uses input text directly)
    try:
        output["engine_outputs"]["vidyut"] = run_vidyut(cleaned, iast_text, iast_lines, args.mode)
    except Exception as exc:
        output["engine_outputs"]["vidyut"] = {"error": f"vidyut engine failed: {exc}"}

    # Write output
    indent = 2 if args.format == "pretty" else None
    json_str = json.dumps(output, indent=indent, ensure_ascii=False)

    try:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(json_str)
    except OSError as exc:
        print(f"Error writing output: {exc}", file=sys.stderr)
        return 1

    # Print to stdout (IAST only to avoid Devanagari rendering issues)
    print(json_str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
