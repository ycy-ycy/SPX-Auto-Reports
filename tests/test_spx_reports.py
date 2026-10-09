"""Offline checks for data selection, aggregation, publication, and outputs."""

import csv
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
SCRIPT = (Path(__file__).resolve().parents[1] / ".agents" / "skills"
          / "cboe-spx-reports" / "scripts" / "spx_reports.py")
spec = importlib.util.spec_from_file_location("spx_reports", SCRIPT)
reports = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = reports
spec.loader.exec_module(reports)


def csv_report(rows):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["Trade Date", "Options Class", "Underlying", "Product Type", "Exchange", "Volume"])
    writer.writerows(rows)
    return stream.getvalue()


def published_rows(day):
    return [[day.isoformat(), "AAPL", "AAPL", "S", exchange, 1] for exchange in reports.EXCHANGES]


class DataTests(unittest.TestCase):
    day = date(2026, 10, 8)

    def test_numeric_rejects_bad_volume(self):
        for value in (-1, "nan", "inf", None, 2.5):
            with self.subTest(value=value), self.assertRaises(reports.DataError):
                reports.number(value, "volume", volume=True)
        self.assertEqual(reports.number("123.0", "volume", volume=True), 123)

    def test_exact_expiry_pm_only_and_nearest_tie(self):
        options = []
        for strike in (95, 100, 105, 110):
            for side in "CP":
                options.append({"option": f"SPXW261008{side}{strike * 1000:08d}", "volume": strike})
        options += [{"option": "SPX261008C00100000", "volume": 9999},
                    {"option": "SPXW261009C00100000", "volume": 9999}]
        payload = {"data": {"options": options, "last_trade_time": "2026-10-08T16:14:59"}}
        close = datetime(2026, 10, 8, 20, tzinfo=timezone.utc)
        actual = reports.zero_dte_volumes(payload, self.day, 102.5, 2, close)
        self.assertEqual(actual, [(100, 100, 100), (105, 105, 105)])
        with self.assertRaises(reports.DataError):
            reports.zero_dte_volumes(payload, self.day, 100, 5, close)
        payload["data"]["last_trade_time"] = "2026-10-07T16:14:59"
        with self.assertRaises(reports.DataError):
            reports.zero_dte_volumes(payload, self.day, 100, 2, close)

    def test_missing_pair_and_rollover_fail(self):
        close = datetime(2026, 10, 8, 20, tzinfo=timezone.utc)
        payload = {"data": {"last_trade_time": "2026-10-08T16:14:59", "options": [
            {"option": "SPXW261008C00100000", "volume": 10}]}}
        with self.assertRaises(reports.DataError):
            reports.zero_dte_volumes(payload, self.day, 100, 1, close)
        payload["data"]["options"] = [{"option": f"SPXW261008{s}00100000", "volume": 0} for s in "CP"]
        with self.assertRaises(reports.DataError):
            reports.zero_dte_volumes(payload, self.day, 100, 1, close)

    def test_monthly_exact_filter_and_underlying_merging(self):
        rows = published_rows(self.day) + [
            ["2026/10/08", "SPX", "SPX", "I", "CBOE", 100],
            ["2026/10/08", "SPXW", "SPX", "I", "CBOE", 50],
            ["2026/10/08", "2SPX", "SPX", "I", "C2", 10],
            ["2026/10/08", "VIXW", "VIX", "I", "CBOE", 30],
            ["2026/10/08", "XSP", "XSP", "I", "BATS", 5],
            ["2026/10/08", "SPESG", "SPESG", "I", "EDGX", 7],
            ["2026/10/08", "SPXL", "SPXL", "S", "EDGX", 9999],
            ["2026/10/09", "SPX", "SPX", "I", "CBOE", 9999],
        ]
        self.assertEqual(reports.monthly_volumes(csv_report(rows), [self.day]), [(160, 30, 12)])

    def test_missing_exchange_duplicate_and_bad_schema_fail(self):
        rows = published_rows(self.day) + [[str(self.day), "SPX", "SPX", "I", "CBOE", 100]]
        for bad in (rows[:1] + rows[2:], rows + [rows[-1]]):
            with self.assertRaises(reports.DataError):
                reports.monthly_volumes(csv_report(bad), [self.day])
        with self.assertRaises(reports.DataError):
            reports.monthly_volumes("<html>Not a CSV</html>", [self.day])
        with self.assertRaises(reports.DataError):
            reports.monthly_volumes(csv_report(published_rows(self.day)), [self.day])

    def test_zero_group_is_counted_as_zero_on_published_day(self):
        rows = published_rows(self.day) + [[str(self.day), "SPX", "SPX", "I", "CBOE", 100]]
        self.assertEqual(reports.monthly_volumes(csv_report(rows), [self.day]), [(100, 0, 0)])

    def test_five_minute_ohlc_and_missing_minute(self):
        start = reports.eastern_time("2026-10-08T09:30:00")
        end = start + timedelta(minutes=10)
        rows = [{"datetime": (start + timedelta(minutes=i)).isoformat(),
                 "price": {"open": 100 + i, "high": 102 + i, "low": 99 + i, "close": 101 + i}}
                for i in range(10)]
        # Extra out-of-session rows must not affect OHLC.
        rows.append({"datetime": (start - timedelta(minutes=1)).isoformat(), "price": {}})
        actual = reports.five_minute_candles({"data": rows}, self.day, start, end)
        self.assertEqual(len(actual), 2)
        self.assertEqual((actual[0].open, actual[0].high, actual[0].low, actual[0].close), (100, 106, 99, 105))
        self.assertEqual(actual[1].when.minute, 35)
        with self.assertRaises(reports.DataError):
            reports.five_minute_candles({"data": rows[:3] + rows[4:]}, self.day, start, end)

    def test_daily_candles_require_every_session(self):
        payload = {"data": [{"date": str(self.day), "open": "100", "high": "110", "low": "95", "close": "105"}]}
        self.assertEqual(reports.daily_candles(payload, [self.day])[0].close, 105)
        with self.assertRaises(reports.DataError):
            reports.daily_candles(payload, [self.day, self.day + timedelta(days=1)])


