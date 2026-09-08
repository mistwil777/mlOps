"""Prompt templates for pair generation and correction during raw ingestion."""

from __future__ import annotations


PAIR_GENERATION = """\
You are a training data specialist for a fine-tuning pipeline.

Project context:
- Domain: {domain}
- Task type: {task_type}
- Adapter: {adapter_name}
- Adapter description: {adapter_description}

Extract {n_pairs} high-quality training pairs from the content below.
Each pair must have:
- "input": the instruction or source input (question, code, prompt)
- "output": the expected model response (answer, translated code, completion)

Rules:
- Pairs must be self-contained and meaningful without external context
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

Evaluate the training pair below. Return a JSON object with:
- "score": float 0.0-1.0 (1.0 = perfect)
- "error_type": short category if score < 0.8 (e.g. "incorrect_typing", "missing_context", "wrong_language", "too_short"), else null
- "error_description": one sentence describing the issue, else null
- "suggested_fix": one sentence on how to fix it, else null

[PAIR]
Input: {input}
Output: {output}

JSON:"""
