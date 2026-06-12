<!-- version: v2.0 | date: 2026-06-04 | breaking: collect-web now config-driven, no hardcoded languages -->
# /collect-web — Autonomous web collection + source discovery

## Usage
```
/collect-web project=<name> [adapter=<adapter_name>] [topic=<keyword>] [max_pages=50]
```

## What this skill does

Reads the project config to understand the domain, task type, and pair strategy,
then launches the appropriate collection strategy:

- `pair_strategy: fuse`      → search for **bilingual sources** (same concept in source + target lang)
  using `agents/source_discoverer.py` + `agents/doc_collector.py`
- `pair_strategy: doc_to_code` → search for **monolingual documentation** (description + code)
  using `agents/doc_collector.py`
- `domain: text, task_type: qa` → search for **Q&A pairs** using `agents/web_collector.py`

Zero hardcoded language names — everything driven by `config/projects/<project>.yaml`
and `config/languages.yaml`.

## Steps for Claude

### Step 1 — Load and analyse project config
```python
import yaml
cfg = yaml.safe_load(open(f"config/projects/{project}.yaml"))
domain        = cfg.get("domain", "code")
task_type     = cfg.get("task_type", "generation")
adapters      = cfg.get("adapters", [])

# If adapter specified, filter to that adapter only
if adapter:
    adapters = [a for a in adapters if a["name"] == adapter]
```

Identify for each adapter:
- `pair_strategy`: fuse | doc_to_code | qa
- `lang_pair`: key into `config/languages.yaml`
- `source_configs`: already declared sources to avoid duplicates

### Step 2 — For adapters with pair_strategy=fuse: discover bilingual sources

```bash
source .venv/bin/activate && python -m agents.source_discoverer \
    --pair <lang_pair> \
    --project <project> \
    --adapter <adapter_name> \
    --max-sources 5 \
    --output /tmp/discovered_<adapter>.yaml
```

Review discovered sources. For each confirmed source (both languages):

```bash
# Collect source-lang side
python -m agents.doc_collector \
    --url <source_lang_url> \
    --lang <source_lang> \
    --adapter-name <adapter_name> \
    --max-pages 30 \
    --output datalake/projects/<project>/raw/doc_crawl_$(date +%Y%m%dT%H%M%S).jsonl

# Collect target-lang side  
python -m agents.doc_collector \
    --url <target_lang_url> \
    --lang <target_lang> \
    --adapter-name <adapter_name> \
    --max-pages 30 \
    --output datalake/projects/<project>/raw/doc_crawl_$(date +%Y%m%dT%H%M%S).jsonl
```

### Step 3 — For adapters with pair_strategy=doc_to_code: collect monolingual docs

Use source_configs already declared in the project, or add newly discovered ones:

```bash
python -m agents.doc_collector \
    --url <url> \
    --lang <lang> \
    --adapter-name <adapter_name> \
    --max-pages <max_pages> \
    --output datalake/projects/<project>/raw/doc_crawl_$(date +%Y%m%dT%H%M%S).jsonl
```

### Step 4 — For domain=text: collect Q&A / prose data

```bash
python -m agents.web_collector \
    --project <project> \
    --topic "<topic>" \
    --domain text \
    --task-type qa \
    --max-pages <max_pages> \
    --adapter-name <adapter_name> \
    --output datalake/projects/<project>/raw/web_$(date +%Y%m%dT%H%M%S).jsonl
```

### Step 5 — Report

Print per-adapter summary:
- N new sources discovered
- N records collected (per language)
- Suggested next step: `/clean-dataset project=<project>` or collect more

## Key principle

The skill reads intent from config, never hardcodes languages or topics.
`concept_keywords` in `config/languages.yaml` drive the search queries.
`config/trusted_domains.yaml` constrains which sites are crawled.

## Expected output
```
Adapter: java-to-python-translator (pair_strategy=fuse)
  source_discoverer: 3 new Java sources, 4 new Python sources
  doc_collector (java): 87 records → raw/doc_crawl_20260604T...jsonl
  doc_collector (python): 112 records → raw/doc_crawl_20260604T...jsonl

Adapter: python-migration-quality-reviewer (pair_strategy=doc_to_code)
  doc_collector (python): 54 records → raw/doc_crawl_20260604T...jsonl

Total: 253 new records collected
Next: /clean-dataset project=migrator
```
