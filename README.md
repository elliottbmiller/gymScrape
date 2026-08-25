# RecWell occupancy logger

Polls Purdue RecWell's facility counts and builds a weekday × hour picture of
when each space is empty.

No dependencies — just Python 3.8+.

## What this data actually is

The counts are **entered by hand by RecWell staff on their rounds**. Not sensors,
not turnstiles — a person walks through, eyeballs the room, types a number. That
single fact drives most of the design here, and it should shape how much you
trust the output.

Consequences worth internalizing:

**One row = one human count, not one poll.** The scraper dedupes on the reading
timestamp, so polling every 30 minutes doesn't manufacture 336 fake measurements
a week. If staff walk hourly, you get roughly 126 real counts per location per
week, and the CSV says so honestly. Extra polls cost nothing but an HTTP request.

**Times are bucketed by when the count was taken, not when cron fired.** A count
entered at 19:08 and noticed at 20:00 belongs to the 7pm hour. Getting this wrong
would smear every reading forward by up to half an hour.

**A thin cell means nobody counted, not that the gym was empty.** This is the
failure mode most likely to mislead you, because it biases toward what you want
to believe. If nobody walks a round at 6am, 6am inherits whatever the 5am count
was — possibly genuinely quiet, possibly stale nonsense.

**Peak hours may be systematically undercounted.** Staff are busiest exactly when
the building is busiest, so rounds slip when it's packed and estimates get
rougher. Expect true peaks to be at least as bad as they look, never better.

## The endpoint

```
https://goboardapi.azurewebsites.net/api/FacilityCount/GetCountsByAccount?AccountAPIKey=aedeaf92-036d-4848-980b-7eb5526ea40c
```

Two field-level gotchas, both handled: the real number is `LastCount`
(`CountOfParticipants` and `PercetageCapacity` are hardcoded `0` for every
location), and closed spaces keep reporting their final count indefinitely, so
`IsClosed` rows are recorded but filtered out of the analysis by default.

## Where to run it

Test it once locally first:

```
python scrape.py
```

**Don't collect on a laptop that gets closed.** The API exposes only the current
count — there is no history endpoint, so a round walked while your machine was
off is lost permanently. Task Scheduler's "run as soon as possible after a missed
start" doesn't rescue you: it fires on boot and records whatever the number is
*then*, not the one you missed.

The gaps also aren't random. A laptop that's off overnight and during class
misses precisely the early mornings and mid-mornings you're trying to evaluate.
In a simulated run against 134 known staff counts, an evenings-only laptop
captured 38 of them — 28%, all from hours already known to be busy.

### Purdue Linux servers (always on, and you already have access)

Run the preflight check **on the machine you plan to schedule from**:

```
bash preflight.sh
```

It verifies Python, outbound network to the API, write access, timezone, cron
availability, and does a live scrape — then prints the exact crontab line to use.

Four things that specifically break on shared university Linux:

**Load-balanced hostnames.** `data.cs.purdue.edu` and similar aliases round-robin
across machines. Your crontab exists only on the host you edited it on. Note the
fully-qualified hostname preflight prints and `ssh` to that one directly
thereafter, or cron may simply never fire.

**AFS home directories.** Cron jobs run without a Kerberos token, so writes to
AFS start failing once your token expires — silently, hours later, after your
test run looked perfect. Preflight detects the filesystem type and warns. If it
says AFS, write to local scratch instead of home.

**Cron's empty PATH.** `python3` alone often isn't found. Use the absolute path
preflight reports.

**Mail spam.** Without `MAILTO=""`, every run emails you its output. 48 messages
a day.

The resulting crontab (`crontab -e`):

```
MAILTO=""
*/30 * * * * /usr/bin/python3 /home/you/recwell/scrape.py >> /home/you/recwell/cron.log 2>&1
```

RCAC clusters (Scholar, Negishi, Gilbreth) generally don't offer user cron; CS
department machines usually do. Preflight tells you which situation you're in.

On courtesy: this is 48 small requests a day to a public endpoint, which is
nothing. Keep it at that, don't parallelize it, and stop if anyone asks.

### Verify it's still alive

Cron failing silently is the main risk, and you won't notice until you've lost a
week. Two quick checks:

```
tail cron.log              # should have a recent entry
python3 analyze.py --quality   # coverage-by-hour reveals collection gaps
```



### GitHub Actions (fallback, or run alongside for redundancy)

`.github/workflows/scrape.yml` is included and ready. Full setup:

**1. Make the repo public.** Free unlimited Actions minutes; there's nothing
sensitive here. A private repo would burn roughly 1,400 of your 2,000 free
monthly minutes at this frequency.

**2. Push the files** from the folder containing them:

```
git init
git branch -M main
git add .
git commit -m "recwell occupancy logger"
git remote add origin https://github.com/YOU/recwell-usage.git
git push -u origin main
```

