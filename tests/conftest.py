"""Expose this flat CLI repository under both supported pytest entry points."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
