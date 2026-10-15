#!/usr/bin/env python3
"""Run momcozy directly without installing."""

import sys
from pathlib import Path

# Ensure package directory is on Python path
repo_root = Path(__file__).resolve().parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from momcozy.cli import main

if __name__ == "__main__":
    main()
