"""Fetch public Cboe data and render two repeatable, scheduler-friendly reports."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True

CDN = "https://cdn.cboe.com/api/global/delayed_quotes"
OPTIONS_URL = f"{CDN}/options/_SPX.json"
INTRADAY_URL = f"{CDN}/charts/intraday/_SPX.json"
HISTORY_URL = f"{CDN}/charts/historical/_SPX.json"
VOLUME_URL = (
    "https://www.cboe.com/us/options/market_statistics/"
    "historical_data/download/all_symbols/"
)
EXCHANGES = ("CBOE", "BATS", "C2", "EDGX")
GROUPS = ("SPX", "VIX", "Other")
COLORS = ("#2563eb", "#f59e0b", "#8b5cf6")
UP, DOWN = "#16a34a", "#dc2626"
OUTPUTS = {"zero-dte": "spx_0dte.png", "index-mtd": "cboe_index_mtd.png"}
OPTION = re.compile(r"^(SPXW|SPX)(\d{6})([CP])(\d{8})$")


class DataError(ValueError):
    """Unavailable, incomplete, or invalid source data; never fabricate a report."""


@dataclass(frozen=True)
class Candle:
    when: datetime | date
    open: float
    high: float
    low: float
    close: float


def number(value, label: str, *, volume: bool = False) -> float | int:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise DataError(f"Invalid {label}: {value!r}") from exc
    if not math.isfinite(result) or result < 0 or (volume and not result.is_integer()):
        raise DataError(f"Invalid {label}: {value!r}")
    return int(result) if volume else result


def candle(when, record) -> Candle:
    values = [number(record.get(k), k) for k in ("open", "high", "low", "close")]
    o, h, low, c = values
    if min(values) <= 0 or not low <= min(o, c) <= max(o, c) <= h:
        raise DataError(f"Invalid OHLC at {when}")
    return Candle(when, o, h, low, c)


def eastern_time(value: str) -> datetime:
    """Cboe chart/last-trade timestamps without an offset are Eastern time."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
    return dt.astimezone(ZoneInfo("America/New_York"))


def calendar_for(start: date, end: date):
    import exchange_calendars as xcals

    # SPX cash-index RTH and US options business days follow the US equity calendar.
    return xcals.get_calendar(
        "XNYS", start=(start - timedelta(days=7)).isoformat(),
        end=(end + timedelta(days=7)).isoformat()
    )


class CboeClient:
    def __init__(self, timeout: float = 30, attempts: int = 3):
        self.timeout = timeout
        self.attempts = attempts

    def read(self, url: str) -> bytes:
        request = Request(url, headers={"User-Agent": "SPXReports/1.0", "Accept": "*/*"})
        for attempt in range(self.attempts):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                retryable = not isinstance(exc, HTTPError) or exc.code in (
                    408, 429, 500, 502, 503, 504
                )
                if not retryable or attempt + 1 == self.attempts:
                    raise DataError(f"Cboe request failed: {url}: {exc}") from exc
                time.sleep(2 ** attempt)
        raise AssertionError("Unreachable")

    def json(self, url: str) -> dict:
        try:
            result = json.loads(self.read(url))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataError(f"Cboe returned invalid JSON: {url}") from exc
        if not isinstance(result, dict) or "data" not in result:
            raise DataError(f"Cboe JSON schema changed: {url}")
        return result

    def month_csv(self, day: date) -> str:
        params = [
            ("reportType", "volume"), ("month", str(day.month)),
            ("year", str(day.year)), ("volumeType", "sum"),
            ("volumeAggType", "daily"),
        ] + [("exchanges", exchange) for exchange in EXCHANGES]
        try:
            return self.read(VOLUME_URL + "?" + urlencode(params)).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DataError("Cboe returned invalid CSV encoding") from exc


def daily_candles(payload: dict, sessions: list[date]) -> list[Candle]:
    wanted = set(sessions)
    found = {}
    for row in payload["data"]:
        day = date.fromisoformat(row["date"])
        if day in wanted:
            if day in found:
                raise DataError(f"Duplicate daily candle: {day}")
            found[day] = candle(day, row)
    missing = wanted - found.keys()
    if missing:
        raise DataError(f"Daily SPX candles not published for {sorted(missing)}")
    return [found[day] for day in sessions]


