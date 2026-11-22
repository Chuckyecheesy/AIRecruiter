import sys
from pathlib import Path

# Make `import api...` work for plain `pytest` (no pyproject/pythonpath configured).
ROOT = str(Path(__file__).resolve().parents[2])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
