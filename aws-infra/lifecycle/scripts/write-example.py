#!/usr/bin/env python3
"""Write a new synthetic input only. Never creates an approval or authority key."""

import argparse
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "tests")]
from support import fixture
from mlo_lifecycle.journal import atomic
from mlo_lifecycle.model import need

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
need(not args.output.exists(), "refusing to replace an existing file")
atomic(args.output, fixture())