def five_minute_candles(payload: dict, day: date, market_open: datetime,
                        market_close: datetime) -> list[Candle]:
    minutes = {}
    for row in payload["data"]:
        dt = eastern_time(row["datetime"])
        if dt.date() == day and market_open <= dt < market_close:
            if dt in minutes:
                raise DataError(f"Duplicate intraday candle: {dt}")
            minutes[dt] = candle(dt, row["price"])
    ordered = sorted(minutes)
    if (not ordered or ordered[0] > market_open + timedelta(minutes=1)
            or ordered[-1] < market_close - timedelta(minutes=1)):
        raise DataError(f"Full closing SPX intraday data not available for {day}")
    if any(b - a > timedelta(minutes=1) for a, b in zip(ordered, ordered[1:])):
        raise DataError(f"Missing intraday minutes for {day}")
    buckets = {}
    for dt in ordered:
        bucket = dt.replace(minute=dt.minute // 5 * 5, second=0, microsecond=0)
        buckets.setdefault(bucket, []).append(minutes[dt])
    return [Candle(dt, bars[0].open, max(b.high for b in bars),
                   min(b.low for b in bars), bars[-1].close)
            for dt, bars in sorted(buckets.items())]


def closing_quote_data(payload: dict, day: date, market_close: datetime) -> dict:
    data = payload["data"]
    last = data.get("last_trade_time")
    if not last or eastern_time(last).date() != day or eastern_time(last) < market_close:
        raise DataError(f"Closing quote snapshot not available for {day}")
    return data


def closing_spx_price(history: dict, quotes: dict, day: date,
                      market_close: datetime) -> tuple[float, str]:
    if any(date.fromisoformat(row["date"]) == day for row in history["data"]):
        return daily_candles(history, [day])[0].close, "historical_daily"
    # The historical series can lag after the close while the same-day quote
    # already contains Cboe's closing OHLC. Never use the final minute or a
    # previous day's price as a substitute for the official closing index print.
    data = closing_quote_data(quotes, day, market_close)
    if data.get("symbol") != "^SPX" or data.get("security_type") != "index":
        raise DataError("Closing quote is not the SPX index")
    return candle(day, data).close, "closing_quote"


def zero_dte_volumes(payload: dict, day: date, close: float, count: int,
                     market_close: datetime) -> list[tuple[float, int, int]]:
    data = closing_quote_data(payload, day, market_close)
    pairs = {}
    seen = set()
    for row in data["options"]:
        match = OPTION.fullmatch(row["option"].strip())
        if not match:
            continue
        root, expiry, side, strike_text = match.groups()
        # Standard SPX is AM-settled and ceased trading on the preceding day.
        if root != "SPXW" or expiry != day.strftime("%y%m%d"):
            continue
        symbol = row["option"]
        if symbol in seen:
            raise DataError(f"Duplicate option: {symbol}")
        seen.add(symbol)
        strike = int(strike_text) / 1000
        pairs.setdefault(strike, {})[side] = number(row.get("volume"), symbol, volume=True)
    if len(pairs) < count:
        raise DataError(
            f"Only {len(pairs)} same-day SPXW strikes available for {day}; need {count}. "
            "Cboe may have removed expired contracts; live quotes cannot recover history."
        )
    nearest = sorted(pairs, key=lambda strike: (abs(strike - close), strike))[:count]
    result = []
    for strike in sorted(nearest):
        if set(pairs[strike]) != {"C", "P"}:
            raise DataError(f"Missing put/call quote at strike {strike}")
        result.append((strike, pairs[strike]["P"], pairs[strike]["C"]))
    if not any(put + call for _, put, call in result):
        raise DataError("Selected 0DTE volumes are all zero; snapshot may have rolled over")
    return result


def monthly_volumes(text: str, sessions: list[date]) -> list[tuple[int, int, int]]:
    reader = csv.DictReader(io.StringIO(text))
    required = {"Trade Date", "Options Class", "Underlying", "Product Type", "Exchange", "Volume"}
    if not required.issubset(reader.fieldnames or []):
        raise DataError(f"Cboe CSV schema changed: {reader.fieldnames}")
    wanted = set(sessions)
    totals = {day: [0, 0, 0] for day in sessions}
    coverage = {day: set() for day in sessions}
    index_days = set()
    seen = set()
    for row in reader:
        day = date.fromisoformat(row["Trade Date"].replace("/", "-"))
        if day not in wanted:
            continue
        exchange = row["Exchange"].strip().upper()
        if exchange not in EXCHANGES:
            raise DataError(f"Unexpected exchange: {exchange}")
        key = (day, exchange, row["Options Class"])
        if key in seen:
            raise DataError(f"Duplicate CSV volume row: {key}")
        seen.add(key)
        volume = number(row["Volume"], "daily volume", volume=True)
        # Use all product types for per-exchange publication checks. Some exchanges
        # have no index prints; absence of an index class itself means zero.
        coverage[day].add(exchange)
        if row["Product Type"].strip() != "I":
            continue
        index_days.add(day)
        underlying = row["Underlying"].strip().upper()
        group = 0 if underlying == "SPX" else 1 if underlying == "VIX" else 2
        totals[day][group] += volume
    for day in sessions:
        missing = set(EXCHANGES) - coverage[day]
        if missing or day not in index_days:
            raise DataError(f"Incomplete Cboe daily volume for {day}; missing exchanges: {sorted(missing)}")
    return [tuple(totals[day]) for day in sessions]


@contextmanager
def plotting():
    # Keep Matplotlib's font cache out of the repository and remove it on exit.
    previous = os.environ.get("MPLCONFIGDIR")
    with tempfile.TemporaryDirectory(prefix="spx-mpl-") as config:
        os.environ["MPLCONFIGDIR"] = config
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            with plt.rc_context({
                "font.family": "DejaVu Sans", "font.size": 10,
                "axes.spines.top": False, "axes.spines.right": False,
                "axes.titleweight": "bold", "figure.facecolor": "white",
                "axes.facecolor": "white", "savefig.facecolor": "white",
            }):
                yield plt
        finally:
            if previous is None:
                os.environ.pop("MPLCONFIGDIR", None)
            else:
                os.environ["MPLCONFIGDIR"] = previous


def draw_candles(ax, bars: list[Candle]):
    from matplotlib.patches import Rectangle
    for x, bar in enumerate(bars):
        color = UP if bar.close >= bar.open else DOWN
        ax.vlines(x, bar.low, bar.high, color=color, linewidth=1)
        body = abs(bar.close - bar.open)
        if body:
            ax.add_patch(Rectangle((x - .32, min(bar.open, bar.close)), .64,
                                   body, facecolor=color, edgecolor=color))
        else:
            ax.hlines(bar.close, x - .32, x + .32, color=color, linewidth=1.3)
    ax.set_xlim(-1, len(bars))
    ax.autoscale_view(scalex=False)
    ax.set_ylabel("SPX")
    ax.grid(axis="y", alpha=.16)
    ax.set_axisbelow(True)


def save_png(fig, destination: Path):
    # Render completely before touching the previous report; temporary output is
    # removed even if replacement fails. Downloads exist only in memory.
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=160, bbox_inches="tight")
    destination.parent.mkdir(parents=True, exist_ok=True)
    path = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".tmp", delete=False) as f:
            path = Path(f.name)
            f.write(buffer.getvalue())
        os.replace(path, destination)
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def plot_zero_dte(bars, volumes, close, day, destination):
    with plotting() as plt:
        from matplotlib.ticker import FuncFormatter
        fig, (price_ax, volume_ax) = plt.subplots(1, 2, figsize=(15, 7),
                                                gridspec_kw={"width_ratios": [1.5, 1]},
                                                layout="constrained")
        try:
            draw_candles(price_ax, bars)
            ticks = list(range(0, len(bars), max(1, len(bars) // 7)))
            price_ax.set_xticks(ticks, [bars[i].when.astimezone(ZoneInfo("America/Chicago")).strftime("%H:%M") for i in ticks])
            price_ax.set_xlabel("America/Chicago (CT)")
            price_ax.set_title("SPX regular session | 5-minute candles")
            price_ax.axhline(close, color="#64748b", ls="--", lw=.9)
            price_ax.text(.02, .97, f"Close: {close:,.2f}", transform=price_ax.transAxes,
                          va="top", bbox={"facecolor": "white", "alpha": .85, "edgecolor": "none"})
            strikes, puts, calls = zip(*volumes)
            y = list(range(len(strikes)))
            volume_ax.barh(y, [-v for v in puts], color=DOWN, height=.68, label="Put")
            volume_ax.barh(y, calls, color=UP, height=.68, label="Call")
            maximum = max(max(puts), max(calls), 1)
            extent = maximum * 1.3
            volume_ax.set_xlim(-extent, extent)
            volume_ax.axvline(0, color="#334155", lw=1)
            volume_ax.set_yticks(y, [f"{s:,.0f}" if s.is_integer() else f"{s:,.3f}" for s in strikes])
            volume_ax.set_ylabel("Strike")
            volume_ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{abs(v) / 1000:g}k"))
            volume_ax.set_xlabel("Put  <---  contracts  --->  Call")
            volume_ax.set_title(f"0DTE SPXW | {len(strikes)} nearest strikes")
            volume_ax.legend(loc="upper right", frameon=False)
            volume_ax.margins(y=.15)
            for i, (put, call) in enumerate(zip(puts, calls)):
                volume_ax.text(-put - maximum * .02, i, f"{put:,}", ha="right", va="center", fontsize=9)
                volume_ax.text(call + maximum * .02, i, f"{call:,}", ha="left", va="center", fontsize=9)
            fig.suptitle(f"SPX closing 0DTE volume | {day.isoformat()}", fontsize=17, weight="bold")
            fig.supxlabel("Source: Cboe delayed quotes | Volume is cumulative contracts for the displayed session; PM-settled SPXW only", fontsize=9)
            save_png(fig, destination)
        finally:
            plt.close(fig)


def plot_index_mtd(bars, volumes, adv, start, cutoff, destination):
    with plotting() as plt:
        from matplotlib.ticker import FuncFormatter
        fig = plt.figure(figsize=(max(15, len(bars) * .55 + 3), 9), layout="constrained")
        grid = fig.add_gridspec(2, 2, width_ratios=[3.5, 1.3], height_ratios=[2.4, 2])
        price_ax = fig.add_subplot(grid[0, 0])
        volume_ax = fig.add_subplot(grid[1, 0], sharex=price_ax)
        summary_ax = fig.add_subplot(grid[0, 1])
        adv_ax = fig.add_subplot(grid[1, 1], sharey=volume_ax)
        try:
            draw_candles(price_ax, bars)
            price_ax.set_title("SPX | Daily candles")
            price_ax.tick_params(labelbottom=False)
            x = list(range(len(bars)))
            bottom = [0] * len(bars)
            for group, color, col in zip(GROUPS, COLORS, range(3)):
                heights = [v[col] for v in volumes]
                volume_ax.bar(x, heights, bottom=bottom, width=.64, color=color, label=group)
                bottom = [a + b for a, b in zip(bottom, heights)]
            # Full contract counts stay legible in a complete month: slanted
            # labels fit one trading-day column, with extra height above stacks.
            dense = len(bars) > 12
            for i, daily_total in enumerate(bottom):
                volume_ax.annotate(
                    f"{daily_total:,}", (i, daily_total), xytext=(0, 5),
                    textcoords="offset points", fontsize=9, color="#334155",
                    ha="left" if dense else "center", va="bottom",
                    rotation=65 if dense else 0, rotation_mode="anchor",
                )
            step = max(1, math.ceil(len(bars) / 12))
            ticks = x[::step]
            volume_ax.set_xticks(ticks, [bars[i].when.strftime("%b %d") for i in ticks])
            volume_ax.set_ylabel("Contracts / day")
            volume_ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v / 1e6:g}M"))
            volume_ax.grid(axis="y", alpha=.16)
            volume_ax.set_axisbelow(True)
            volume_ax.legend(ncol=3, frameon=False, loc="lower left",
                             bbox_to_anchor=(0, 1.02), borderaxespad=0)
            total = sum(adv)
            volume_ax.set_ylim(0, max(1, total, *bottom) * 1.35)
            bottom = 0
            for group, color, value in zip(GROUPS, COLORS, adv):
                adv_ax.bar(0, value, bottom=bottom, width=.45, color=color)
                bottom += value
            adv_ax.set_xlim(-.65, .65)
            adv_ax.set_xticks([0], ["MTD ADV"])
            adv_ax.tick_params(axis="y", labelleft=False)
            adv_ax.set_title(f"MTD ADV | {len(bars)} trading days")
            adv_ax.text(0, total * 1.025, f"Total: {total:,.0f}", ha="center", weight="bold", fontsize=12)
            # Keep the ADV bar on the same row and shared y-axis as daily volume;
            # the upper-right panel holds numbers without stretching the bar.
            summary_ax.set_axis_off()
            summary_ax.set_title("MTD average daily volume")
            for i, (group, color, value) in enumerate(zip(GROUPS, COLORS, adv)):
                summary_ax.text(.05, .85 - i * .15, f"{group}: {value:,.0f}", color=color,
                                transform=summary_ax.transAxes, weight="bold", fontsize=13)
            summary_ax.text(.05, .35, f"Total: {total:,.0f}", transform=summary_ax.transAxes,
                            weight="bold", fontsize=13)
            summary_ax.text(.05, .2, f"Contracts / trading day\n{len(bars)} completed trading days",
                            transform=summary_ax.transAxes, color="#64748b", fontsize=11)
            adv_ax.grid(axis="y", alpha=.16)
            adv_ax.set_axisbelow(True)
            fig.suptitle(f"Cboe index option volume | {start:%B %Y} through {cutoff.isoformat()}",
                         fontsize=17, weight="bold")
            fig.supxlabel("Source: Cboe | CBOE + BATS (BZX) + C2 + EDGX | Product Type = I | SPX / VIX grouped by underlying", fontsize=9)
            save_png(fig, destination)
        finally:
            plt.close(fig)


def zero_dte_report(client, day, count, output_dir, now=None):
    requested_day = day
    cal = calendar_for(day - timedelta(days=14), day)
    if now is not None:
        session = cal.date_to_session(day.isoformat(), direction="previous")
        if now < cal.session_close(session).to_pydatetime() + timedelta(minutes=20):
            session = cal.previous_session(session)
        day = session.date()
    if not cal.is_session(day.isoformat()):
        return {"status": "skipped", "reason": "No SPX regular session", "date": day.isoformat()}
    market_open = cal.session_open(day.isoformat()).to_pydatetime()
    market_close = cal.session_close(day.isoformat()).to_pydatetime()
    history = client.json(HISTORY_URL)
    quotes = client.json(OPTIONS_URL)
    close, close_source = closing_spx_price(history, quotes, day, market_close)
    bars = five_minute_candles(client.json(INTRADAY_URL), day, market_open, market_close)
    volumes = zero_dte_volumes(quotes, day, close, count, market_close)
    destination = output_dir / OUTPUTS["zero-dte"]
    plot_zero_dte(bars, volumes, close, day, destination)
    return {"status": "ok", "date": day.isoformat(), "output": str(destination.resolve()),
            "requested_date": requested_day.isoformat(), "used_previous_session": day != requested_day,
            "close": close, "close_source": close_source,
            "strikes": len(volumes), "quote_timestamp": quotes.get("timestamp"),
            "put_volume": sum(v[1] for v in volumes), "call_volume": sum(v[2] for v in volumes)}


def index_mtd_report(client, today, output_dir):
    cutoff = today - timedelta(days=1)
    start = cutoff.replace(day=1)
    cal = calendar_for(start, cutoff)
    sessions = [s.date() for s in cal.sessions_in_range(start.isoformat(), cutoff.isoformat())]
    if not sessions:
        return {"status": "skipped", "reason": "No completed trading days in target month",
                "through": cutoff.isoformat()}
    bars = daily_candles(client.json(HISTORY_URL), sessions)
    volumes = monthly_volumes(client.month_csv(cutoff), sessions)
    adv = tuple(sum(v[i] for v in volumes) / len(sessions) for i in range(3))
    destination = output_dir / OUTPUTS["index-mtd"]
    plot_index_mtd(bars, volumes, adv, start, cutoff, destination)
    return {"status": "ok", "through": cutoff.isoformat(), "month": start.strftime("%Y-%m"),
            "output": str(destination.resolve()), "trading_days": len(sessions),
            "adv": dict(zip(GROUPS, adv)), "total_adv": sum(adv)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", choices=OUTPUTS)
    parser.add_argument("--date", type=date.fromisoformat,
                        help="Exact 0DTE session or MTD run date (YYYY-MM-DD); default 0DTE uses latest completed session")
    parser.add_argument("--strikes", type=int, default=10, help="Nearest strikes, default 10 (zero-dte only)")
    parser.add_argument("--output-dir", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds")
    args = parser.parse_args(argv)
    if args.strikes < 1 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--strikes and --timeout must be positive")
    try:
        now = datetime.now(timezone.utc)
        today = args.date or now.astimezone(ZoneInfo("America/Chicago")).date()
        client = CboeClient(timeout=args.timeout)
        if args.report == "zero-dte":
            result = zero_dte_report(client, today, args.strikes, args.output_dir,
                                     now if args.date is None else None)
        else:
            result = index_mtd_report(client, today, args.output_dir)
        print(json.dumps(result))
        return 0
    except (DataError, ImportError, KeyError, TypeError, ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "report": args.report, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
