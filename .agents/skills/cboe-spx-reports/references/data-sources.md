# Public Cboe sources and report semantics

Verified against the public responses on October 9, 2026 (America/Chicago).
These are the endpoints used by Cboe's public web pages, not a versioned API
contract. Schema changes must fail visibly rather than silently alter the result.

## Closing 0DTE report

[SPX delayed quotes](https://www.cboe.com/delayed_quotes/spx/quote_table/)
uses these public JSON resources:

- Options: `https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json`
- Intraday: `https://cdn.cboe.com/api/global/delayed_quotes/charts/intraday/_SPX.json`
- Daily OHLC: `https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_SPX.json`

All have a `data` property. Option `data` is an object with `options` and the
underlying `last_trade_time`. Each option has an OSI-style `option` string and
numeric `volume`. For example, `SPXW261008P07765000` is the October 8, 2026
PM-settled put at 7765.000; divide the last eight digits by 1000. Require an
expiration matching the selected session date and root SPXW. Standard SPX is
AM-settled and is not that day's closing 0DTE series. See the [SPX specifications](https://www.cboe.com/tradable_products/sp_500/spx_options/specifications/).

Intraday `data` is an array of `datetime` and nested `price` objects with
`open`, `high`, `low`, `close`. Offset-free chart and last-trade times are ET.
The top-level snapshot `timestamp` is displayed only as source metadata; it
does not determine the expiration date. Regular-session minute records in the
verified response started at 09:31 ET and ended at 15:59 ET. Aggregate into
five-minute wall-clock buckets (09:30, 09:35, etc.), using first open, maximum
high, minimum low, last close. Require coverage to within one minute of the
session boundaries and reject internal missing minutes. Use the daily OHLC
`close` for strike selection and the closing reference line. If the selected
day is not yet in the historical series, use the SPX index closing quote's
`close` after validating its same-day `last_trade_time` reaches cash close,
its symbol is `^SPX`, security type is `index`, and its OHLC is valid. The
quote's `close` is distinct from `prev_day_close`. The last displayed minute
is not guaranteed to contain the official closing index print.

Daily `data` is an array of `date` (YYYY-MM-DD) and string-valued OHLC. Validate
selected records; old historical records may have a zero open and are irrelevant
to the requested month. The MTD report requires every requested day to be
published. The 0DTE report can use the validated same-day closing quote when
the historical daily series lags; JSON `close_source` identifies the source.

## Index option month-to-date report

[Historical Options Data Download](https://www.cboe.com/us/options/market_statistics/historical_data/)
provides the all-symbol monthly daily-volume CSV. Its download URL is:

```text
https://www.cboe.com/us/options/market_statistics/historical_data/download/all_symbols/?reportType=volume&month=10&year=2026&volumeType=sum&volumeAggType=daily&exchanges=CBOE&exchanges=BATS&exchanges=C2&exchanges=EDGX
```

Replace month and year with those of CT yesterday. Repeat the `exchanges`
parameter four times; BATS is the identifier used by the public form for BZX.
The response is plain CSV with these columns:

```text
Trade Date,Options Class,Underlying,Product Type,Exchange,Volume
2026/10/07,SPXW,SPX,I,CBOE,4408022
2026/10/07,VIXW,VIX,I,CBOE,86061
```

The report may contain dates after the requested cutoff when replaying a run.
Filter to the target month's expected sessions through yesterday before summing.
The daily count is the count of these sessions, not the number of CSV records.
Group all `I` rows by underlying, including adjusted classes (such as `2SPX`)
when their underlying is SPX. XSP, SPESG, and other index underlyings are Other.
Each row is one class/exchange/day; reject duplicates before aggregating.

Validate publication for every expected day and exchange using the full CSV,
including non-index rows: an exchange may legitimately have no index trades.
Missing index class/group rows on an otherwise published day count as zero.
Require at least some index records on each day, and reject a day missing a whole
exchange's report. This catches missing daily files but cannot detect every
possible omission of an individual class in an upstream report.

## Calendar and freshness

Use `exchange_calendars` XNYS for US equity regular-session holidays, early closes,
and the SPX cash-session window. See its [session API](https://github.com/gerrymanoim/exchange_calendars).
NYSE is the cash-session calendar proxy here, not a calendar for Cboe overnight
or curb sessions. Cboe index option daily CSV volume includes the sessions Cboe
includes in its daily totals. Do not filter those totals to cash RTH.

The evening report requires the underlying quote's ET last-trade date to match
the requested day and to reach the cash close. It requires nonzero combined
volume across selected strikes, complete call/put pairs, and a validated closing
price from daily OHLC or the same-day SPX closing quote. Without `--date`,
choose the latest cash session whose close plus 20
minutes has passed. Before today's publication window, on holidays/weekends,
or just after CT midnight, use the preceding completed session rather than
rejecting the run on a time gate. At 00:03 CT on October 9, 2026 this selects
October 8; at 20:00 CT on October 9 it selects October 9. An explicit `--date`
requests an exact session and disables automatic date fallback.
Expired chains are retained only while Cboe provides them; no
historical recovery or persistent snapshot store is included.

The daily volume and MTD ADV bars occupy the same figure row and share a
Matplotlib y-axis. Their limits, ticks, pixel scale, and zero baseline match.
Each daily stack has an exact, comma-separated total of SPX + VIX + Other
contracts above it. Dense months tilt these labels to keep them separate.
The common upper limit includes all daily totals and the ADV total, with
headroom for labels; numeric ADV details occupy the upper-right summary panel.

For a morning run on November 1, the report targets October through October 31.
For a run on October 2, it targets October through October 1. Weekends and
holidays remain the calendar cutoff but do not add days to the ADV denominator.
If that month's cutoff precedes its first trading day, return a clean skip.
