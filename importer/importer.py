#!/usr/bin/env python3
"""Import Ergon NEM12 detailed meter exports into an existing InfluxDB 2.x.

Standard library only. The InfluxDB write API takes line protocol over plain
HTTP, so there is no dependency to install and no image to build: this runs on
a stock python image with the script bind-mounted.

Set DRY_RUN=1 to parse and summarise without writing anything.
"""
import csv
import glob
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("TZ", "Australia/Brisbane"))
URL = os.getenv("INFLUX_URL", "http://localhost:8086").rstrip("/")
TOKEN = os.getenv("INFLUX_TOKEN", "")
ORG = os.getenv("INFLUX_ORG", "home")
BUCKET = os.getenv("INFLUX_BUCKET", "ergon-data")
MEASUREMENT = os.getenv("INFLUX_MEASUREMENT", "grid_energy")
DATA_DIR = os.getenv("DATA_DIR", "/data")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5000"))
DRY_RUN = os.getenv("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")

# NEM12 200 record field positions:
# 200,NMI,NMIConfiguration,RegisterID,NMISuffix,MDMDataStream,MeterSerial,UOM,IntervalLength,NextRead
NMI = 1
REGISTER_ID = 3
NMI_SUFFIX = 4
UOM = 7
INTERVAL_LENGTH = 8

VALID_INTERVALS = (1, 5, 10, 15, 30, 60)


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _interval_minutes(row, path):
    """Interval length from the 200 record, tolerating non-conforming exports."""
    for idx in (INTERVAL_LENGTH, 7, 6, 5):
        if idx >= len(row):
            continue
        minutes = _num(row[idx])
        if minutes is not None and int(minutes) in VALID_INTERVALS:
            return int(minutes)
    raise ValueError(f"{path}: 200 record has no recognisable interval length: {row}")


def _header(row, path):
    interval = _interval_minutes(row, path)
    register = ""
    for idx in (NMI_SUFFIX, REGISTER_ID):
        if idx < len(row) and row[idx].strip():
            register = row[idx].strip()
            break
    uom = row[UOM].strip() if UOM < len(row) else ""
    if uom and uom.lower() != "kwh":
        print(
            f"warning: {path}: register {register or '?'} is in {uom}, not kWh; "
            "kwh and watts_average will be wrong",
            file=sys.stderr,
        )
    return {
        "nmi": row[NMI].strip() if NMI < len(row) else "",
        "register": register or "unknown",
        "interval_minutes": interval,
    }


def _readings(header, day):
    """Expand one buffered 300 record into per-interval readings."""
    interval = header["interval_minutes"]
    midnight = datetime.combine(day["date"], datetime.min.time(), tzinfo=TZ)
    for i, raw in enumerate(day["values"]):
        kwh = _num(raw)
        if kwh is None:
            continue
        yield {
            "time": midnight + timedelta(minutes=i * interval),
            "kwh": kwh,
            "nmi": header["nmi"],
            "register": header["register"],
            "interval_minutes": interval,
            "quality": day["quality"][i] or "unknown",
        }


def parse_nem12(path):
    """Yield readings from the 200/300/400 records of a NEM12 file.

    A 300 record is buffered rather than emitted immediately, because any 400
    records that follow it refine the per-interval quality flags.
    """
    header = None
    day = None

    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.reader(handle):
            if not row:
                continue
            record = row[0].strip()

            if record in ("200", "300", "900"):
                if day is not None:
                    yield from _readings(header, day)
                    day = None

            if record == "200":
                header = _header(row, path)

            elif record == "300":
                if header is None:
                    raise ValueError(f"{path}: 300 record before any 200 record")

                expected = (24 * 60) // header["interval_minutes"]
                values = row[2:2 + expected]
                if len(values) != expected:
                    raise ValueError(
                        f"{path}: {row[1].strip()} has {len(values)} interval values; "
                        f"expected {expected} for {header['interval_minutes']}-minute data"
                    )

                quality = row[2 + expected].strip() if len(row) > 2 + expected else ""
                day = {
                    "date": datetime.strptime(row[1].strip(), "%Y%m%d").date(),
                    "values": values,
                    "quality": [quality] * expected,
                }

            elif record == "400" and day is not None:
                # 400,StartInterval,EndInterval,QualityMethod,...
                # Overrides the 300's day-level flag, which is "V" when quality
                # varies across the day.
                start, end = _num(row[1] if len(row) > 1 else ""), _num(row[2] if len(row) > 2 else "")
                method = row[3].strip() if len(row) > 3 else ""
                if start is None or end is None or not method:
                    continue
                lo = max(int(start) - 1, 0)
                hi = min(int(end), len(day["quality"]))
                for i in range(lo, hi):
                    day["quality"][i] = method

        if day is not None:
            yield from _readings(header, day)