**3. Grant write permission.** Repo → **Settings → Actions → General →
Workflow permissions** → **Read and write permissions** → Save. The default is
read-only and the commit step will fail without this. It's the single most
common setup mistake.

**4. Test immediately.** Repo → **Actions** tab → enable workflows if prompted →
select "poll recwell counts" → **Run workflow**. It should finish green in under
a minute, and `occupancy.csv` should appear in the repo with ~37 rows.

**5. Read the data** whenever you like:

```
git pull
python analyze.py --list
python analyze.py -l "colby" --quiet-times
```

Notes on the platform:

- Scheduled runs are commonly 5–20 minutes late and occasionally skipped under
  load. Harmless here — the analysis keys off each count's own timestamp, and
  staff only enter counts about hourly anyway.
- Most runs will report "no new counts this run" and commit nothing. That's
  correct behaviour, not a failure: we poll twice as often as the numbers change.
- GitHub disables scheduled workflows after 60 days without repo activity, and
  the bot's own commits don't reset that clock. Irrelevant for a few weeks; if it
  lapses, the Actions tab has a re-enable button.
- GitHub emails you when a workflow run fails, which covers the silent-death
  problem you'd otherwise have with cron.

### If you'd rather use the laptop anyway

Task Scheduler → Create Task (not "Basic Task"):

- **General** → "Run whether user is logged on or not"
- **Triggers** → New → Daily → "Repeat task every 30 minutes" for "Indefinitely"
- **Actions** → Start a program → `python` → arguments `scrape.py` → **Start in**
  set to the script's folder
- **Conditions** → uncheck "Start the task only if the computer is on AC power",
  check "Wake the computer to run this task"
- **Settings** → check "Run task as soon as possible after a scheduled start is
  missed"

Wake timers only work from sleep, not from shutdown or hibernation, so this
narrows the gaps rather than closing them.

### Running both

Fine, and they merge losslessly since every row is keyed by
`(location_id, reading_time)`:

```
python merge.py laptop.csv actions.csv -o occupancy.csv
```

A count seen by both collectors becomes one row, keeping whichever copy noticed
it sooner.

## Reading the data

```bash
python3 analyze.py --list                      # locations and count volume
python3 analyze.py -l "colby"                  # heatmap for one space
python3 analyze.py -l "colby" --quiet-times    # + ranked emptiest slots
python3 analyze.py --cadence                   # how often staff update
python3 analyze.py --quality                   # audit the hand-entered numbers
```

Run `--quality` before trusting anything. It checks for the fingerprints of
estimation:

```
  408 counts total
  ending in 0 or 5: 100.0%   (chance would be ~20%)
    ^ heavy rounding -- treat these as estimates, not headcounts
  identical to previous count:  17.5% of updates
  counts entered per hour of day:
     6:00     3  ##
    18:00    24  ####################
```

If nearly every count ends in 0 or 5, nobody is counting individual people —
they're eyeballing to the nearest five, and a reading of 40 might really be 34 or
46. If a high share of updates are identical to the previous one, numbers are
being carried forward rather than re-counted. Both are fine for "packed or dead,"
useless for "38% or 45% full."

The heatmap flags its own thin spots:

```
Colby Fitness  (cap 380, 136 counts, avg 34% full)
        6   7   8   9  10  11  12  13  14  15  16  17  18  19  20  21  22
     ---------------------------------------------------------------------
Mon    17  16  12  16  12  12  26  30  37  48  59  66  78  68  57  51  42
Sat     4  10  10  10   4  12  10  20  26  33  32  34  46  38  37  24  29
     (7 slots with no count ('.'); 102 slots resting on a single count)
```

That last line is the important one. After a week most cells rest on a single
hand-typed estimate. The broad shape — mornings quiet, 6pm brutal, weekends
calmer — will be real. Any specific cell will not be.

## Caveats beyond the data collection

- Late August runs much busier than mid-semester. Treat week one as an upper
  bound, and expect football Saturdays, breaks, and finals to distort things.
- Capacities are RecWell's own and some are generous (Colby Fitness at 380). Half
  of a generous capacity can still mean queuing for a squat rack.
- Small rooms are noisy: Climbing Cave has capacity 15, so one group of friends
  swings it 20 points. Trust the big spaces' curves.

## Files

- `preflight.sh` — check a Linux host is suitable before scheduling
- `scrape.py` — one poll, appends only new counts, logs to `scrape.log`
- `analyze.py` — heatmaps, quiet times, cadence, quality audit
- `merge.py` — combine CSVs from multiple collectors, deduped
- `.github/workflows/scrape.yml` — always-on collection via GitHub Actions
- `occupancy.csv` — created on first run; one row per distinct count
