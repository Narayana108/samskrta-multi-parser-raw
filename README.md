# samskrta-multi-parser-raw

Unified multi-engine Sanskrit analyzer. Runs three independent engines on the same Devanagari input and produces structured JSON results under distinct top-level keys in a single output file.

## Overview

This tool analyzes Sanskrit text (single words or full shloka lines) through three parallel engines, each producing raw structured output without merging or filtering. The engines are:

| Engine | Source | Capabilities |
|--------|--------|-------------|
| `sanskrit_parser` | Local Python package | Sandhi splitting, morphological tags, vakya (sentence) parsing |
| `dharmamitra` | Remote API (dharmamitra.org) | Sandhi splitting with lemma morphosyntax tags |
| `vidyut` | Local Python package | Kosha dictionary lookup, dhatu/pratipadika prakriya (derivation), meter classification, recursive sandhi splitting |

Each engine runs independently. If one fails, its key contains `{"error": "..."}` and execution continues for the remaining engines.

## Prerequisites

- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** for dependency management (or `pip`)
- **Vidyut data directory** (`data-0.4.0/`) containing kosha, prakriya, chandas, sandhi, and cheda subdirectories

### Install dependencies

```bash
cd samskrta-multi-parser-raw
uv sync
```

Or with pip:

```bash
pip install sanskrit-parser indic-transliteration vidyut>=0.4.0 requests
```

## Quick Start

```bash
# Pada mode (single-word analysis)
uv run python app.py pada

# Shloka mode (full-line analysis)
uv run python app.py shloka

# Custom input and output
uv run python app.py shloka -i my_shloka.txt -o result.json
```

## CLI Arguments

```
usage: app.py [-h] [-i INPUT] [-o OUTPUT] [-f {json,pretty}] {pada,shloka}

positional arguments:
  {pada,shloka}        Analysis mode: 'pada' for single-word, 'shloka' for full-line analysis

options:
  -h, --help           Show this help message
  -i, --input INPUT    Path to input file (use '-' for stdin); falls back to input.txt
  -o, --output OUTPUT  Path to output file (default: output.json)
  -f, --format FORMAT  Output format: 'json' (compact) or 'pretty' (indented, default)
```

## Input Files

| File | Purpose |
|------|---------|
| `input.txt` | Default input (used when `-i` not specified and no mode-specific file exists) |
| `shloka_input.txt` | Default for shloka mode |
| `pada_input.txt` | Default for pada mode |

Input files should contain Devanagari Sanskrit text. Classical punctuation (`।`, `॥`) is automatically stripped during preprocessing.

## Output Schema

The output is a JSON object with the following structure:

```json
{
  "input": {
    "devanagari": "वागर्थाविव संपृक्तौ...",
    "iast": "vāgarthāviva saṃpṛktau..."
  },
  "mode": "pada" | "shloka",
  "engine_outputs": {
    "sanskrit_parser": { ... },
    "dharmamitra": { ... },
    "vidyut": { ... }
  }
}
```

### sanskrit_parser output

```json
{
  "mode": "pada" | "shloka",
  "input": "Devanagari text",
  "sandhi_splits": [
    {
      "split_index": 0,
      "split": ["वाक्", "अर्थ", "अव", "संपृक्तौ"],
      "items": [
        {
          "pada": "वाक्",
          "morphological_tags": [
            {"root": "वच्", "tags": ["द्विवचनम्", "प्रथमा", "स्त्रीलिङ्गम्"]}
          ]
        }
      ],
      "vakya_parses": [
        {
          "parse_index": 0,
          "cost": 12.5,
          "graph": [
            {"pada": "वाक्", "root": "वच्", "tags": [...], "predecessor": {...}, "sambandha": "..."}
          ]
        }
      ]
    }
  ]
}
```

### dharmamitra output

```json
{
  "api_endpoint": "https://dharmamitra.org/api/tagging/",
  "mode": "unsandhied-lemma-morphosyntax",
  "input_lines": ["vāgarthāviva saṃpṛktau..."],
  "raw_output": "vāk|N vāc|N artha|N ...",
  "tokens": [
    {"form": "vāk|N", "tagged": true},
    {"form": "vāc|N", "tagged": true}
  ]
}
```

### vidyut output

