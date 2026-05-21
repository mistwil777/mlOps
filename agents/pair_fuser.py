"""Pair fuser: cross-match monolingual doc pairs and generate migration pairs via LLM.

Workflow:
  1. Load source-lang pairs (e.g. python) and target-lang pairs (e.g. java) from JSONL
  2. Embed all descriptions (prompts) using nomic-embed-text
  3. For each source pair: find the K nearest target pairs by cosine similarity
  4. Call the LLM with (source_code, target_code, concept) → ask for an aligned migration pair
  5. Write fused pairs as JSONL: {prompt: python_code, completion: java_code, ...}

Generic: driven entirely by config/languages.yaml — no hardcoded language names.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.pair_fuser")

_OLLAMA_URL       = "http://localhost:11434/api/generate"
_EMBED_MODEL      = "nomic-embed-text"
_OLLAMA_GEN_MODEL = "qwen2.5-coder:7b"  # local LLM for synthesis — overridable
_RATE_LIMIT_S     = 0.3
_TOP_K_MATCHES    = 3    # candidates per source pair before LLM filter
_SIM_THRESHOLD    = 0.55 # min cosine similarity to attempt fusion
_HTTP_TIMEOUT     = 60


def _load_languages() -> dict[str, Any]:
    path = Path("config/languages.yaml")
    if not path.exists():
        raise FileNotFoundError("config/languages.yaml not found")
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _embed_ollama(texts: list[str]) -> list[list[float]]:
    """Embed texts via Ollama nomic-embed-text."""
    vecs = []
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        for text in texts:
            r = client.post(
                "http://localhost:11434/api/embeddings",
                json={"model": _EMBED_MODEL, "prompt": text[:1024]},
            )
            r.raise_for_status()
            vecs.append(r.json()["embedding"])
    return vecs


def _embed_sentence_transformers(texts: list[str]) -> list[list[float]]:
    """Embed via sentence-transformers, run in a subprocess with timeout to avoid PyTorch deadlocks."""
    import concurrent.futures  # noqa: PLC0415
    import json as _json        # noqa: PLC0415
    import subprocess           # noqa: PLC0415
    import sys as _sys          # noqa: PLC0415

    script = (
        "import json, sys\n"
        "from sentence_transformers import SentenceTransformer\n"
        "texts = json.loads(sys.argv[1])\n"
        "m = SentenceTransformer('nomic-ai/nomic-embed-text-v1.5', trust_remote_code=True)\n"
        "vecs = m.encode(texts, normalize_embeddings=True).tolist()\n"
        "print(json.dumps(vecs))\n"
    )
    proc = subprocess.run(
        [_sys.executable, "-c", script, _json.dumps(texts)],
        capture_output=True, text=True, timeout=30,
        env={**__import__("os").environ, "TOKENIZERS_PARALLELISM": "false"},
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "sentence-transformers subprocess failed")
    return _json.loads(proc.stdout.strip())


def _embed_sklearn_tfidf(texts: list[str]) -> list[list[float]]:
    """TF-IDF via scikit-learn — requires pip install scikit-learn.

    Fits on the combined corpus so cross-language technical vocabulary is shared.
    Lower semantic quality than neural embeddings but reliable and fast.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore  # noqa: PLC0415
    from sklearn.preprocessing import normalize                   # type: ignore  # noqa: PLC0415

    vec = TfidfVectorizer(sublinear_tf=True, ngram_range=(1, 2), analyzer="word", min_df=1)
    mat = vec.fit_transform(texts)
    mat = normalize(mat, norm="l2")
    return mat.toarray().tolist()


_EMBED_BACKEND: str = ""  # "ollama" | "sentence-transformers" | "tfidf" | "none"