def _escape_key(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace("=", "\\=")
        .replace(" ", "\\ ")
    )


def _escape_measurement(value):
    return str(value).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def _escape_string_field(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def line_protocol(reading):
    """One line of InfluxDB line protocol, second precision.

    nmi and register are tags, so E1 and E2 land in separate series instead of
    overwriting each other. quality is a field, so re-importing a corrected
    export updates the point in place rather than forking a new series.
    """
    # Rounded to shed binary-float noise: 0.019 kWh over 5 min is 228 W, not
    # 227.99999999999997 W.
    watts = round(reading["kwh"] * 60.0 / reading["interval_minutes"] * 1000.0, 6)
    tags = f"nmi={_escape_key(reading['nmi'])},register={_escape_key(reading['register'])}"
    fields = (
        f"kwh={reading['kwh']},"
        f"watts_average={watts},"
        f'quality="{_escape_string_field(reading["quality"])}"'
    )
    return f"{_escape_measurement(MEASUREMENT)},{tags} {fields} {int(reading['time'].timestamp())}"


def write_batch(lines):
    if DRY_RUN or not lines:
        return
    query = urlencode({"org": ORG, "bucket": BUCKET, "precision": "s"})
    request = urllib.request.Request(
        f"{URL}/api/v2/write?{query}",
        data="\n".join(lines).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Token {TOKEN}",
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()
        raise SystemExit(f"InfluxDB rejected the write ({exc.code}): {detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach InfluxDB at {URL}: {exc.reason}")


def main():
    if not DRY_RUN and not TOKEN:
        raise SystemExit(
            "INFLUX_TOKEN is empty. Set it in .env, or set DRY_RUN=1 to parse without writing."
        )

    files = sorted(
        set(glob.glob(f"{DATA_DIR}/*.csv")) | set(glob.glob(f"{DATA_DIR}/*.CSV"))
    )
    if not files:
        raise SystemExit(f"No CSV files found in {DATA_DIR}")

    if DRY_RUN:
        print("DRY_RUN is set: parsing and summarising only, nothing will be written.\n")

    grand_total = 0
    for path in files:
        print(f"Reading {Path(path).name}")
        kwh_by_register = defaultdict(float)
        count_by_register = defaultdict(int)
        days_by_register = defaultdict(set)
        batch = []
        written = 0

        for reading in parse_nem12(path):
            key = (reading["nmi"], reading["register"])
            kwh_by_register[key] += reading["kwh"]
            count_by_register[key] += 1
            days_by_register[key].add(reading["time"].date())

            batch.append(line_protocol(reading))
            if len(batch) >= BATCH_SIZE:
                write_batch(batch)
                written += len(batch)
                batch.clear()

        if batch:
            write_batch(batch)
            written += len(batch)

        for key in sorted(kwh_by_register):
            nmi, register = key
            print(
                f"  NMI {nmi} register {register}: "
                f"{count_by_register[key]:,} intervals over {len(days_by_register[key])} days, "
                f"{kwh_by_register[key]:,.1f} kWh"
            )
        print(f"  total {sum(kwh_by_register.values()):,.1f} kWh across all registers")
        grand_total += written

    if DRY_RUN:
        print("\nNothing written (DRY_RUN).")
    else:
        print(f"\nWrote {grand_total:,} points to bucket '{BUCKET}' at {URL}.")


if __name__ == "__main__":
    main()
