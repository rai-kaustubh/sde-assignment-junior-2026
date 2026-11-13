import sys
from pathlib import Path

SOLUTION_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SOLUTION_DIR.parent
SCHEMAS_DIR = REPO_ROOT / "assignment-a-data" / "schemas"

sys.path.insert(0, str(SOLUTION_DIR))  # the solution modules import each other as top-level names
