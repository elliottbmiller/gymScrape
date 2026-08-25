#!/usr/bin/env python3
"""
Turn occupancy.csv into a weekday x hour picture of how busy each space is.

  python analyze.py --list             # locations and how many counts each has
  python analyze.py -l "colby"         # heatmap for one space
  python analyze.py -l "colby" --quiet-times
  python analyze.py --quality          # audit the human-entered data
  python analyze.py --cadence          # how often staff actually update

Rows are bucketed by when the count was TAKEN (reading_time), not when we polled.

Because the counts are entered by hand, --quality is worth running before you
trust any of this. It looks for the fingerprints of estimation: round numbers,
counts copy-forwarded unchanged between rounds, and hours of the day where
nobody is updating at all.

Stdlib only. Cells are average % of capacity.
"""

import argparse
import csv
import os
import statistics
import sys
from collections import defaultdict, Counter
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "occupancy.csv")

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
RAMP = [22, 28, 34, 100, 142, 178, 208, 202, 196]


def colorize(pct, text, enabled):
    if not enabled:
        return text
    idx = min(len(RAMP) - 1, int(pct / 100 * len(RAMP)))
    return f"\033[38;5;{RAMP[idx]}m{text}\033[0m"


def parse_dt(text):
    text = str(text).strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def load(path, keep_closed=False):
    if not os.path.exists(path):
        sys.exit(f"No data file at {path}. Run scrape.py at least once first.")

    rows, dropped_closed = [], 0
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("pct_full") or not r.get("reading_hour"):
                continue
            try:
                r["pct_full"] = float(r["pct_full"])
                r["hour"] = int(r["reading_hour"])
                r["count"] = int(r["count"])
            except ValueError:
                continue
            r["weekday"] = r.get("reading_weekday", "")

            if not keep_closed and r.get("is_closed", "").lower() == "true":
                dropped_closed += 1
                continue
            rows.append(r)

    if dropped_closed:
        print(f"(filtered out {dropped_closed} readings from closed spaces)")
    if not rows:
        sys.exit("No usable rows yet -- give it a few hours of polling.")
    return rows


def build_grid(rows):
    grid = defaultdict(list)
    for r in rows:
        grid[(r["weekday"], r["hour"])].append(r["pct_full"])
    return grid


def print_heatmap(name, rows, start, end, color):
    grid = build_grid(rows)
    hours = list(range(start, end + 1))
    overall = statistics.mean(r["pct_full"] for r in rows)
    cap = rows[-1].get("capacity", "?")

    print(f"\n{name}  (cap {cap}, {len(rows)} counts, avg {overall:.0f}% full)")
    print("     " + "".join(f"{h:>4}" for h in hours))
    print("     " + "-" * (4 * len(hours)))

    for day in DAYS:
        cells = []
        for h in hours:
            vals = grid.get((day, h))
            if not vals:
                cells.append("   .")
            else:
                avg = statistics.mean(vals)
                cells.append(colorize(avg, f"{avg:>4.0f}", color))
        print(f"{day}  " + "".join(cells))

    thin = sum(1 for day in DAYS for h in hours if len(grid.get((day, h), [])) == 1)
    missing = sum(1 for day in DAYS for h in hours if not grid.get((day, h)))
    notes = []
    if missing:
        notes.append(f"{missing} slots with no count ('.')")
    if thin:
        notes.append(f"{thin} slots resting on a single count")
    if notes:
        print("     (" + "; ".join(notes) + ")")


def print_quiet_times(name, rows, start, end, top=12):
    grid = build_grid(rows)
    scored = [(statistics.mean(v), d, h, len(v))
              for (d, h), v in grid.items()
              if start <= h <= end and len(v) >= 2]
    if not scored:
        print(f"\n{name}: not enough repeat counts yet to rank quiet times.")
        return
    scored.sort(key=lambda t: t[0])
    print(f"\n{name} -- quietest slots so far:")
    for avg, day, hour, n in scored[:top]:
        print(f"  {day} {hour:>2}:00-{hour:>2}:59   {avg:>5.1f}% full   (n={n})")


