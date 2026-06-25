"""Execution layer. Phase 0 ships a paper/dry-run executor only — no live orders."""

from .paper import PaperExecutor

__all__ = ["PaperExecutor"]
