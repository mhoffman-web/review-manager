import os, sys, tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="rm_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["SECRET_KEY"] = "test-secret-key-not-for-prod"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
