#!/usr/bin/env python3
"""XAU/USD ASK H1 and daily UTC aggregation, rolling three years.

Archived months: Dukascopy H1 bi5; current month: daily M1 bi5.
CSV records are authoritative for progress reconciliation; never delete valid
previously saved hourly bars when replacing a month. Download may be partial
because a provider file can be unavailable, especially for the current day.
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
BACKFILL_MONTHS_PER_RUN = 3
CURRENT_MONTH_OLDER_DAYS_PER_RUN = 2


def is_sufficient_completed_m1_day(day: date, count: int) -> bool:
    """Conservative heuristic, not proof of minute-level completeness.

    Saturday normally has no active hours; Sunday often has just a short
    reopening session. Trading days with fewer than 18 hours should be
    revisited rather than permanently marked as downloaded.
    """
    if day.weekday() == 5:
        return False
    if day.weekday() == 6:
        return count >= 1
    return count >= 18


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
        if stamp >= current_hour or volume <= 0:
            continue
        op, hi, lo, cl = [v / PRICE_DIVISOR for v in
                           (raw_open, raw_high, raw_low, raw_close)]
        if not (200 <= lo <= min(op, cl) <= max(op, cl) <= hi <= 20000):
            raise RuntimeError(f"Invalid price at {stamp}: {op}, {hi}, {lo}, {cl}")
        stamp_s = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        candles[stamp_s] = (stamp_s, op, hi, lo, cl, volume)
    print(f"Fetched {year}-{month:02d}: {len(candles)} closed H1 candles", flush=True)
    return candles



def fetch_daily_m1(day: date, now: datetime) -> dict[str, tuple] | None:
    """Fetch 1-minute ASK candles for a UTC day and roll to CLOSED H1 candles.

    None: not downloadable (retry later). Empty dict: no active trading hours.
    """
    url = (f"{BASE}/{day.year}/{day.month - 1:02d}/{day.day:02d}/"
           "ASK_candles_min_1.bi5")
    compressed = None
    for attempt in range(2):
        try:
            req = Request(url, headers={"User-Agent": "XAUUSD-H1-Archive/3.0"})
            with urlopen(req, timeout=24) as response:
                compressed = response.read()
            break
        except HTTPError as exc:
            if exc.code == 404:
                print(f"M1 daily file not available yet: {url}", flush=True)
                return None
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 1:
                print(f"WARNING: M1 HTTP {exc.code}: {url}", flush=True)
                return None
            print(f"M1 HTTP {exc.code}; retrying: {url}", flush=True)
            time.sleep(5)
        except (URLError, TimeoutError, ConnectionError) as exc:
            if attempt == 1:
                print(f"WARNING: M1 network error {exc}: {url}", flush=True)
                return None
            print(f"M1 connection error; retrying: {url}", flush=True)
            time.sleep(5)

    if not compressed:
        print(f"M1 empty response: {url}", flush=True)
        return None
    try:
        raw = lzma.decompress(compressed)
    except lzma.LZMAError as exc:
        print(f"WARNING: M1 decompress error {exc}: {url}", flush=True)
        return None
    if len(raw) % RECORD.size:
        print(f"WARNING: M1 record size error: {url}", flush=True)
        return None
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    floor_hour = now.replace(minute=0, second=0, microsecond=0)
    minute_bars = {}
    for sec, raw_o, raw_c, raw_l, raw_h, vol in RECORD.iter_unpack(raw):
        stamp = midnight + timedelta(seconds=sec)
        if stamp.date() != day or stamp >= floor_hour:
            continue
        # Dukascopy supplies flat, zero-volume filler during market closures.
        if vol <= 0:
            continue
        o, h, l, c = (v / PRICE_DIVISOR for v in
                       (raw_o, raw_h, raw_l, raw_c))
        if not (200 <= l <= min(o, c) <= max(o, c) <= h <= 20000):
            print(f"WARNING: invalid M1 price at {stamp}: {url}", flush=True)
            return None
        minute_bars[stamp] = (stamp, o, h, l, c, vol)
    buckets = {}
    for stamp, o, h, l, c, vol in sorted(minute_bars.values()):
        hour = stamp.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append((stamp, o, h, l, c, vol))
    result = {}
    for hour, group in buckets.items():
        # Require at least part of the final 10 minutes so an hour that is
        # still being populated is not mistaken for a completed hour.
        if day == now.date() and group[-1][0].minute < 50:
            continue
        stamp_s = hour.strftime("%Y-%m-%dT%H:%M:%SZ")
        result[stamp_s] = (stamp_s, group[0][1], max(x[2] for x in group),
                           min(x[3] for x in group), group[-1][4],
                           sum(x[5] for x in group))
    print(f"Fetched daily M1 {day}: {len(result)} active closed H1 candles", flush=True)
    return result


def read_existing() -> dict[str, tuple]:
    if not H1_FILE.exists():
        return {}
    result = {}
    with H1_FILE.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            k = row["UTC_Time"]
            parsed = (k, *[float(row[c]) for c in
                           ("Open", "High", "Low", "Close", "Volume")])
            if parsed[-1] > 0:
                result[k] = parsed
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
    existing = read_existing()
    # STATUS.json is only a checkpoint; the CSV is the source of truth.
    # A previously completed month can disappear from the CSV after a stale
    # or interrupted workflow commit. Requeue it rather than skipping forever.
    available_month_counts: dict[str, int] = {}
    available_day_counts: dict[str, int] = {}
    for ts in existing:
        k_month, k_day = ts[:7], ts[:10]
        available_month_counts[k_month] = available_month_counts.get(k_month, 0) + 1
        available_day_counts[k_day] = available_day_counts.get(k_day, 0) + 1
    prior_completed = set(status.get("completed_months", []))
    valid_old_months = {month_key(y, m) for y, m in months
                        if (y, m) != current}
    completed = {m for m in prior_completed & valid_old_months
                 if available_month_counts.get(m, 0) > 0}
    lost_months = sorted((prior_completed & valid_old_months) - completed)
    if lost_months:
        print(f"WARNING: checkpoint months absent from H1 CSV; "
              f"queue download: {lost_months}", flush=True)
    missing = [(y, m) for y, m in months if
               (y, m) != current and month_key(y, m) not in completed]
    # For recent data use daily M1 instead of the often unavailable current
    # month's monthly H1 file. Retry today/yesterday every hourly run.
    tracked_days = set(status.get("completed_m1_days", []))
    # A successful HTTP response is not enough for a "complete day" label:
    # make sure the actual saved H1 rows still exist and are substantial.
    verified_days = set()
    for day_text in tracked_days:
        try:
            day_value = date.fromisoformat(day_text)
        except ValueError:
            continue
        if is_sufficient_completed_m1_day(
                day_value, available_day_counts.get(day_text, 0)):
            verified_days.add(day_text)
    lost_days = sorted(tracked_days - verified_days)
    if lost_days:
        print(f"WARNING: checkpoint M1 days incomplete or absent from H1 CSV; "
              f"queue retry: {lost_days}", flush=True)
    tracked_days = verified_days
    current_month_start = now.date().replace(day=1)
    older_days = []
    cursor = current_month_start
    while cursor < now.date() - timedelta(days=1):
        # Sunday trading resumes for a short session; Saturday stays closed.
        if cursor.isoformat() not in tracked_days and cursor.weekday() != 5:
            older_days.append(cursor)
        cursor += timedelta(days=1)
    priority = [now.date(), now.date() - timedelta(days=1)]
    priority += older_days[:CURRENT_MONTH_OLDER_DAYS_PER_RUN]
    selected_days = list(dict.fromkeys(d for d in priority if d >= current_month_start))
    failed_days = []
    recent_ok = False
    for day in selected_days:
        replacement = fetch_daily_m1(day, now)
        if replacement is None:
            failed_days.append(day.isoformat())
            continue
        recent_ok = recent_ok or (day >= now.date()-timedelta(days=1))
        if replacement:
            # Replace only the returned H1 bars; preserve previously saved
            # hours if the upstream day file is temporarily incomplete.
            existing.update(replacement)
        if day < now.date():
            # Do not permanently skip a partially published daily M1 file.
            actual_count = sum(ts.startswith(day.isoformat()) for ts in existing)
            if is_sufficient_completed_m1_day(day, actual_count):
                tracked_days.add(day.isoformat())
            else:
                print(f"WARNING: {day} only {actual_count} active H1 bars; "
                      "retry on a later run", flush=True)
        time.sleep(1)

    # Fill old months gradually; do not re-request completed months.
    selected = missing[:BACKFILL_MONTHS_PER_RUN]
    print(f"Months complete: {len(completed)}/{len(months)-1}; "
          f"historical requested: {selected}; recent M1 dates: {selected_days}",
          flush=True)
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
            failed.append(month_key(year, month))
            continue
        # Merge instead of deleting prior month rows. A provider response
        # containing only part of a month must not erase older archived bars.
        existing.update(replacement)
        completed.add(month_key(year, month))
        successful += 1
        time.sleep(2)

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
        "latest_data_source": "daily_m1_for_current_month_monthly_h1_for_history",
        "recent_m1_download_successful": recent_ok,
        "last_h1_age_hours": round((now - datetime.strptime(
            rows[-1][0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            ).total_seconds() / 3600, 2),
        "current_hourly_data_fresh": (
            (now - datetime.strptime(rows[-1][0], "%Y-%m-%dT%H:%M:%SZ")
             .replace(tzinfo=timezone.utc)).total_seconds() <= 3 * 3600),
        "completed_m1_days": sorted(d for d in tracked_days if
                                     d.startswith(now.strftime("%Y-%m"))),
        "checkpoint_months_requeued": lost_months,
        "checkpoint_m1_days_requeued": lost_days,
        "failed_m1_days_this_run": failed_days,
        "completed_month_count": len(completed),
        "remaining_month_count": len(remaining),
        "missing_months": remaining,
        "failed_months_this_run": failed,
        "months_downloaded_this_run": successful,
        "completed_months": sorted(completed),
        "note": "Historical backfill incomplete while missing_months is nonempty. "
                "Recent hours use daily M1 when available; check current_hourly_data_fresh. "
                "Flat zero-volume periods omitted; daily UTC buckets aggregate active H1.",
        "source": BASE,
    }
    STATUS_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
