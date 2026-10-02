"""Scratch directory for test databases and temporary files.

It lives inside the repository because the sandbox used during development denies
writes to the OS temp directory. ``DIET_AGENT_TEST_TMP`` overrides the parent, which is
useful when a restricted environment cannot write into an existing directory.
"""

import os
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / ".tmp"

# Parent of the per-test scratch directories; every test file shares this one place.
TEST_SCRATCH_ROOT = Path(os.environ.get("DIET_AGENT_TEST_TMP") or DEFAULT_ROOT) / "tests"
