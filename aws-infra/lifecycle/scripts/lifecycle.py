#!/usr/bin/env python3
"""Use with lifecycle/.venv/bin/python; no real execution option exists."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mlo_lifecycle.__main__ import main

raise SystemExit(main())
