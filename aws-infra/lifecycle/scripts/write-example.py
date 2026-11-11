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
parser.add_argument("--track2", action="store_true")
args = parser.parse_args()
need(not args.output.exists(), "refusing to replace an existing file")
if args.track2:
    from track2_support import track2_fixture
    atomic(args.output, track2_fixture())
else:
    atomic(args.output, fixture())
