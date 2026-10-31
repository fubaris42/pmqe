"""A job function for pool tests: kills its worker process on 'crash' files.

Lives in its own module so spawned worker processes can import it.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from pmqe import extract
from pmqe.models import FileResult


def run_job(paths: Sequence[str], output_dir: str) -> list[FileResult]:
    for p in paths:
        if "crash" in os.path.basename(p):
            os._exit(3)  # simulate a native-library crash
    return extract.run_job(paths, output_dir)
