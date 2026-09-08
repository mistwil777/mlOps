"""Backward-compatibility wrapper — delegates to pair_forge.PairForgeAgent.

All logic lives in the pair_forge package. This module exists so that
existing imports in pipeline/ and api/ continue to work unchanged.

    from pipeline.ingest.agent import IngestionAgent
"""

from __future__ import annotations

from pair_forge.agent import PairForgeAgent as IngestionAgent

__all__ = ["IngestionAgent"]
