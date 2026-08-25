#!/usr/bin/env python3
"""
Poll Purdue RecWell occupancy (GoBoard API) and record each DISTINCT reading.

Important context: these counts are entered by hand by RecWell staff on their
rounds. That shapes the whole design:

  * We dedupe. Polling every 30 min doesn't give you 336 measurements a week --
    it gives you however many rounds staff actually walked, each recorded
    several times. We only append a row when LastUpdatedDateAndTime changes for
    that location, so one row == one real human count.

  * We bucket by when the count was TAKEN, not when we polled. A count entered
    at 19:08 and noticed by cron at 20:00 belongs to the 7pm hour. The CSV
    stores both; analyze.py uses the reading time.

  * LastCount is the real number. CountOfParticipants and PercetageCapacity are
    hardcoded 0 for every location.

  * Closed spaces keep reporting their final count indefinitely, so is_closed
    and staleness are recorded and filtered downstream.

Stdlib only. Run from cron / Task Scheduler every 30 minutes -- polling more
often than staff walk rounds costs nothing now that we dedupe.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

API_URL = (
    "https://goboardapi.azurewebsites.net/api/FacilityCount/GetCountsByAccount"
    "?AccountAPIKey=aedeaf92-036d-4848-980b-7eb5526ea40c"
)

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "occupancy.csv")
LOG_PATH = os.path.join(HERE, "scrape.log")

USER_AGENT = "recwell-personal-logger/1.0 (personal schedule planning; low volume)"
TIMEOUT = 20
RETRIES = 3
RETRY_BACKOFF = 5

FIELDS = [
    "reading_time",       # when staff entered the count -- the timestamp that matters
    "reading_weekday",
    "reading_hour",
    "reading_minute",
    "location_id",
    "location_name",
    "facility_name",
    "count",              # LastCount
    "capacity",           # TotalCapacity
    "pct_full",
    "is_closed",
    "observed_at",        # when our poll first saw this reading
    "observed_lag_min",   # how long after the count we noticed it
]


def log(msg):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fetch_raw():
    req = urllib.request.Request(API_URL, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/xml;q=0.9",
    })
    delay = RETRY_BACKOFF
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8-sig", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_err = e
            log(f"attempt {attempt}/{RETRIES} failed: {e}")
        if attempt < RETRIES:
            time.sleep(delay)
            delay *= 2
    raise SystemExit(f"all attempts failed: {last_err}")


def strip_ns(tag):
    return tag.rsplit("}", 1)[-1]


def parse(raw):
    """Accept either JSON or XML; return a list of plain dicts."""
    text = raw.lstrip()
    if text.startswith("[") or text.startswith("{"):
        data = json.loads(text)
        return [data] if isinstance(data, dict) else data

    root = ET.fromstring(text)
    out = []
    for node in root:
        out.append({strip_ns(c.tag): c.text for c in node})
    return out


def as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_dt(value):
    """LastUpdatedDateAndTime carries no timezone; treat it as local."""
    if not value:
        return None
    text = str(value).strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def load_seen(path):
    """location_id -> most recent reading_time we've already recorded."""
    seen = {}
    if not os.path.exists(path):
        return seen
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                lid = row.get("location_id")
                rt = row.get("reading_time")
                if lid and rt:
                    seen[lid] = rt
    except (OSError, csv.Error) as e:
        log(f"warning: could not read existing CSV ({e}); may write duplicates")
    return seen


def flatten(rec, now_local):
    count = as_int(rec.get("LastCount"))
    capacity = as_int(rec.get("TotalCapacity"))

    pct = ""
    if count is not None and capacity:
        pct = round(100.0 * count / capacity, 1)

    raw_rt = rec.get("LastUpdatedDateAndTime") or ""
    rt = parse_dt(raw_rt)

    lag = ""
    if rt is not None:
        lag = round((now_local - rt).total_seconds() / 60.0, 1)

    closed = str(rec.get("IsClosed")).strip().lower() in ("true", "1", "yes")

    return {
        "reading_time": raw_rt,
        "reading_weekday": rt.strftime("%a") if rt else "",
        "reading_hour": rt.hour if rt else "",
        "reading_minute": rt.minute if rt else "",
        "location_id": rec.get("LocationId") or "",
        "location_name": (rec.get("LocationName") or "").strip(),
        "facility_name": (rec.get("FacilityName") or "").strip(),
        "count": count if count is not None else "",
        "capacity": capacity if capacity is not None else "",
        "pct_full": pct,
        "is_closed": "true" if closed else "false",
        "observed_at": now_local.isoformat(timespec="seconds"),
        "observed_lag_min": lag,
    }


def main():
    ap = argparse.ArgumentParser(description="Poll RecWell occupancy.")
    ap.add_argument("--all", action="store_true",
                    help="record every poll, including readings already seen")
    args = ap.parse_args()

    now_local = datetime.now()

    raw = fetch_raw()
    try:
        records = parse(raw)
    except (json.JSONDecodeError, ET.ParseError) as e:
        log(f"could not parse response ({e}). First 200 chars: {raw[:200]!r}")
        raise SystemExit(2)

    if not records:
        log("endpoint returned zero locations -- writing nothing")
        raise SystemExit(2)

    rows = [flatten(r, now_local) for r in records]

    if not args.all:
        seen = load_seen(CSV_PATH)
        fresh = [r for r in rows
                 if r["reading_time"] and seen.get(r["location_id"]) != r["reading_time"]]
    else:
        fresh = rows

    if not fresh:
        log(f"polled {len(rows)} locations, no new counts since last poll")
        return

    new_file = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerows(fresh)

    n_open = sum(1 for r in fresh if r["is_closed"] == "false")
    log(f"polled {len(rows)} locations, {len(fresh)} new counts ({n_open} open) -> {CSV_PATH}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log(f"UNEXPECTED ERROR: {type(e).__name__}: {e}")
        sys.exit(1)
