#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run Desktop Checkup directly from a source checkout."""

import sys
from pathlib import Path
import runpy

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

if __name__ == "__main__":
    runpy.run_module("health_check.app", run_name="__main__")