def _embed(texts: list[str]) -> list[list[float]]:
    global _EMBED_BACKEND

    # 1. Ollama nomic-embed-text (best)
    if _EMBED_BACKEND in ("", "ollama"):
        try:
            result = _embed_ollama(texts)
            _EMBED_BACKEND = "ollama"
            return result
        except Exception:
            if _EMBED_BACKEND == "ollama":
                _EMBED_BACKEND = ""  # was working before — reset to retry next call
            else:
                _EMBED_BACKEND = "skip-ollama"

    # 2. sentence-transformers (good quality — may time out if PyTorch is broken on this host)
    if _EMBED_BACKEND in ("", "skip-ollama", "sentence-transformers"):
        try:
            logger.info("Ollama unavailable — trying sentence-transformers")
            result = _embed_sentence_transformers(texts)
            _EMBED_BACKEND = "sentence-transformers"
            return result
        except (ImportError, Exception) as exc:
            logger.warning("sentence-transformers unavailable: %s", exc)
            _EMBED_BACKEND = "tfidf"  # skip permanently for this run

    # 3. scikit-learn TF-IDF (fallback — shared vocabulary, cross-lang keyword matching)
    if _EMBED_BACKEND in ("tfidf",):
        try:
            logger.warning("Neural embeddings unavailable — falling back to TF-IDF (lower quality)")
            result = _embed_sklearn_tfidf(texts)
            return result
        except (ImportError, Exception) as exc:
            logger.warning("scikit-learn unavailable: %s", exc)
            _EMBED_BACKEND = "none"

    raise RuntimeError(
        "No embedding backend available for pair fusion.\n"
        "Fix (choose one):\n"
        "  1. Start Ollama:               ollama serve  (then: ollama pull nomic-embed-text)\n"
        "  2. Install sentence-transformers: pip install sentence-transformers\n"
        "  3. Install scikit-learn:          pip install scikit-learn  (lower quality, cross-lang)"
    )


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _find_matches(
    src_vecs: list[list[float]],
    tgt_vecs: list[list[float]],
    k: int = _TOP_K_MATCHES,
    threshold: float = _SIM_THRESHOLD,
) -> list[list[tuple[int, float]]]:
    """For each source vector, return top-k target indices above threshold."""
    results = []
    for sv in src_vecs:
        sims = [(i, _cosine(sv, tv)) for i, tv in enumerate(tgt_vecs)]
        top = sorted([(i, s) for i, s in sims if s >= threshold], key=lambda x: -x[1])[:k]
        results.append(top)
    return results


_FUSE_PROMPT_TEMPLATE = """\
You are an expert in code migration. Your task is to create a training pair for a \
fine-tuned model that migrates {source_lang} code to {target_lang}.

Here is an example of a {source_lang} implementation:
```{source_lang}
{source_code}
```

Here is a related {target_lang} implementation of a similar concept:
```{target_lang}
target_code_hint
```

Generate a clean, idiomatic {target_lang} version that directly corresponds to \
the {source_lang} code above. Output ONLY the {target_lang} code, no explanations.
""".replace("target_code_hint", "{target_code}")


def _call_llm(prompt: str, model: str) -> str | None:
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            r = client.post(
                _OLLAMA_URL,
                json={"model": model, "prompt": prompt, "stream": False},
            )
            r.raise_for_status()
            return r.json().get("response", "").strip()
    except Exception as exc:
        logger.warning("LLM call failed: %s", exc)
        return None