class ReportTests(unittest.TestCase):
    def test_default_date_uses_ct_at_utc_month_boundary(self):
        with patch.object(reports, "datetime") as clock:
            clock.now.return_value = datetime(2026, 2, 1, 2, tzinfo=timezone.utc)
            with patch.object(reports, "index_mtd_report", return_value={"status": "ok"}) as report:
                with patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(reports.main(["index-mtd"]), 0)
            self.assertEqual(report.call_args.args[1], date(2026, 1, 31))

    def test_skill_runs_after_copy_without_repository_wrapper(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            skill = root / "cboe-spx-reports"
            shutil.copytree(SCRIPT.parents[1], skill)
            result = subprocess.run(
                [sys.executable, "-B", str(skill / "scripts" / "spx_reports.py"),
                 "zero-dte", "--date", "2026-10-10"],
                cwd=root, capture_output=True, text=True, check=True,
            )
            self.assertIn('"status": "skipped"', result.stdout)
            self.assertEqual(list(root.iterdir()), [skill])
            self.assertFalse(list(skill.rglob("__pycache__")))

    def test_calendar_holidays_early_close_dst(self):
        cal = reports.calendar_for(date(2026, 1, 1), date(2026, 12, 31))
        self.assertFalse(cal.is_session("2026-12-25"))
        self.assertEqual(cal.session_close("2026-11-27").hour, 18)
        self.assertEqual(cal.session_close("2026-07-01").hour, 20)
        self.assertEqual(cal.session_close("2026-12-01").hour, 21)

    def test_month_boundary_prior_month_and_common_adv_denominator(self):
        client = Mock()
        sessions = [s.date() for s in reports.calendar_for(date(2026, 10, 1), date(2026, 10, 31)).sessions_in_range("2026-10-01", "2026-10-31")]
        rows = []
        history = []
        for day in sessions:
            history.append({"date": str(day), "open": 100, "high": 110, "low": 95, "close": 105})
            rows += published_rows(day) + [[str(day), "SPXW", "SPX", "I", "CBOE", 100]]
        rows.append([str(sessions[0]), "VIXW", "VIX", "I", "CBOE", 44])
        client.json.return_value = {"data": history}
        client.month_csv.return_value = csv_report(rows)
        with patch.object(reports, "plot_index_mtd") as plot:
            result = reports.index_mtd_report(client, date(2026, 11, 1), Path("unused"))
        client.month_csv.assert_called_once_with(date(2026, 10, 31))
        self.assertEqual(result["month"], "2026-10")
        self.assertEqual(result["trading_days"], len(sessions))
        self.assertEqual(result["adv"], {"SPX": 100, "VIX": 44 / len(sessions), "Other": 0})
        self.assertEqual(result["total_adv"], 100 + 44 / len(sessions))
        self.assertEqual(plot.call_args.args[3:5], (date(2026, 10, 1), date(2026, 10, 31)))

    def test_empty_month_and_weekend_skip_without_requests(self):
        client = Mock()
        self.assertEqual(reports.index_mtd_report(client, date(2026, 8, 2), Path("unused"))["status"], "skipped")
        self.assertEqual(reports.zero_dte_report(client, date(2026, 10, 10), 10, Path("unused"))["status"], "skipped")
        client.json.assert_not_called()
        client.month_csv.assert_not_called()

    def test_default_zero_dte_selects_latest_completed_session(self):
        cases = [
            # The user's midnight CT example must still fetch October 8.
            (datetime(2026, 10, 9, 5, 3, tzinfo=timezone.utc), date(2026, 10, 8)),
            (datetime(2026, 10, 9, 19, tzinfo=timezone.utc), date(2026, 10, 8)),
            (datetime(2026, 10, 9, 20, 19, tzinfo=timezone.utc), date(2026, 10, 8)),
            (datetime(2026, 10, 9, 20, 20, tzinfo=timezone.utc), date(2026, 10, 9)),
            (datetime(2026, 10, 10, 18, tzinfo=timezone.utc), date(2026, 10, 9)),
            (datetime(2026, 10, 12, 14, tzinfo=timezone.utc), date(2026, 10, 9)),
            (datetime(2026, 12, 25, 18, tzinfo=timezone.utc), date(2026, 12, 24)),
            # Early close, in winter: 13:00 ET plus 20 minutes is 18:20 UTC.
            (datetime(2026, 11, 27, 18, 19, tzinfo=timezone.utc), date(2026, 11, 25)),
            (datetime(2026, 11, 27, 18, 20, tzinfo=timezone.utc), date(2026, 11, 27)),
        ]
        for now, expected in cases:
            with self.subTest(now=now):
                today = now.astimezone(reports.ZoneInfo("America/Chicago")).date()
                client = Mock()
                client.json.return_value = {"timestamp": "source snapshot"}
                bar = reports.Candle(expected, 100, 102, 99, 101)
                with patch.object(reports, "closing_spx_price", return_value=(bar.close, "historical_daily")) as daily:
                    with patch.object(reports, "five_minute_candles", return_value=[]) as intraday:
                        with patch.object(reports, "zero_dte_volumes", return_value=[(100, 20, 40)]) as quotes:
                            with patch.object(reports, "plot_zero_dte") as plot:
                                result = reports.zero_dte_report(client, today, 1, Path("unused"), now)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["date"], str(expected))
                self.assertEqual(result["requested_date"], str(today))
                self.assertEqual(result["used_previous_session"], expected != today)
                self.assertEqual(daily.call_args.args[2], expected)
                self.assertEqual(intraday.call_args.args[1], expected)
                self.assertEqual(quotes.call_args.args[1], expected)
                self.assertEqual(plot.call_args.args[3], expected)

    def test_default_cli_fetches_retained_chain_after_ct_midnight(self):
        day = date(2026, 10, 8)
        start = reports.eastern_time("2026-10-08T09:30:00")
        price = {"open": 100, "high": 102, "low": 99, "close": 101}
        payloads = {
            reports.HISTORY_URL: {"data": [{"date": str(day), **price}]},
            reports.INTRADAY_URL: {"data": [
                {"datetime": (start + timedelta(minutes=i)).isoformat(), "price": price}
                for i in range(390)]},
            reports.OPTIONS_URL: {"data": {"last_trade_time": "2026-10-08T16:14:59", "options": [
                {"option": f"SPXW261008{s}00100000", "volume": 10} for s in "CP"]}},
        }
        with patch.object(reports, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 9, 5, 3, tzinfo=timezone.utc)
            with patch.object(reports.CboeClient, "json", side_effect=payloads.__getitem__):
                with patch.object(reports, "plot_zero_dte") as plot:
                    with patch("sys.stdout", new_callable=io.StringIO) as output:
                        self.assertEqual(reports.main(["zero-dte", "--strikes", "1"]), 0)
        self.assertIn('"date": "2026-10-08"', output.getvalue())
        self.assertEqual(len(plot.call_args.args[0]), 78)

    def test_zero_dte_uses_same_day_closing_quote_when_history_lags(self):
        day = date(2026, 10, 9)
        start = reports.eastern_time("2026-10-09T09:30:00")
        price = {"open": 7811.68, "high": 7812.25, "low": 7811.14, "close": 7812.23}
        quote = {"symbol": "^SPX", "security_type": "index",
                 "last_trade_time": "2026-10-09T16:14:59",
                 "open": 7786.3599, "high": 7820.5698, "low": 7779.3398, "close": 7811.54,
                 "options": [{"option": f"SPXW261009{s}07810000", "volume": 10} for s in "CP"]}
        payloads = {
            reports.HISTORY_URL: {"data": [{"date": "2026-10-08", **price}]},
            reports.OPTIONS_URL: {"data": quote},
            reports.INTRADAY_URL: {"data": [
                {"datetime": (start + timedelta(minutes=i)).isoformat(), "price": price}
                for i in range(1, 390)]},
        }
        client = Mock()
        client.json.side_effect = payloads.__getitem__
        with patch.object(reports, "plot_zero_dte") as plot:
            result = reports.zero_dte_report(client, day, 1, Path("unused"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["date"], "2026-10-09")
        self.assertEqual(result["close"], 7811.54)
        self.assertEqual(result["close_source"], "closing_quote")
        self.assertEqual(len(plot.call_args.args[0]), 78)
        self.assertEqual(plot.call_args.args[2], 7811.54)
        market_close = datetime(2026, 10, 9, 20, tzinfo=timezone.utc)
        for change in ({"last_trade_time": "2026-10-08T16:14:59"},
                       {"last_trade_time": "2026-10-09T15:59:00"},
                       {"close": None}, {"close": 9000}, {"symbol": "SPY"}):
            with self.subTest(change=change), self.assertRaises(reports.DataError):
                reports.closing_spx_price(payloads[reports.HISTORY_URL],
                                          {"data": {**quote, **change}}, day, market_close)
        # A published official daily close remains preferred; invalid published
        # candles must fail rather than being hidden by the fallback.
        history = {"data": [{"date": str(day), "open": 7800, "high": 7820,
                             "low": 7790, "close": 7810}]}
        self.assertEqual(reports.closing_spx_price(history, {"data": quote}, day, market_close),
                         (7810, "historical_daily"))
        history["data"][0]["close"] = 9000
        with self.assertRaises(reports.DataError):
            reports.closing_spx_price(history, {"data": quote}, day, market_close)

    def test_adv_and_daily_volume_share_scale_and_pixel_baseline(self):
        day = date(2026, 10, 8)
        bars = [reports.Candle(day, 100, 102, 99, 101)]
        def inspect(fig, destination):
            fig.canvas.draw()
            price_ax, volume_ax, summary_ax, adv_ax = fig.axes
            self.assertTrue(volume_ax.get_shared_y_axes().joined(volume_ax, adv_ax))
            self.assertEqual(volume_ax.get_ylim(), adv_ax.get_ylim())
            self.assertGreater(volume_ax.get_ylim()[1], 131)
            for value in (0, 100, 131):
                self.assertAlmostEqual(volume_ax.transData.transform((0, value))[1],
                                       adv_ax.transData.transform((0, value))[1])
        with patch.object(reports, "save_png", side_effect=inspect):
            reports.plot_index_mtd(bars, [(100, 30, 1)], (100, 30, 1), day, day, Path("unused"))

    def test_errors_preserve_previous_report(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / reports.OUTPUTS["index-mtd"]
            path.write_bytes(b"previous report")
            with patch.object(reports.CboeClient, "json", side_effect=reports.DataError("Unavailable")):
                with patch("sys.stderr", new_callable=io.StringIO):
                    code = reports.main(["index-mtd", "--date", "2026-10-08", "--output-dir", folder])
            self.assertEqual(code, 1)
            self.assertEqual(path.read_bytes(), b"previous report")
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_atomic_replace_failure_cleans_temp(self):
        fig = Mock()
        fig.savefig.side_effect = lambda buffer, **kwargs: buffer.write(b"new png")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "spx_0dte.png"
            path.write_bytes(b"previous")
            with patch.object(reports.os, "replace", side_effect=OSError("Locked")):
                with self.assertRaises(OSError):
                    reports.save_png(fig, path)
            self.assertEqual(path.read_bytes(), b"previous")
            self.assertEqual(list(Path(folder).iterdir()), [path])

    def test_plots_are_readable_pngs_and_overwrite_without_residue(self):
        from PIL import Image
        day = date(2026, 10, 8)
        bars = [reports.Candle(reports.eastern_time("2026-10-08T09:30:00"), 100, 102, 99, 101),
                reports.Candle(reports.eastern_time("2026-10-08T09:35:00"), 101, 102, 98, 99)]
        daily = [reports.Candle(day, 100, 102, 99, 101)]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            path = output / reports.OUTPUTS["zero-dte"]
            path.write_bytes(b"old")
            reports.plot_zero_dte(bars, [(100.0, 20, 40), (105.0, 50, 10)], 101, day, path)
            other = output / reports.OUTPUTS["index-mtd"]
            reports.plot_index_mtd(daily, [(100, 30, 1)], (100, 30, 1), day, day, other)
            self.assertEqual({p.name for p in output.iterdir()}, set(reports.OUTPUTS.values()))
            for png in (path, other):
                with Image.open(png) as img:
                    self.assertEqual(img.format, "PNG")
                    self.assertGreater(img.width, 1500)
                    self.assertGreater(img.height, 800)
                    self.assertGreater(len(img.convert("RGB").getcolors(img.width * img.height)), 100)


if __name__ == "__main__":
    unittest.main()
