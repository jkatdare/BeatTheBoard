"""
Analyse whatever the logger has collected so far.

    python analyze.py

Answers, in order:
  1. how much data do we have
  2. how long before departure does the board actually post a track
  3. the per-train track distribution -- i.e. the lookup table itself
  4. baseline accuracy of "guess this train's most common track",
     scored at TRACK level and at PLATFORM level (ceil(track/2))
  5. whether GPS presence/proximity anticipates the posting

Read-only. Safe to run while the logger is going.
"""

import io
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB = "track_history.db"
NYP_LAT, NYP_LON = 40.7498, -73.9918

# Tracks 17-21 are not reachable for an NJT revenue departure; 20/21 only reach
# West Side Yard via KN interlocking. Kept here as the feasibility mask.
NJT_FEASIBLE = set(range(1, 17))


def rule(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def platform_of(track):
    """Tracks 13 and 14 share an island. Predicting the platform is worth ~10pts
    of accuracy for free, and puts the rider in the right place either way."""
    try:
        return math.ceil(int(str(track).strip()) / 2)
    except (TypeError, ValueError):
        return None


def km_from_penn(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    dy = (lat - NYP_LAT) * 111.0
    dx = (lon - NYP_LON) * 111.0 * math.cos(math.radians(NYP_LAT))
    return math.hypot(dx, dy)


def pctl(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# ---------------------------------------------------------------- 1. coverage
rule("1. COVERAGE")
obs = conn.execute("SELECT COUNT(*) c, COUNT(DISTINCT service_date) d, "
                   "COUNT(DISTINCT train_id) t, MIN(polled_at) a, MAX(polled_at) b "
                   "FROM observations").fetchone()
post = conn.execute("SELECT COUNT(*) FROM track_postings").fetchone()[0]
veh = conn.execute("SELECT COUNT(*) FROM vehicle_positions").fetchone()[0]
print("observations      %s" % obs["c"])
print("service days      %s" % obs["d"])
print("distinct trains   %s" % obs["t"])
print("track postings    %s   <-- labelled examples; this is the number that matters"
      % post)
print("vehicle rows      %s" % veh)
print("window            %s  ->  %s" % (obs["a"], obs["b"]))

if post < 30:
    print("\n** Too early for meaningful stats. Let the logger run through at least")
    print("   one full service day, ideally 2-3 weeks, then re-run this. **")

# --------------------------------------------------------- 2. posting lead time
rule("2. HOW EARLY DOES THE BOARD POST?")
leads = [r[0] / 60.0 for r in conn.execute(
    "SELECT lead_seconds FROM track_postings WHERE lead_seconds IS NOT NULL "
    "AND lead_seconds BETWEEN -600 AND 7200")]
if leads:
    print("n=%d   median %.1f min   p10 %.1f   p90 %.1f   max %.1f"
          % (len(leads), pctl(leads, .5), pctl(leads, .1),
             pctl(leads, .9), max(leads)))
    print("\nThis is the number to beat. Any prediction that lands earlier than")
    print("the median, and does not later flip, is genuine value.")
else:
    print("no lead-time data yet")

# ------------------------------------------------- 3+4. the lookup table itself
rule("3. PER-TRAIN TRACK DISTRIBUTION  (the model, basically)")
by_train = defaultdict(list)
for r in conn.execute("SELECT train_id, track, service_date FROM track_postings"):
    if r["track"] and str(r["track"]).strip().isdigit():
        by_train[r["train_id"]].append(int(r["track"]))

multi = {t: v for t, v in by_train.items() if len(v) >= 2}
print("trains with >=2 observations: %d  (of %d seen)" % (len(multi), len(by_train)))

if multi:
    print("\nTRAIN   N   TRACKS SEEN                     MODE  MODE%   PLATFORMS")
    print("-" * 72)
    for t, tracks in sorted(multi.items(), key=lambda kv: -len(kv[1]))[:25]:
        c = Counter(tracks)
        mode, n = c.most_common(1)[0]
        plats = Counter(platform_of(x) for x in tracks)
        seen = " ".join("%s:%d" % kv for kv in sorted(c.items()))
        print("%-7s %-3d %-30s %-5s %5.0f%%  %s"
              % (t, len(tracks), seen[:30], mode, 100.0 * n / len(tracks),
                 " ".join("P%s:%d" % kv for kv in sorted(plats.items()))))

rule("4. BASELINE: 'guess this train's most common track'")
print("Leave-one-out over every labelled departure, so a train with a single")
print("observation contributes nothing and cannot inflate the score.\n")
hit_t = tot_t = hit_p = tot_p = 0
for t, tracks in by_train.items():
    for i, actual in enumerate(tracks):
        others = tracks[:i] + tracks[i + 1:]
        if not others:
            continue
        guess = Counter(others).most_common(1)[0][0]
        tot_t += 1
        hit_t += (guess == actual)
        gp, ap = platform_of(guess), platform_of(actual)
        if gp and ap:
            tot_p += 1
            hit_p += (gp == ap)
if tot_t:
    print("  TRACK-level    %4d/%-4d = %5.1f%%" % (hit_t, tot_t, 100.0 * hit_t / tot_t))
    print("  PLATFORM-level %4d/%-4d = %5.1f%%   <-- what the rider experiences"
          % (hit_p, tot_p, 100.0 * hit_p / tot_p))
    if tot_p:
        print("\n  platform - track = %+.1f pts, free."
              % (100.0 * hit_p / tot_p - 100.0 * hit_t / tot_t))
else:
    print("  not enough repeat observations yet")

# --------------------------------------------------- 5. does GPS anticipate it?
rule("5. IS GPS A BERTH SIGNAL?")
print("For each train-day, compare distance-from-Penn while the track was still")
print("unposted vs. after it posted. If the equipment is detectably at Penn")
print("BEFORE the board commits, that is a real head start.\n")
near_unposted = far_unposted = 0
gps_rows = 0
for r in conn.execute("SELECT track, raw FROM observations"):
    try:
        item = json.loads(r["raw"])
    except (json.JSONDecodeError, TypeError):
        continue
    d = km_from_penn(item.get("GPSLATITUDE"), item.get("GPSLONGITUDE"))
    if d is None:
        continue
    gps_rows += 1
    if not (r["track"] or "").strip():
        if d < 0.5:
            near_unposted += 1
        else:
            far_unposted += 1
total_rows = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
print("rows carrying GPS at all: %d / %d (%.0f%%)"
      % (gps_rows, total_rows, 100.0 * gps_rows / max(total_rows, 1)))
print("unposted AND within 500m of Penn : %d   <-- berthed, track not yet announced"
      % near_unposted)
print("unposted AND further than 500m   : %d" % far_unposted)
if near_unposted:
    print("\nThose are the opportunities. Next step is figuring out WHICH track a")
    print("berthed consist is on -- GPS alone cannot resolve ~10m track spacing,")
    print("but arrival-track data or the turn map might.")

print("\n" + "=" * 72)
print("Re-run after a few days. Section 4 is the one to watch: if the platform")
print("baseline clears ~70%%, the simple lookup approach is already a product.")
print("=" * 72)
