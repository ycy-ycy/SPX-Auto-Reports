"""Repository entry point for the portable Cboe report skill."""

import runpy
import sys
from pathlib import Path

sys.dont_write_bytecode = True
runpy.run_path(
    str(Path(__file__).resolve().parents[1] / ".agents" / "skills"
        / "cboe-spx-reports" / "scripts" / "spx_reports.py"),
    run_name="__main__",
)
