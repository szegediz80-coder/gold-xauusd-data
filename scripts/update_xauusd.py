#!/usr/bin/env python3
"""XAU/USD ASK H1 and UTC D1, rolling 3 years, resumable backfill.

A GitHub Actions run downloads only a few monthly files, then saves progress.
HTTP 429/5xx network errors don't erase previously fetched months.
The status JSON explicitly says when the historical backfill is incomplete.
"""
from __future__ import annotations

import csv
import json
import lzma
import struct
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data"
H1_FILE = OUT / "XAUUSD_H1_ASK_UTC.csv"
D1_FILE = OUT / "XAUUSD_D1_ASK_UTC.csv"
STATUS_FILE = OUT / "STATUS.json"
BASE = "https://datafeed.dukascopy.com/datafeed/XAUUSD"
RECORD = struct.Struct(">IIIIIf")
PRICE_DIVISOR = 1000
# One current-month probe + four historical months per run. Avoid bulk throttling.
BACKFILL_MONTHS_PER_RUN = 4


def three_years_before(day: date) -> date:
    try:
        return day.replace(year=day.year - 3)
    except ValueError:
        return day.replace(year=day.year - 3, day=28)


def month_range(start: date, stop: date):
    y, m = start.year, start.month
    while (y, m) <= (stop.year, stop.month):
        yield (y, m)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def fetch_month(year: int, month: int, now: datetime) -> dict[str, tuple]:
    url = f"{BASE}/{year}/{month - 1:02d}/ASK_candles_hour_1.bi5"
    compressed = None
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "XAUUSD-H1-Archive/2.0"})
            with urlopen(req, timeout=35) as response:
                compressed = response.read()
            break
        except HTTPError as exc:
            if exc.code == 404 and (year, month) == (now.year, now.month):
                print(f"Current-month file not published yet: {url}", flush=True)
                return {}
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"Dukascopy HTTP {exc.code} for {url}") from exc
            backoff = 6 * (attempt + 1)
            print(f"Dukascopy HTTP {exc.code}; retry in {backoff}s: {url}", flush=True)
            time.sleep(backoff)
        except (URLError, TimeoutError, ConnectionError) as exc:
            if attempt == 2:
                raise RuntimeError(f"Network error: {exc} for {url}") from exc
            backoff = 6 * (attempt + 1)
            print(f"Network error; retry in {backoff}s: {url}", flush=True)
            time.sleep(backoff)

    if not compressed:
        if (year, month) == (now.year, now.month):
            return {}
        raise RuntimeError(f"Empty historical month: {url}")

    try:
        uncompressed = lzma.decompress(compressed)
    except lzma.LZMAError as exc:
        raise RuntimeError(f"Can't decompress file {url}") from exc
    if len(uncompressed) % RECORD.size:
        raise RuntimeError(f"Unexpected record size in {url}")

    month_start = datetime(year, month, 1, tzinfo=timezone.utc)
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    candles = {}
    for seconds, raw_open, raw_close, raw_low, raw_high, volume in RECORD.iter_unpack(uncompressed):
        stamp = month_start + timedelta(seconds=seconds)
        if stamp.year != year or stamp.month != month:
            raise RuntimeError(f"Unexpected timestamp {stamp} in {url}")
        if stamp >= current_hour:
            continue
        op, hi, lo, cl = [v / PRICE_DIVISOR for v in
                           (raw_open, raw_high, raw_low, raw_close)]
        if not (200 <= lo <= min(op, cl) <= max(op, cl) <= hi <= 20000):
            raise RuntimeError(f"Invalid price at {stamp}: {op}, {hi}, {lo}, {cl}")
        stamp_s = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        candles[stamp_s] = (stamp_s, op, hi, lo, cl, volume)
    print(f"Fetched {year}-{month:02d}: {len(candles)} closed H1 candles", flush=True)
    return candles


def read_existing() -> dict[str, tuple]:
    if not H1_FILE.exists():
        return {}
    result = {}
    with H1_FILE.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            k = row["UTC_Time"]
            result[k] = (k, *[float(row[c]) for c in
                             ("Open", "High", "Low", "Close", "Volume")])
    return result


def write_csv(path: Path, columns: list[str], rows: list[tuple]):
    temp = path.with_suffix(".tmp")
    with temp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)
    temp.replace(path)


