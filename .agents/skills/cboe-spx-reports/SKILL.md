---
name: cboe-spx-reports
description: Generate SPX closing 0DTE put/call volume and Cboe month-to-date index option volume charts from public Cboe data. Use for an evening SPX report or a morning index volume report, including scheduled runs.
---

# Cboe SPX reports

This folder is the complete portable skill, including its Python tool and dependencies.
Copy the entire `cboe-spx-reports` folder to another agent's skill directory to transfer it.
No repository-specific absolute paths, credentials, or other skills are required.

## Setup

Use Python 3.11 or newer. Install `requirements.txt` into a local virtual environment.
Resolve the script relative to this skill: `scripts/spx_reports.py`. In the original
repository, `tools/spx_reports.py` is an equivalent convenience entry point.
Use `python -B` to avoid leaving bytecode files.

## Choose the report

- **Evening, approximately 20:00 America/Chicago:**
  `python -B <skill-folder>/scripts/spx_reports.py zero-dte --output-dir <report-directory>`
  produces `spx_0dte.png`. It selects the latest session whose cash close plus
  the 20-minute delayed-data window has passed, falling back to the previous
  completed trading day before today's close or on weekends and holidays.
  A run just after CT midnight still reports the preceding session if its
  Cboe data is retained; do not skip simply because CT today has not closed.
  It plots that session's SPX regular-session 5-minute candles
  (green up, red down), and cumulative same-session PM-settled SPXW put/call contract
  volume at the ten strikes nearest the official SPX daily close. Puts extend
  left in red and calls right in green on a symmetric axis. `--strikes N` changes
  the number of strikes. Ten means ten total strikes, not ten on each side.
- **Morning, approximately 09:00 America/Chicago:**
  `python -B <skill-folder>/scripts/spx_reports.py index-mtd --output-dir <report-directory>`
  produces `cboe_index_mtd.png`. The target month contains yesterday's CT calendar
  date. Include only completed trading days from that month's first day through
  yesterday. Plot SPX daily candles above stacked index option volume, with SPX
  at the base, VIX above it, and Other at the top. Plot a separate stacked MTD
  ADV bar beside daily volume on the same row, sharing its y-axis and zero
  baseline, with numbers for all three groups and the total.

Set the scheduler timezone to `America/Chicago`, not a fixed UTC offset. DST changes
the UTC time. The tool resolves CT today itself; do not substitute the machine's
local date. The morning task must still run on the first day of a new month, so it
can report the preceding month. These instructions describe scheduler invocations;
they do not install or enable a scheduler.

## Data invariants

Read [references/data-sources.md](references/data-sources.md) when inspecting or
changing source schemas, market-session handling, or aggregation.

- Fetch option quotes and SPX OHLC from Cboe, not SPY or a substitute ticker.
  When the historical daily series has not published the selected 0DTE day,
  use Cboe's same-day SPX index closing quote `close`, validating its symbol,
  security type, OHLC, and last-trade date/time at or after cash close. Report
  `close_source`; never substitute the final minute or previous-day close.
- The evening report uses `volume`, not open interest, and excludes AM-settled
  standard SPX contracts whose last trading day preceded their expiration date.
  Use the actual selected session date for expiry and freshness checks. Return
  that date from the JSON instead of labeling a fallback image as CT today.
- The morning CSV request selects **all four** Cboe exchanges: CBOE, BATS (BZX),
  C2, EDGX, daily sum, all symbols. Filter `Product Type = I` and group by exact
  `Underlying`: SPX, VIX, everything else. Do not prefix-match option roots.
- Compute each ADV as the group's MTD total divided by the same count of US
  equity trading sessions, including zero-volume days for a group. Do not divide
  by calendar days or by each group's observed row count.

## Run and report

The tool prints one JSON status. Exit 0 with `status=ok` means a new PNG was
written; return or display that PNG. Exit 0 with `status=skipped` means no applicable
session; communicate the skip without presenting an old PNG as current. Exit 1
with `status=error` means source data is unavailable or incomplete; report the
error and do not claim success or fabricate missing volume.

Each successful run replaces its fixed filename atomically. Downloads stay in
memory, runtime temporary files are cleaned up, and no raw data or logs are
persisted. A failed/skipped run preserves any previous PNG. Do not rename reports
with timestamps or leave scratch files behind. The default output directory is
the current working directory; pass an explicit output directory for schedulers.

`--date YYYY-MM-DD` overrides the date for reproducibility. For `index-mtd`, its
cutoff remains that date minus one day. For `zero-dte`, it requests exactly that
date's expiration without automatic fallback, but cannot recover historical
option snapshots from the live endpoint. Run default `zero-dte` without `--date`
when latest-completed-session selection is wanted.
Cboe can roll over or remove expired quotes; if the selected session's contracts
are missing, report the limitation rather than switching to the next expiration.
