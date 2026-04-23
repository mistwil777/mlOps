# /collect-web — Tech watch + collect new training pairs from the web

## Usage
```
/collect-web project=<name> [topic=<search_query>] [max_pages=20] [since=<YYYY-MM-DD>]
```

## What this skill does
Runs the web collector agent to:
1. Find new Python→Java (or other language pair) code examples on the web
2. Score and filter them
3. Add accepted pairs to `datalake/projects/<project>/raw/web_<date>.jsonl`
4. Generate a tech watch summary in `dashboards/watch_<project>_<date>.html`

Zero cloud LLM calls — uses local Ollama for scoring only.

## Sources targeted (configurable per project)
- GitHub search (public repos, MIT/Apache license only)
- Stack Overflow (Q&A with accepted answers)
- Rosetta Code (polyglot examples)
- Project-specific sources from `config/projects/<project>.yaml`

## Steps for Claude

### Step 1 — Load project config
Read `config/projects/<project>.yaml` to get `src_lang`, `tgt_lang`, and any
`web_sources` list defined there.

### Step 2 — Check Crawl4AI is available
```bash
source .venv/bin/activate && python -c "import crawl4ai; print('ok')"
```

### Step 3 — Run web collector agent
```bash
source .venv/bin/activate && python -m agents.web_collector \
    --project <project> \
    --topic "<topic>" \
    --max-pages <max_pages> \
    --since <since> \
    --output datalake/projects/<project>/raw/web_$(date +%Y%m%d).jsonl
```

The agent:
- Crawls targeted sources
- Extracts code blocks (src_lang → tgt_lang pairs)
- Validates syntax via tree-sitter
- Scores pairs with local Ollama
- Respects robots.txt and rate limits (1 req/sec)

### Step 4 — Generate watch report
```bash
python -m agents.report_generator watch \
    --project <project> \
    --output dashboards/watch_<project>_$(date +%Y%m%d).html
```

### Step 5 — Report
- N pages crawled, N pairs extracted, N accepted
- Top 3 new pair examples (truncated)
- Link to HTML dashboard

## Scheduling
For autonomous monthly collection, use Prefect:
```bash
python -m agents.web_collector schedule --project <project> --cron "0 2 1 * *"
```
This runs collection on the 1st of each month at 2am, logs results, and
prepares data for the next retraining cycle.

## Expected output
```
Crawled: 18 pages (2 skipped: robots.txt)
Extracted: 134 raw pairs
After syntax validation: 112 pairs
After quality scoring: 89 pairs accepted

Written: datalake/projects/<project>/raw/web_20260423.jsonl
Watch report: dashboards/watch_<project>_20260423.html

Next: run /clean-dataset project=<project> to merge with existing dataset
```