def fmt_h1(row):
    stamp, op, hi, lo, cl, vol = row
    return (stamp, f"{op:.3f}", f"{hi:.3f}", f"{lo:.3f}",
            f"{cl:.3f}", f"{vol:.4f}")


def aggregate_utc_days(rows: list[tuple], now: datetime) -> list[tuple]:
    results = []
    current_day, group = None, []

    def emit():
        if not group:
            return
        date_str = group[0][0][:10]
        op = group[0][1]
        hi = max(r[2] for r in group)
        lo = min(r[3] for r in group)
        cl = group[-1][4]
        pct = (cl / op - 1) * 100
        results.append((date_str, f"{op:.3f}", f"{hi:.3f}",
                        f"{lo:.3f}", f"{cl:.3f}", f"{pct:.3f}",
                        len(group), "IN_PROGRESS" if date_str == now.date().isoformat()
                        else "CLOSED"))

    for row in rows:
        day = row[0][:10]
        if current_day is not None and day != current_day:
            emit()
            group = []
        current_day = day
        group.append(row)
    emit()
    return results


def main():
    OUT.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    start = three_years_before(now.date())
    months = list(month_range(start, now.date()))
    current = (now.year, now.month)
    status = {}
    if STATUS_FILE.exists():
        try:
            status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("Existing STATUS.json unreadable; rebuilding progress", flush=True)
    completed = set(status.get("completed_months", []))
    completed &= {month_key(y, m) for y, m in months if (y, m) != current}

    existing = read_existing()
    missing = [(y, m) for y, m in months if
               (y, m) != current and month_key(y, m) not in completed]
    # Always try current month to keep last hour current, then backfill oldest gaps.
    selected = [current] + missing[:BACKFILL_MONTHS_PER_RUN]
    print(f"Months complete: {len(completed)}/{len(months)-1}; "
          f"trying {selected}", flush=True)

    failed = []
    successful = 0
    for year, month in selected:
        try:
            replacement = fetch_month(year, month, now)
        except (RuntimeError, ValueError, OSError) as exc:
            failed.append(month_key(year, month))
            print(f"WARNING: {exc}; will retry on a later run", flush=True)
            continue
        if not replacement:
            if (year, month) != current:
                failed.append(month_key(year, month))
                print(f"WARNING: No data for completed month {year}-{month:02d}", flush=True)
            continue
        prefix = f"{year}-{month:02d}-"
        existing = {k: v for k, v in existing.items() if not k.startswith(prefix)}
        existing.update(replacement)
        if (year, month) != current:
            completed.add(month_key(year, month))
        successful += 1
        time.sleep(2)  # Request pacing helps to avoid rate limiting.

    floor_hour = now.replace(minute=0, second=0, microsecond=0)
    rows = [r for k, r in sorted(existing.items())
            if start.isoformat() <= k[:10] and
            datetime.strptime(k, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc) < floor_hour]
    if not rows:
        raise RuntimeError("Dukascopy gave no usable H1 data; retry later. "
                           f"Failed months: {failed}")

    daily = aggregate_utc_days(rows, now)
    remaining = [month_key(y, m) for y, m in months
                 if (y, m) != current and month_key(y, m) not in completed]
    write_csv(H1_FILE, ["UTC_Time", "Open", "High", "Low", "Close", "Volume"],
              [fmt_h1(r) for r in rows])
    write_csv(D1_FILE, ["UTC_Date", "Open", "High", "Low", "Close",
                        "Change_Pct", "H1_Count", "Status"], daily)
    result = {
        "instrument": "XAU/USD", "price_side": "ASK", "timezone": "UTC",
        "window_start_utc": start.isoformat(),
        "checked_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_h1_open_utc": rows[-1][0],
        "last_d1_date_utc": daily[-1][0],
        "h1_count": len(rows), "d1_count": len(daily),
        "complete_historical_backfill": len(remaining) == 0,
        "completed_month_count": len(completed),
        "remaining_month_count": len(remaining),
        "missing_months": remaining,
        "failed_months_this_run": failed,
        "months_downloaded_this_run": successful,
        "completed_months": sorted(completed),
        "note": "Partial historical coverage until missing_months is empty. "
                "Last H1 may be stale if Dukascopy has not published current month. "
                "D1 is aggregated from available closed H1 UTC candles.",
        "source": BASE,
    }
    STATUS_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