def print_cadence(rows):
    """How often do staff actually enter a new count?"""
    by_loc = defaultdict(list)
    for r in rows:
        dt = parse_dt(r.get("reading_time", ""))
        if dt:
            by_loc[r["location_name"]].append(dt)

    gaps = []
    per_loc = {}
    for name, times in by_loc.items():
        times = sorted(set(times))
        loc_gaps = [(b - a).total_seconds() / 60.0 for a, b in zip(times, times[1:])]
        loc_gaps = [g for g in loc_gaps if 0 < g < 24 * 60]
        if loc_gaps:
            per_loc[name] = statistics.median(loc_gaps)
        gaps.extend(loc_gaps)

    print("\nHow often counts get entered")
    print("-" * 46)
    if not gaps:
        print("Not enough distinct counts yet. Check back after a few hours.")
        return

    gaps.sort()
    med = statistics.median(gaps)
    print(f"  {len(gaps)} intervals between counts")
    print(f"  median   {med:>6.0f} min")
    print(f"  10th pct {gaps[len(gaps)//10]:>6.0f} min")
    print(f"  90th pct {gaps[(9*len(gaps))//10]:>6.0f} min")

    if per_loc:
        slow = sorted(per_loc.items(), key=lambda kv: -kv[1])[:5]
        print("\n  Least frequently updated:")
        for name, m in slow:
            print(f"    {m:>5.0f} min   {name}")

    print()
    print(f"  -> A week gives you roughly {int(7 * 18 * 60 / med)} counts per location.")
    if med > 45:
        print(f"     Polling every 30 min is finer than the {med:.0f}-min update cycle,")
        print( "     which is fine -- dedupe means extra polls cost nothing but a request.")


def print_quality(rows):
    """Look for the fingerprints of hand-entered estimates."""
    print("\nData quality audit")
    print("-" * 46)

    counts = [r["count"] for r in rows]
    n = len(counts)

    # 1. Round-number clustering. Real counts hit every integer; estimates cluster.
    mult5 = sum(1 for c in counts if c % 5 == 0)
    mult10 = sum(1 for c in counts if c % 10 == 0)
    print(f"  {n} counts total")
    print(f"  ending in 0 or 5: {100.0*mult5/n:>5.1f}%   (chance would be ~20%)")
    print(f"  ending in 0:      {100.0*mult10/n:>5.1f}%   (chance would be ~10%)")
    if mult5 / n > 0.40:
        print("    ^ heavy rounding -- treat these as estimates, not headcounts")

    # 2. Copy-forward: a new timestamp carrying the identical previous number.
    by_loc = defaultdict(list)
    for r in rows:
        dt = parse_dt(r.get("reading_time", ""))
        if dt:
            by_loc[r["location_name"]].append((dt, r["count"]))

    repeats = compared = 0
    for name, seq in by_loc.items():
        seq.sort()
        for (_, a), (_, b) in zip(seq, seq[1:]):
            compared += 1
            if a == b:
                repeats += 1
    if compared:
        print(f"\n  identical to previous count: {100.0*repeats/compared:>5.1f}% of updates")
        if repeats / compared > 0.25:
            print("    ^ numbers often carried forward unchanged between rounds")

    # 3. Coverage by hour -- where is nobody counting?
    per_hour = Counter(r["hour"] for r in rows)
    active = [h for h in range(24) if per_hour.get(h)]
    if active:
        print("\n  counts entered per hour of day:")
        peak = max(per_hour.values())
        for h in range(min(active), max(active) + 1):
            c = per_hour.get(h, 0)
            bar = "#" * int(round(20 * c / peak)) if peak else ""
            flag = "   <- no data" if c == 0 else ""
            print(f"    {h:>2}:00  {c:>4}  {bar}{flag}")

    print("\n  Reminder: hours with few counts are hours nobody walked a round,")
    print("  not hours the gym was empty. Don't read a thin cell as a quiet one.")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-f", "--file", default=CSV_PATH)
    p.add_argument("-l", "--location", help="substring of the location name")
    p.add_argument("--list", action="store_true")
    p.add_argument("--start", type=int, default=6)
    p.add_argument("--end", type=int, default=23)
    p.add_argument("--quiet-times", action="store_true")
    p.add_argument("--cadence", action="store_true")
    p.add_argument("--quality", action="store_true")
    p.add_argument("--keep-closed", action="store_true")
    p.add_argument("--no-color", action="store_true")
    args = p.parse_args()

    rows = load(args.file, args.keep_closed)
    color = not args.no_color and sys.stdout.isatty()

    if args.location:
        needle = args.location.lower()
        rows = [r for r in rows if needle in r["location_name"].lower()]
        if not rows:
            sys.exit(f"No location matching {args.location!r}. Try --list.")

    if args.cadence:
        print_cadence(rows)
        return
    if args.quality:
        print_quality(rows)
        return

    by_loc = defaultdict(list)
    for r in rows:
        by_loc[r["location_name"]].append(r)

    if args.list:
        for name in sorted(by_loc):
            print(f"{len(by_loc[name]):>5} counts   {name}")
        return

    for name in sorted(by_loc):
        print_heatmap(name, by_loc[name], args.start, args.end, color)
        if args.quiet_times:
            print_quiet_times(name, by_loc[name], args.start, args.end)


if __name__ == "__main__":
    main()
