"""Reversible anonymization before text leaves the machine (standard library only).

Detects: the project's `sensitive_terms` (customer names, code names...), emails, IPv4 addresses,
phone numbers, IBANs, bank card numbers (Luhn), French social security numbers and common secret tokens.
Each distinct value gets a stable placeholder such as [EMAIL_1]; `restore` puts the originals back
into the LLM response, locally.

LIMIT: this is pattern-based. It does NOT detect person names, places or free-text identifiers unless
they are listed in `sensitive_terms`. It lowers the risk, it is not a guarantee.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SECRET", re.compile(r"\b(?:AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_\-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|xox[abprs]-[A-Za-z0-9\-]{10,})\b")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b")),
    ("NIR", re.compile(r"\b[12] ?\d{2} ?(?:0[1-9]|1[0-2]) ?\d{2} ?\d{3} ?\d{3} ?\d{2}\b")),
    ("IP", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
    ("PHONE", re.compile(r"(?<![\w.])(?:\+|00)\d{1,3}[ .\-]?\(?\d{1,4}\)?(?:[ .\-]?\d{2,4}){2,4}\b|(?<![\w.])0\d(?:[ .\-]?\d{2}){4}\b")),
]
_CARD = re.compile(r"\b(?:\d[ \-]?){13,19}\b")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


@dataclass
class Anonymized:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)  # placeholder -> original
    counts: dict[str, int] = field(default_factory=dict)    # entity kind -> number of distinct values

    @property
    def n_entities(self) -> int:
        return len(self.mapping)


class Anonymizer:
    def __init__(self, sensitive_terms: list[str] | None = None) -> None:
        terms = sorted({t.strip() for t in (sensitive_terms or []) if t and t.strip()}, key=len, reverse=True)
        self._terms = [(t, re.compile(rf"(?<!\w){re.escape(t)}(?!\w)", re.IGNORECASE)) for t in terms]

    def anonymize(self, text: str) -> Anonymized:
        mapping: dict[str, str] = {}
        by_value: dict[tuple[str, str], str] = {}
        counts: dict[str, int] = {}

        def placeholder(kind: str, value: str) -> str:
            key = (kind, value)  # exact text: each casing variant keeps its own placeholder (exact round trip)
            if key not in by_value:
                counts[kind] = counts.get(kind, 0) + 1
                by_value[key] = f"[{kind}_{counts[kind]}]"
                mapping[by_value[key]] = value
            return by_value[key]

        # Structured patterns first, so a term inside an email cannot split it and leak its other half.
        for kind, pattern in _PATTERNS:
            text = pattern.sub(lambda m, k=kind: placeholder(k, m.group(0)), text)

        def _card(m: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", m.group(0))
            return placeholder("CARD", m.group(0)) if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)

        text = _CARD.sub(_card, text)
        for _, pattern in self._terms:
            text = pattern.sub(lambda m: placeholder("TERM", m.group(0)), text)
        return Anonymized(text=text, mapping=mapping, counts=counts)

    @staticmethod
    def restore(text: str, mapping: dict[str, str]) -> str:
        for ph in sorted(mapping, key=len, reverse=True):
            text = text.replace(ph, mapping[ph])
        return text