```json
{
  "kosha": [
    {
      "devanagari": "वाक्",
      "punctuation": "",
      "is_compound": false,
      "grammatical_entries": [
        {
          "type": "सुन्तन्तः",
          "pratipadika": "वाच्",
          "artha": "वाचनम्",
          "linga": "स्त्रीलिङ्गम्",
          "vibhakti": "प्रथमा",
          "vacana": "एकवचनम्"
        }
      ],
      "is_verb": false
    },
    {
      "devanagari": "संपृक्तौ",
      "is_compound": true,
      "sandhi_splits": [
        "सम् + पृक्तौ",
        "सम् + प्र + क्तौ"
      ]
    }
  ],
  "prakriya": {
    "dhatus": [
      {
        "dhatu": "वच्",
        "krdantas": [
          {"step": 1, "sutra": "3.1.1", "source": "krt", "terms_dev": "क्त", "changed_dev": ["क्त"]}
        ],
        "tinantas": [
          {"label": "लट्/मध्यम/एक", "form": "वक्ति"},
          {"label": "लङ्/मध्यम/एक", "form": "वक्त्"},
          {"label": "लोट्/मध्यम/एक", "form": "वक्षति"}
        ]
      }
    ],
    "pratipadikas": [
      {
        "lemma": "वाच्",
        "steps": [
          {"step": 1, "sutra": "1.1.1", "source": "pratyaya", "terms_dev": "सुप्", "changed_dev": ["सुप्"]}
        ]
      }
    ]
  },
  "meter": [
    {
      "line_devanagari": "वागर्थाविव संपृक्तौ वागर्थप्रतिपत्तये",
      "padas": [
        {
          "devanagari": "वागर्थाविव संपृक्तौ",
          "meter": "मन्दक्रान्ता",
          "akshara_count": 8,
          "weight_pattern": "LLLLLLLL"
        },
        {
          "devanagari": "वागर्थप्रतिपत्तये",
          "meter": "शार्दूलविक्रीडित",
          "akshara_count": 8,
          "weight_pattern": "GLGLGLGL"
        }
      ]
    }
  ]
}
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `VIDYUT_DATA_DIR` | `./data-0.4.0` | Path to vidyut data directory containing kosha, prakriya, chandas, sandhi, and cheda subdirectories |

## Error Isolation

Each engine runs independently. If one fails, its key contains `{"error": "..."}` and execution continues for the remaining engines. Common failure modes:

- **sanskrit_parser unavailable**: Package not installed or import error
- **Dharmamitra API unavailable**: Network timeout or API error (returns `{"error": "Dharmamitra API unavailable: ..."}`)
- **Vidyut data directory not found**: `VIDYUT_DATA_DIR` points to non-existent directory (returns `{"error": "Vidyut data directory not found"}`)

## Example Output

```bash
$ uv run python app.py shloka -o /tmp/output.json
{"input": {"devanagari": "वागर्थाविव संपृक्तौ...", "iast": "vāgarthāviva saṃpṛktau..."}, "mode": "shloka", "engine_outputs": {...}}

$ cat /tmp/output.json | python3 -m json.tool
```

## Architecture

```
app.py (CLI entry point)
├── preprocess_devanagari()     # Strip classical punctuation
├── devanagari_to_iast()        # Convert Devanagari → IAST
├── read_input()                # Read from file or stdin
├── run_sanskrit_parser()       # Local: sandhi + morphology + vakya
├── run_dharmamitra()           # Remote: API-based lemma tags
├── run_vidyut()                # Local: kosha + prakriya + meter + sandhi
├── normalize.py                # Normalization & deduplication layer
└── main()                      # Orchestrates all engines, writes JSON
```

The vidyut engine implements recursive compound sandhi splitting using DFS traversal through the kosha dictionary and sandhi rules, with quality filtering to prevent spurious splits.

## Two-Output Architecture

The system produces two outputs:

### `output.json` (raw)
Complete raw output from all three engines. Used for:
- Debugging
- Investigating parser failures
- Developing new heuristics

Typically ~100KB for a śloka.

### `output.normalized.json` (normalized)
Generated by running `uv run python normalize.py` on `output.json`.

Contains:
- Surface forms from sandhi splits
- Canonical morphology (lemma, vibhakti, vacana, linga)
- Deduplicated analyses
- Source priority: Dharmamitra > sanskrit_parser > Vidyut

Typically ~3KB for a śloka (97% reduction).

## Normalization Pipeline

```
raw engine outputs
    ↓
surface form extraction (sanskrit_parser sandhi splits)
    ↓
Dharmamitra lemma matching (IAST → Devanagari transliteration)
    ↓
morphology extraction (sanskrit_parser morphological tags)
    ↓
canonical tag normalization
    ↓
deduplication by surface form
    ↓
normalized output
```

This turns a ~100KB raw output into a ~3KB normalized output while preserving all linguistic information.

## Example Normalized Output

```json
{
  "input": {"devanagari": "वागर्थाविव...", "iast": "vāgarthāviva..."},
  "mode": "shloka",
  "padas": [
    {
      "surface": "वागर्थौ",
      "analysis": {
        "lemma": "वागर्थ",
        "vibhakti": "द्वितीया",
        "vacana": "द्विवचनम्",
        "linga": "पुंल्लिङ्गम्"
      }
    },
    {
      "surface": "इव",
      "analysis": {"lemma": "इव"}
    }
  ]
}
```

Each surface form has its canonical morphology, with no parser internals or duplicate candidates.
