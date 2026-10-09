"""Windows click-to-run reports: show results and open only a newly written PNG."""

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = {"zero-dte": "spx_0dte.png", "index-mtd": "cboe_index_mtd.png"}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in OUTPUTS:
        print("Usage: run_report.py {zero-dte,index-mtd}", file=sys.stderr)
        return 1
    report = args[0]
    try:
        completed = subprocess.run(
            [sys.executable, "-B", str(ROOT / "tools" / "spx_reports.py"),
             report, "--output-dir", str(ROOT)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace",
        )
        if completed.stdout:
            print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        if completed.returncode != 0:
            print("Report failed; no image opened.", file=sys.stderr)
            return 1
        result = json.loads(completed.stdout)
        if result.get("status") == "skipped":
            print(f"Skipped: {result.get('reason', 'No applicable session')}")
            print("No new image was generated; previous images were not opened.")
            return 0
        if result.get("status") != "ok":
            print(f"Report failed: {result.get('error', 'Unexpected status')}", file=sys.stderr)
            return 1
        output = ROOT / OUTPUTS[report]
        if Path(result["output"]).resolve() != output.resolve() or not output.is_file():
            raise ValueError("Successful result did not identify the expected PNG")
        if report == "index-mtd":
            print(f"Month: {result['month']}; through: {result['through']}; "
                  f"trading days: {result['trading_days']}")
            print("MTD ADV (contracts / trading day):")
            for group in ("SPX", "VIX", "Other"):
                print(f"  {group}: {result['adv'][group]:,.2f}")
            print(f"  Total: {result['total_adv']:,.2f}")
        else:
            print(f"Session: {result['date']}")
        print(f"New image: {output}")
        try:
            os.startfile(str(output), cwd=str(ROOT))
        except OSError as exc:
            print(f"PNG generated, but could not open it: {exc}", file=sys.stderr)
            return 1
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Report launcher failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
