"""Ensures the project root is importable as `learn` regardless of how
pytest is invoked (plain `pytest`, `python -m pytest`, from a subdirectory, etc.)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
