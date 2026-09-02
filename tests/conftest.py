"""Ensure the repo root (where watch.py lives) is importable regardless of cwd."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))