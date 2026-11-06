#!/usr/bin/env python3
"""XAU/USD ASK H1 and aggregated UTC D1, rolling 3 years.

Uses the Dukascopy public compressed hourly candle files, only the
Python standard library, and updates the latest two months on every run.
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
RECORD = struct.Struct(">IIIIIf")  # seconds, open, close, low, high, volume
PRICE_DIVISOR = 1000


def three_years_before(day: date) -> date:
    try:
        return day.replace(year=day.year - 3)
    except ValueError:  # February 29
        return day.replace(year=day.year - 3, day=28)


def month_range(start: date, stop: date):
    y, m = start.year, start.month
    while (y, m) <= (stop.year, stop.month):
        yield (y, m)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def previous_month(day: date) -> tuple[int, int]:
    if day.month == 1:
        return day.year - 1, 12
    return day.year, day.month - 1


def fetch_month(year: int, month: int, now: datetime) -> dict[str, tuple]:
    url = f"{BASE}/{year}/{month - 1:02d}/ASK_candles_hour_1.bi5"
    compressed = None
    for attempt in range(4):
        try:
            req = Request(url, headers={"User-Agent": "XAUUSD-H1-Archive/1.0"})
            with urlopen(req, timeout=40) as response:
                compressed = response.read()
            break
        except HTTPError as exc:
            # The current month may not yet exist right after its start.
            if exc.code == 404 and (year, month) == (now.year, now.month):
                print(f"Current-month file not published yet: {url}")
                return {}
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise RuntimeError(f"Dukascopy HTTP {exc.code} for {url}") from exc
        except (URLError, TimeoutError, ConnectionError) as exc:
            if attempt == 3:
                raise RuntimeError(f"Network error downloading {url}: {exc}") from exc
        time.sleep(1.5 * (attempt + 1))
    if not compressed:
        if (year, month) == (now.year, now.month):
            return {}
        raise RuntimeError(f"Empty historical month: {url}")

    try:
        uncompressed = lzma.decompress(compressed)
    except lzma.LZMAError as exc:
        raise RuntimeError(f"Can't decompress Dukascopy file {url}") from exc
    if len(uncompressed) % RECORD.size:
        raise RuntimeError(f"Unexpected record size in {url}")

    month_start = datetime(year, month, 1, tzinfo=timezone.utc)
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    candles = {}
    for seconds, raw_open, raw_close, raw_low, raw_high, volume in RECORD.iter_unpack(uncompressed):
        stamp = month_start + timedelta(seconds=seconds)
        if stamp.year != year or stamp.month != month:
            raise RuntimeError(f"Unexpected timestamp {stamp} in {url}")
        if stamp >= current_hour:  # Never save an unfinished H1 candle.
            continue
        op, hi, lo, cl = [raw / PRICE_DIVISOR for raw in (raw_open, raw_high, raw_low, raw_close)]
        if not (200 <= lo <= min(op, cl) <= max(op, cl) <= hi <= 20000):
            raise RuntimeError(f"Invalid XAUUSD price values at {stamp}: {op}, {hi}, {lo}, {cl}")
        key = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        candles[key] = (key, op, hi, lo, cl, volume)
    print(f"Fetched {year}-{month:02d}: {len(candles)} closed H1 candles")
    return candles


def read_existing() -> dict[str, tuple]:
    if not H1_FILE.exists():
        return {}
    data = {}
    with H1_FILE.open(newline="", encoding="utf-8") as file:
        for r in csv.DictReader(file):
            k = r["UTC_Time"]
            data[k] = (k, *[float(r[c]) for c in ("Open", "High", "Low", "Close", "Volume")])
    return data


def write_csv(path: Path, columns: list[str], rows: list[tuple]):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(columns)
        writer.writerows(rows)
    tmp.replace(path)


def fmt_h1(row):
    stamp, op, hi, lo, cl, vol = row
    return (stamp, f"{op:.3f}", f"{hi:.3f}", f"{lo:.3f}", f"{cl:.3f}", f"{vol:.4f}")


def aggregate_utc_days(rows: list[tuple], now: datetime) -> list[tuple]:
    result = []
    current_day = None
    group = []

    def emit():
        if not group:
            return
        date_str = group[0][0][:10]
        op = group[0][1]
        hi = max(r[2] for r in group)
        lo = min(r[3] for r in group)
        cl = group[-1][4]
        change_pct = (cl / op - 1) * 100
        result.append((date_str, f"{op:.3f}", f"{hi:.3f}", f"{lo:.3f}",
                       f"{cl:.3f}", f"{change_pct:.3f}", len(group),
                       "IN_PROGRESS" if date_str == now.date().isoformat() else "CLOSED"))

    for row in rows:
        day_str = row[0][:10]
        if current_day is not None and day_str != current_day:
            emit()
            group = []
        current_day = day_str
        group.append(row)
    emit()
    return result


def main():
    OUT.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    start = three_years_before(now.date())
    existing = read_existing()
    if not existing:
        months = list(month_range(start, now.date()))
        print(f"First run: download {len(months)} months")
    else:
        months = sorted({previous_month(now.date()), (now.year, now.month)})
        print(f"Hourly update: refresh months {months}")
    for year, month in months:
        replacement = fetch_month(year, month, now)
        # A missing/empty current-month file should not remove old data.
        if not replacement:
            continue
        prefix = f"{year}-{month:02d}-"
        existing = {k: v for k, v in existing.items() if not k.startswith(prefix)}
        existing.update(replacement)

    floor_hour = now.replace(minute=0, second=0, microsecond=0)
    rows = [r for k, r in sorted(existing.items())
            if start.isoformat() <= k[:10] and datetime.strptime(k, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc) < floor_hour]
    if not rows:
        raise RuntimeError("No historical H1 candles retrieved; refusing to write empty output")

    daily = aggregate_utc_days(rows, now)
    write_csv(H1_FILE, ["UTC_Time", "Open", "High", "Low", "Close", "Volume"], [fmt_h1(r) for r in rows])
    write_csv(D1_FILE, ["UTC_Date", "Open", "High", "Low", "Close", "Change_Pct", "H1_Count", "Status"], daily)
    status = {
        "instrument": "XAU/USD", "price_side": "ASK", "timezone": "UTC",
        "window_start_utc": start.isoformat(), "checked_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_h1_open_utc": rows[-1][0], "last_d1_date_utc": daily[-1][0],
        "h1_count": len(rows), "d1_count": len(daily),
        "note": "H1 closed candles only; current UTC day's D1 is IN_PROGRESS; D1 aggregated from H1",
        "source": "https://datafeed.dukascopy.com/datafeed/XAUUSD",
    }
    STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()
