#!/usr/bin/env python3
"""
Merge occupancy CSVs from multiple sources into one clean file.

Useful if you collect from more than one place -- say GitHub Actions running
around the clock plus your laptop when it happens to be on. Because every row is
keyed by (location_id, reading_time), overlapping collection merges cleanly: the
same staff count seen by both collectors is one row, not two.

  python merge.py laptop.csv actions.csv -o occupancy.csv

Safe to re-run. Output is sorted by reading time.
"""

import argparse
import csv
import os
import sys

FIELDS = [
    "reading_time", "reading_weekday", "reading_hour", "reading_minute",
    "location_id", "location_name", "facility_name",
    "count", "capacity", "pct_full", "is_closed",
    "observed_at", "observed_lag_min",
]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="CSV files to merge")
    ap.add_argument("-o", "--output", default="merged.csv")
    args = ap.parse_args()

    merged = {}
    per_file = {}

    for path in args.inputs:
        if not os.path.exists(path):
            sys.exit(f"No such file: {path}")
        n = 0
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row.get("location_id"), row.get("reading_time"))
                if not all(key):
                    continue
                n += 1
                # Keep whichever copy noticed the count soonest.
                prior = merged.get(key)
                if prior is None:
                    merged[key] = row
                else:
                    try:
                        if float(row.get("observed_lag_min") or 1e9) < \
                           float(prior.get("observed_lag_min") or 1e9):
                            merged[key] = row
                    except ValueError:
                        pass
        per_file[path] = n

    rows = sorted(merged.values(),
                  key=lambda r: (r.get("reading_time", ""), r.get("location_name", "")))

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    total_in = sum(per_file.values())
    for path, n in per_file.items():
        print(f"  {n:>6} rows   {path}")
    print(f"  {len(rows):>6} rows   {args.output}  "
          f"({total_in - len(rows)} duplicate counts collapsed)")


if __name__ == "__main__":
    main()
