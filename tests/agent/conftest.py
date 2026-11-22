import sys
from pathlib import Path

# Make `import api...` work for plain `pytest` (no pyproject/pythonpath configured).
ROOT = str(Path(__file__).resolve().parents[2])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


import pytest


@pytest.fixture(autouse=True)
def _tmp_checkpoint_db(tmp_path, monkeypatch):
    """Never write data/checkpoints.db from tests."""
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
