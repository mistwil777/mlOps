"""Prompt templates for pair generation and correction during raw ingestion."""

from __future__ import annotations


CONTENT_EXTRACTION = """\
You are a content pre-processor for a fine-tuning pipeline.

Adapter: {adapter_name}
Description: {adapter_description}
Domain: {domain}

From the content below, extract every block that is relevant to this adapter and domain.
Include: domain-specific concepts, procedures, rules, examples, comparisons, definitions.
Exclude: generic boilerplate, headers/footers, page numbers, repeated navigation text.

Return each extracted block as a separate string in a JSON array.
If there is absolutely nothing relevant, return [].
Output ONLY the JSON array, no explanation.

Content:
{content}

JSON array:"""


PAIR_GENERATION = """\
You are a training data specialist for a fine-tuning pipeline.

Project context:
- Domain: {domain}
- Task type: {task_type}
- Adapter: {adapter_name}
- Adapter description: {adapter_description}

{rag_context_block}
{kg_context_block}

Extract {n_pairs} high-quality training pairs from the content below.
Each pair must have:
- "input": the instruction or source input (question, document excerpt, scenario)
- "output": the expected model response (answer, analysis, procedure, transformation)

Rules:
- Pairs must be self-contained and meaningful without external context
- Respect domain semantics — do not simplify or paraphrase concepts that carry precise meaning
- Minimum {min_tokens} tokens per output
- Maximum {max_tokens} tokens per output
- Output ONLY a JSON array, no explanation

Content:
{content}

JSON array:"""


PAIR_CORRECTION = """\
You are a training data specialist. Correct the pair below based on the judge feedback.

[PAIR TO CORRECT]
Input: {input}
Output: {output}

[JUDGE FEEDBACK]
Error type: {error_type}
Description: {error_description}
Suggestion: {suggested_fix}

[SIMILAR PAST ERRORS AND THEIR FIXES]
{memory_context}

Generate a corrected version of the output only.
Output ONLY a JSON object with keys "input" and "output", no explanation.

JSON:"""


JUDGE_EVALUATION = """\
You are a quality judge for fine-tuning training data.

Project context:
- Domain: {domain}
- Adapter: {adapter_name}

{kg_constraints_block}

Evaluate the training pair below. Return a JSON object with:
- "score": float 0.0-1.0 (1.0 = perfect)
- "error_type": short category if score < 0.8 (e.g. "incorrect_typing", "missing_context", \
"wrong_language", "too_short", "semantic_error", "constraint_violation"), else null
- "error_description": one sentence describing the issue, else null
- "suggested_fix": one sentence on how to fix it, else null

[PAIR]
Input: {input}
Output: {output}

JSON:"""


def _format_rag_context(rag_context: str) -> str:
    if not rag_context or not rag_context.strip():
        return ""
    return f"Domain knowledge context (from norms and reference documents):\n{rag_context}\n"


def _format_kg_context(kg_context: str) -> str:
    if not kg_context or not kg_context.strip():
        return ""
    return f"Known domain relationships (Knowledge Graph):\n{kg_context}\n"


def _format_kg_constraints(constraints: list[str]) -> str:
    if not constraints:
        return ""
    items = "\n".join(f"  - {c}" for c in constraints)
    return f"Domain constraints that the pair MUST respect:\n{items}\n"