def _extract_code_block(text: str, lang: str) -> str:
    """Extract first fenced code block from LLM response, or return raw text."""
    import re  # noqa: PLC0415
    m = re.search(rf"```{re.escape(lang)}\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


class PairFuser:
    """
    Cross-match source-lang and target-lang doc pairs, generate migration pairs via LLM.
    Generic: source_lang, target_lang driven by config/languages.yaml entry.
    """

    def __init__(
        self,
        source_lang: str,
        target_lang: str,
        llm_model: str = _OLLAMA_GEN_MODEL,
        top_k: int = _TOP_K_MATCHES,
        sim_threshold: float = _SIM_THRESHOLD,
    ):
        self.source_lang   = source_lang
        self.target_lang   = target_lang
        self.llm_model     = llm_model
        self.top_k         = top_k
        self.sim_threshold = sim_threshold

    def fuse(
        self,
        source_pairs: list[dict[str, Any]],
        target_pairs: list[dict[str, Any]],
        max_fused: int = 200,
    ) -> list[dict[str, Any]]:
        if not source_pairs or not target_pairs:
            logger.warning("Empty source or target pairs — nothing to fuse")
            return []

        logger.info("Embedding %d source + %d target descriptions…", len(source_pairs), len(target_pairs))
        src_texts = [p["prompt"] for p in source_pairs]
        tgt_texts = [p["prompt"] for p in target_pairs]

        # Embed source and target in a single call so that TF-IDF (fallback) builds
        # its vocabulary on the combined corpus — cross-language similarity only works
        # when both languages share the same vector space.
        all_vecs  = _embed(src_texts + tgt_texts)
        src_vecs  = all_vecs[:len(src_texts)]
        tgt_vecs  = all_vecs[len(src_texts):]

        # Lower the threshold when using TF-IDF (weaker cross-lang signal vs neural embeddings)
        threshold = self.sim_threshold
        if _EMBED_BACKEND == "tfidf":
            threshold = min(threshold, 0.05)
            logger.info("TF-IDF active — sim_threshold lowered to %.2f", threshold)

        matches = _find_matches(src_vecs, tgt_vecs, k=self.top_k, threshold=threshold)

        fused: list[dict[str, Any]] = []
        for i, top in enumerate(matches):
            if len(fused) >= max_fused:
                break
            if not top:
                continue

            src = source_pairs[i]
            # Take best match only for LLM call
            best_idx, best_sim = top[0]
            tgt = target_pairs[best_idx]

            logger.info(
                "[%d/%d] sim=%.3f — %s → %s",
                i + 1, len(source_pairs), best_sim,
                src["prompt"][:60], tgt["prompt"][:60],
            )

            prompt = _FUSE_PROMPT_TEMPLATE.format(
                source_lang=self.source_lang,
                target_lang=self.target_lang,
                source_code=src["completion"][:1500],
                target_code=tgt["completion"][:1500],
            )

            response = _call_llm(prompt, self.llm_model)
            if not response:
                continue

            java_code = _extract_code_block(response, self.target_lang)
            if len(java_code) < 20:
                continue

            fused.append({
                "prompt":          src["completion"],   # source code as prompt
                "completion":      java_code,           # generated target code
                "source_lang":     self.source_lang,
                "target_lang":     self.target_lang,
                "source_desc":     src["prompt"],
                "target_desc_hint": tgt["prompt"],
                "source_url":      src.get("source_url"),
                "target_url":      tgt.get("source_url"),
                "similarity":      round(best_sim, 4),
                "collector_type":  "fused",
                "collected_at":    datetime.now(timezone.utc).isoformat(),
            })

            time.sleep(_RATE_LIMIT_S)

        logger.info("Generated %d fused pairs", len(fused))
        return fused

    def run(
        self,
        source_jsonl: Path,
        target_jsonl: Path,
        output: Path,
        adapter_name: str | None = None,
        max_fused: int = 200,
    ) -> int:
        source_pairs = read_jsonl(source_jsonl) if source_jsonl.exists() else []
        target_pairs = read_jsonl(target_jsonl) if target_jsonl.exists() else []

        # Filter to relevant lang
        source_pairs = [p for p in source_pairs if p.get("lang") == self.source_lang or not p.get("lang")]
        target_pairs = [p for p in target_pairs if p.get("lang") == self.target_lang or not p.get("lang")]

        logger.info("Loaded %d source (%s) + %d target (%s) pairs",
                    len(source_pairs), self.source_lang, len(target_pairs), self.target_lang)

        fused = self.fuse(source_pairs, target_pairs, max_fused=max_fused)

        if adapter_name:
            for p in fused:
                p["adapter_name"] = adapter_name

        if fused:
            output.parent.mkdir(parents=True, exist_ok=True)
            existing = read_jsonl(output) if output.exists() else []
            seen = {(r.get("prompt", ""), r.get("completion", "")) for r in existing}
            new_unique = [p for p in fused if (p["prompt"], p["completion"]) not in seen]
            write_jsonl(output, existing + new_unique)
            logger.info("Written %d fused pairs → %s", len(new_unique), output)

        return len(fused)


def main() -> None:
    cfg = _load_languages()
    pairs_cfg = cfg.get("pairs", {})

    parser = argparse.ArgumentParser(description="Fuse monolingual doc pairs into migration pairs via LLM")
    parser.add_argument("--pair",         required=True, choices=list(pairs_cfg.keys()),
                        help="Language pair key from config/languages.yaml")
    parser.add_argument("--source-jsonl", required=True, dest="source_jsonl")
    parser.add_argument("--target-jsonl", required=True, dest="target_jsonl")
    parser.add_argument("--output",       required=True)
    parser.add_argument("--max-fused",    type=int, default=200, dest="max_fused")
    parser.add_argument("--model",        default=_OLLAMA_GEN_MODEL)
    parser.add_argument("--adapter-name", default=None, dest="adapter_name")
    args = parser.parse_args()

    pcfg = pairs_cfg[args.pair]
    fuser = PairFuser(
        source_lang=pcfg["source_lang"],
        target_lang=pcfg["target_lang"],
        llm_model=args.model,
    )
    n = fuser.run(
        source_jsonl=Path(args.source_jsonl),
        target_jsonl=Path(args.target_jsonl),
        output=Path(args.output),
        adapter_name=args.adapter_name,
        max_fused=args.max_fused,
    )
    print(f"Fused {n} migration pairs → {args.output}")


if __name__ == "__main__":
    main()
