#!/usr/bin/env python3
"""Run the Phase 0 funding-harvest backtest on bundled sample data.

Usage:
    python scripts/run_backtest.py
"""

from __future__ import annotations

import os
import sys

# Allow running from a checkout without installing (src layout).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ajentix_quant.scripts_entry import run_backtest_main  # noqa: E402

if __name__ == "__main__":
    run_backtest_main()
