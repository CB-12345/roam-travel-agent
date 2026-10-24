"""Keep API tests out of the running local application's task database."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

_task_test_directory = TemporaryDirectory(prefix="roam-tests-")
os.environ["TRAVEL_TASK_DB"] = str(Path(_task_test_directory.name) / "tasks.sqlite3")
