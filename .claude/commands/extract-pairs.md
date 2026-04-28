<!-- version: v1.0 | date: 2026-04-28 | breaking: none -->
# /extract-pairs — Extract training pairs from a source repository

## Usage
```
/extract-pairs project=<name> repo=<path> [min_tokens=50] [max_tokens=2048] [format=mistral|llama|raw]
```

## What this skill does
Extracts input/output pairs from a source code repository and writes them to
`datalake/projects/<project>/processed/pairs_raw.jsonl`.
Uses `pipeline/extract.py::PairExtractor` entirely via Python — no AI call needed.

## Steps for Claude

### Step 1 — Validate
- Check `repo` path exists and is readable
- Load `config/projects/<project>.yaml` to get `src_lang` and `tgt_lang`
- If `format` not provided, infer from the project's configured model

### Step 2 — Run extraction
```bash
source .venv/bin/activate && python -m pipeline.extract \
    --repo <repo> \
    --project <project> \
    --min-tokens <min_tokens> \
    --max-tokens <max_tokens> \
    --format <format>
```

This script:
- Walks the repo for files matching `src_lang` extension
- Finds corresponding `tgt_lang` files (same basename, different extension)
- Validates syntax of both files via tree-sitter
- Writes JSONL to `datalake/projects/<project>/processed/pairs_raw.jsonl`

### Step 3 — Report stats
```bash
wc -l datalake/projects/<project>/processed/pairs_raw.jsonl
python -m pipeline.utils.jsonl stats datalake/projects/<project>/processed/pairs_raw.jsonl
```

Print:
- Total pairs extracted
- Token length distribution (min, max, mean, p95)
- Pairs rejected (syntax error) with count and sample reasons
- File written to

### Step 4 — Suggest next step
If total pairs < 100: warn and suggest augmentation or broader extraction scope.
If total pairs >= 100: suggest `/clean-dataset project=<project>`.

## Expected output
```
Extracted 847 pairs from /path/to/repo
Token stats: min=45 max=1820 mean=312 p95=890
Rejected: 23 (syntax errors: 18, too short: 5)

Output: datalake/projects/<project>/processed/pairs_raw.jsonl

Next: /clean-dataset project=<project>
```
