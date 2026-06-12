"""Dataset cleaning: deduplication, filtering, scoring, train/valid split."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import read_jsonl, write_jsonl, count_tokens
from pipeline.utils.llm import call_llm
from pipeline.utils.logger import get_logger, log_metric

logger = get_logger("mlops.clean")

_MLX_SERVER_URL = "http://localhost:8080"

# Languages for which py_compile syntax check applies
_PYTHON_LANGS = {"python", "py", ""}


def _syntax_check(code: str, lang: str) -> tuple[bool, str]:
    """Check syntax of a code snippet. Returns (valid, error_message)."""
    if not code.strip():
        return False, "empty"
    lang = (lang or "").lower().strip()

    if lang in _PYTHON_LANGS:
        import py_compile, tempfile, os  # noqa: PLC0415,E401
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, encoding="utf-8") as f:
            f.write(code)
            tmp = f.name
        try:
            py_compile.compile(tmp, doraise=True)
            return True, ""
        except py_compile.PyCompileError as e:
            return False, str(e)
        finally:
            os.unlink(tmp)

    try:
        import tree_sitter_languages  # noqa: PLC0415
        from tree_sitter import Language, Parser  # noqa: PLC0415
        lang_obj = tree_sitter_languages.get_language(lang)
        parser = Parser()
        parser.set_language(lang_obj)
        tree = parser.parse(code.encode())
        if tree.root_node.has_error:
            return False, "tree-sitter parse error"
        return True, ""
    except Exception:
        return True, ""  # unknown lang or tree-sitter unavailable → pass through


def _load_project_config(project: str) -> dict[str, Any]:
    path = Path("config/projects") / f"{project}.yaml"
    with path.open() as fh:
        return yaml.safe_load(fh)


def _record_hash(record: dict[str, Any]) -> str:
    key = record.get("prompt", "") + record.get("completion", "")
    return hashlib.md5(key.encode()).hexdigest()


def _mlx_server_available() -> bool:
    try:
        import httpx  # noqa: PLC0415
        r = httpx.get(f"{_MLX_SERVER_URL}/v1/models", timeout=2.0)
        return r.status_code == 200 and len(r.json().get("data", [])) > 0
    except Exception:
        return False


class DataCleaner:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    def deduplicate(self, pairs: list[dict[str, Any]], method: str = "exact+minhash") -> list[dict[str, Any]]:
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []

        for p in pairs:
            h = _record_hash(p)
            if h not in seen:
                seen.add(h)
                unique.append(p)

        removed = len(pairs) - len(unique)
        logger.info("Dedup (exact): removed %d duplicates, %d remaining", removed, len(unique))
        log_metric("dedup_removed", removed)

        if "minhash" in method:
            unique = self._minhash_dedup(unique)

        return unique

    def _minhash_dedup(
        self,
        pairs: list[dict[str, Any]],
        threshold: float = 0.85,
    ) -> list[dict[str, Any]]:
        """Near-duplicate detection using token n-gram Jaccard similarity."""
        def shingles(text: str, k: int = 5) -> set[str]:
            tokens = text.split()
            return {" ".join(tokens[i:i+k]) for i in range(max(1, len(tokens) - k + 1))}

        kept: list[dict[str, Any]] = []
        kept_shingles: list[set[str]] = []

        for p in pairs:
            s = shingles(p.get("prompt", "") + p.get("completion", ""))
            is_dup = any(
                len(s & ks) / max(len(s | ks), 1) >= threshold
                for ks in kept_shingles
            )
            if not is_dup:
                kept.append(p)
                kept_shingles.append(s)

        removed = len(pairs) - len(kept)
        logger.info("Dedup (minhash): removed %d near-duplicates", removed)
        return kept

    def filter_by_rules(
        self,
        pairs: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Apply rule-based filtering. Returns (kept, rejected)."""
        cfg = config or self.config
        quality = cfg.get("quality_thresholds", {})
        min_tok = cfg.get("min_tokens", 30)
        max_tok = quality.get("max_tokens", 2048)
        min_ratio = cfg.get("min_output_ratio", 0.3)
        max_ratio = cfg.get("max_output_ratio", 10.0)
        blacklist = cfg.get("blacklist_patterns", ["TODO", "FIXME", "password", "secret"])

        kept: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        for p in pairs:
            prompt_tok = count_tokens(p.get("prompt", ""))
            completion_tok = count_tokens(p.get("completion", ""))
            total_tok = prompt_tok + completion_tok
            reason = None

            if total_tok < min_tok:
                reason = "too_short"
            elif total_tok > max_tok:
                reason = "too_long"
            elif completion_tok == 0 or prompt_tok / max(completion_tok, 1) > max_ratio:
                reason = "bad_ratio"
            elif prompt_tok > 0 and completion_tok / prompt_tok < min_ratio:
                reason = "bad_ratio"
            else:
                text = p.get("prompt", "") + p.get("completion", "")
                if any(bl in text for bl in blacklist):
                    reason = "blacklisted"

            if reason:
                rejected.append({**p, "reject_reason": reason})
            else:
                kept.append(p)

        logger.info("Filtering: %d kept, %d rejected", len(kept), len(rejected))
        log_metric("filtered_removed", len(rejected))
        return kept, rejected

    def syntax_filter(
        self,
        pairs: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Filter pairs whose completion fails syntax check. Returns (kept, rejected)."""
        kept, rejected = [], []
        src_lang = self.config.get("transform", {}).get("params", {}).get("src_lang", "")
        tgt_lang = self.config.get("transform", {}).get("params", {}).get("tgt_lang", "")

        for p in pairs:
            completion = p.get("completion", "")
            # Determine language of the completion from meta or project config
            lang = p.get("target_lang") or p.get("lang") or tgt_lang or src_lang
            valid, err = _syntax_check(completion, lang)
            if valid:
                kept.append(p)
            else:
                rejected.append({**p, "reject_reason": f"syntax_error: {err}"})

        logger.info("Syntax filter: %d kept, %d rejected", len(kept), len(rejected))
        log_metric("syntax_rejected", len(rejected))
        return kept, rejected

    def score_pairs(
        self,
        pairs: list[dict[str, Any]],
        use_llm: bool = True,
    ) -> list[dict[str, Any]]:
        """Score pairs: heuristic first, then LLM judge if available."""
        # Step 1 — heuristic (always)
        scored = self._heuristic_score(pairs)

        if not use_llm:
            return scored

        # Step 2 — LLM judge via Bedrock (cascade: only on heuristically-passing pairs)
        threshold_heuristic = 0.5
        to_llm = [p for p in scored if p.get("quality_score", 0) >= threshold_heuristic]
        skip = [p for p in scored if p.get("quality_score", 0) < threshold_heuristic]

        if not to_llm:
            return scored

        logger.info("LLM judge: scoring %d/%d pairs (skipping %d below heuristic threshold)",
                    len(to_llm), len(pairs), len(skip))

        llm_scored = []
        failed = 0
        for p in to_llm:
            llm_s = self._llm_judge_score(p)
            if llm_s is None:
                failed += 1
                llm_scored.append(p)  # keep heuristic score
            else:
                # Weighted blend: 30% heuristic + 70% LLM judge
                blended = 0.3 * p["quality_score"] + 0.7 * llm_s
                llm_scored.append({**p, "quality_score": round(blended, 3),
                                   "llm_score": round(llm_s, 3)})

        if failed:
            logger.warning("LLM judge: %d calls failed, kept heuristic score for those", failed)

        return skip + llm_scored

    def _llm_judge_score(self, pair: dict[str, Any]) -> float | None:
        """Call Bedrock/LLM to semantically score a pair. Returns 0-1 or None on failure."""
        src_lang = pair.get("source_lang") or self.config.get("transform", {}).get("params", {}).get("src_lang", "code")
        tgt_lang = pair.get("target_lang") or self.config.get("transform", {}).get("params", {}).get("tgt_lang", "code")
        domain = self.config.get("domain", "code")

        prompt = (
            f"You are a strict {domain} code migration quality judge.\n"
            f"Rate the following {src_lang}→{tgt_lang} training pair from 0 to 10:\n"
            f"- 0-3: incorrect translation (logic errors, wrong constructs)\n"
            f"- 4-6: partially correct (minor issues, non-idiomatic)\n"
            f"- 7-9: correct and idiomatic\n"
            f"- 10: perfect\n\n"
            f"Reply with a single integer ONLY.\n\n"
            f"SOURCE ({src_lang}):\n```\n{pair.get('prompt', '')[:600]}\n```\n\n"
            f"TRANSLATION ({tgt_lang}):\n```\n{pair.get('completion', '')[:600]}\n```"
        )
        try:
            response = call_llm(prompt, max_tokens=4, temperature=0.0)
            if not response:
                return None
            score = float(response.strip().split()[0])
            return min(1.0, max(0.0, score / 10.0))
        except Exception:  # noqa: BLE001
            return None

    def _heuristic_score(self, pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fast scoring without LLM: token ratio, length, structural signals."""
        scored = []
        for p in pairs:
            prompt_tok = count_tokens(p.get("prompt", ""))
            completion_tok = count_tokens(p.get("completion", ""))
            ratio = completion_tok / max(prompt_tok, 1)
            completion = p.get("completion", "")

            score = 0.5
            if 0.5 <= ratio <= 3.0:
                score += 0.2
            if len(completion.strip().splitlines()) > 3:
                score += 0.15
            if any(kw in completion for kw in ("import", "class ", "def ", "function", "public", "return")):
                score += 0.15

            scored.append({**p, "quality_score": min(1.0, score)})
        return scored

    def _mlx_score(self, pair: dict[str, Any], httpx: Any) -> float:
        """Call mlx_lm.server to score a training pair. Returns 0-1."""
        domain = self.config.get("domain", "code")
        task_type = self.config.get("task_type", "generation")

        prompt = (
            f"Rate the quality of this {domain}/{task_type} training pair from 0 to 10. "
            f"Consider: accuracy, completeness, consistency between input and output. "
            f"Reply with a single integer only.\n\n"
            f"INPUT:\n{pair.get('prompt', '')[:500]}\n\n"
            f"OUTPUT:\n{pair.get('completion', '')[:500]}"
        )
        try:
            resp = httpx.post(
                f"{_MLX_SERVER_URL}/v1/chat/completions",
                json={
                    "model": "default",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 8,
                    "temperature": 0.0,
                },
                timeout=30.0,
            )
            text = resp.json()["choices"][0]["message"]["content"].strip()
            return float(text.split()[0]) / 10.0
        except Exception:  # noqa: BLE001
            return 0.5

    def train_valid_split(
        self,
        pairs: list[dict[str, Any]],
        valid_ratio: float = 0.1,
        seed: int = 42,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        random.seed(seed)
        shuffled = pairs.copy()
        random.shuffle(shuffled)
        n_valid = max(1, int(len(shuffled) * valid_ratio))
        return shuffled[n_valid:], shuffled[:n_valid]

    def generate_quality_report(
        self,
        pairs: list[dict[str, Any]],
        output_path: Path | str,
    ) -> None:
        scores = [p.get("quality_score", 0) for p in pairs if "quality_score" in p]

        buckets = {"0.0-0.5": 0, "0.5-0.7": 0, "0.7-0.9": 0, "0.9-1.0": 0}
        for s in scores:
            if s < 0.5:
                buckets["0.0-0.5"] += 1
            elif s < 0.7:
                buckets["0.5-0.7"] += 1
            elif s < 0.9:
                buckets["0.7-0.9"] += 1
            else:
                buckets["0.9-1.0"] += 1

        mean_score = sum(scores) / len(scores) if scores else 0.0

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Quality Report</title>
<style>body{{font-family:monospace;margin:2em}} table{{border-collapse:collapse}}
td,th{{border:1px solid #ccc;padding:8px}}</style></head><body>
<h1>Dataset Quality Report</h1>
<p>Total pairs: {len(pairs)} | Scored: {len(scores)} | Mean score: {mean_score:.3f}</p>
<table><tr><th>Score bucket</th><th>Count</th></tr>
{"".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k,v in buckets.items())}
</table></body></html>"""

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(html, encoding="utf-8")
        logger.info("Quality report written to %s", output_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--score", type=lambda x: x.lower() == "true", default=True)
    parser.add_argument("--threshold", type=float, default=0.7)
    parser.add_argument("--input", default=None,
                        help="Override input path (default: merge all processed/pairs/<adapter>.jsonl)")
    args = parser.parse_args()

    config = _load_project_config(args.project)
    train_path = Path(f"datalake/projects/{args.project}/fine-tuning-ready/train.jsonl")
    valid_path = Path(f"datalake/projects/{args.project}/fine-tuning-ready/valid.jsonl")

    if args.input:
        input_path = Path(args.input)
        raw = read_jsonl(input_path)
        logger.info("Loaded %d raw pairs from %s", len(raw), input_path)
    else:
        pairs_dir = Path(f"datalake/projects/{args.project}/processed/pairs")
        raw = []
        if pairs_dir.exists():
            for f in sorted(pairs_dir.glob("*.jsonl")):
                records = read_jsonl(f)
                raw.extend(records)
                logger.info("Loaded %d pairs from %s", len(records), f.name)
        if not raw:
            raise FileNotFoundError(
                f"No pairs found in {pairs_dir}. Run pipeline/transform.py first."
            )
        logger.info("Total: %d pairs from %s", len(raw), pairs_dir)

    cleaner = DataCleaner(config)
    deduped = cleaner.deduplicate(raw)
    kept, _ = cleaner.filter_by_rules(deduped)
    kept, syntax_rejected = cleaner.syntax_filter(kept)
    logger.info("After syntax filter: %d pairs", len(kept))
    scored = cleaner.score_pairs(kept, use_llm=args.score)
    accepted = [p for p in scored if p.get("quality_score", 1.0) >= args.threshold]

    train, valid = cleaner.train_valid_split(accepted)
    write_jsonl(train_path, train)
    write_jsonl(valid_path, valid)

    log_metric("n_train", len(train))
    log_metric("n_valid", len(valid))

    print(f"Input: {len(raw)} | Deduped: {len(deduped)} | Filtered: {len(kept)} | "
          f"Accepted: {len(accepted)}")
    print(f"Train: {len(train)} → {train_path}")
    print(f"Valid: {len(valid)} → {valid_path}")


if __name__ == "__main__":
    main()
