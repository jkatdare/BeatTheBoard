"""
Classify NY Penn departures as TURN vs LOAD-AND-GO using the GTFS schedule,
then test whether that classification predicts anything useful.

    python turn_match.py

Why this is not what it first looks like
----------------------------------------
The original hope was that a turning train inherits its arrival track, so
decoding the inbound would give the outbound track for free. That is dead:
getVehicleData reports a single station-centroid coordinate (40.750048,
-73.992358) for every train at Penn, and NJT publishes no arrival track
anywhere. The inbound track is simply not observable.

What movement type IS still good for:
  1. It gates the hard physical constraints. A drop-and-go continuing east to
     Sunnyside cannot use the stub tracks 1-4.
  2. It is a plausible conditioning variable. A turn's equipment is already
     berthed and sitting, so it should show a GPS berth coordinate earlier and
     more often than a load-and-go that arrives from the yard at the last
     minute. If true, that explains where the missing GPS coverage lives.
  3. It may split the weak 20% historical prior into a predictable subset and
     an unpredictable one.

This script measures all three rather than assuming any of them.

Turn window from the Tri-Venture Council dwell table (Amtrak/LIRR/NJT):
NJT scheduled turn dwell 22 min, minimum 18. We allow 15-75 min.
"""

import csv
import io
import json
import math
import sqlite3
import sys
import zipfile
from collections import Counter, defaultdict

# reconfigure in place rather than wrapping: a fresh TextIOWrapper closes the
# shared buffer when it is garbage-collected, which kills stdout for any
# script that imports this module.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

GTFS_ZIP = "njt_gtfs_rail.zip"
DB = "track_history.db"
NYP_STOP = "109"
TURN_MIN_MIN, TURN_MAX_MIN = 15, 75
NYP_LAT, NYP_LON = 40.7498, -73.9918


def hms_to_min(t):
    """GTFS times can exceed 24:00:00."""
    try:
        h, m, s = (int(x) for x in t.split(":"))
        return h * 60 + m + s / 60.0
    except (ValueError, AttributeError):
        return None


def load_gtfs():
    zf = zipfile.ZipFile(GTFS_ZIP)

    def tab(name):
        return list(csv.DictReader(io.StringIO(
            zf.read(name).decode("utf-8-sig", "replace"))))

    trips = tab("trips.txt")
    times = tab("stop_times.txt")
    try:
        caldates = tab("calendar_dates.txt")
    except KeyError:
        caldates = []
    zf.close()

    seq = defaultdict(list)
    for st in times:
        seq[st["trip_id"]].append(st)
    for v in seq.values():
        v.sort(key=lambda x: int(x.get("stop_sequence", 0)))

    # service_id -> set of YYYY-MM-DD it runs
    svc = defaultdict(set)
    for r in caldates:
        d = r.get("date", "")
        if len(d) == 8 and r.get("exception_type") == "1":
            svc[r["service_id"]].add(d[:4] + "-" + d[4:6] + "-" + d[6:])
    return trips, seq, svc


def build_turn_map(trips, seq, svc, service_dates):
    """-> {(service_date, outbound_train): inbound_train}"""
    arrivals, departures = defaultdict(list), defaultdict(list)
    for t in trips:
        s = seq.get(t["trip_id"])
        if not s:
            continue
        name = (t.get("trip_short_name") or "").strip()
        route = t.get("route_id")
        dates = svc.get(t.get("service_id"), set()) or service_dates
        if s[-1]["stop_id"] == NYP_STOP:
            m = hms_to_min(s[-1].get("arrival_time"))
            if m is not None:
                for d in dates & service_dates:
                    arrivals[d].append((m, name, route))
        if s[0]["stop_id"] == NYP_STOP:
            m = hms_to_min(s[0].get("departure_time"))
            if m is not None:
                for d in dates & service_dates:
                    departures[d].append((m, name, route))

    turn_of = {}
    for d, deps in departures.items():
        arrs = sorted(arrivals.get(d, []))
        used = set()
        for dep_m, dep_name, dep_route in sorted(deps):
            best = None
            for i, (arr_m, arr_name, arr_route) in enumerate(arrs):
                if i in used:
                    continue
                gap = dep_m - arr_m
                if TURN_MIN_MIN <= gap <= TURN_MAX_MIN:
                    # same route first; otherwise nearest in time
                    score = (0 if arr_route == dep_route else 1, gap)
                    if best is None or score < best[0]:
                        best = (score, i, arr_name)
            if best:
                used.add(best[1])
                turn_of[(d, dep_name)] = best[2]
    return turn_of, arrivals, departures


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    service_dates = {r[0] for r in conn.execute(
        "SELECT DISTINCT service_date FROM observations WHERE service_date IS NOT NULL")}
    print("logged service days: " + ", ".join(sorted(service_dates)))

    trips, seq, svc = load_gtfs()
    turn_of, arrivals, departures = build_turn_map(trips, seq, svc, service_dates)
    print("GTFS trips %d | NYP arrivals/day ~%d | NYP departures/day ~%d"
          % (len(trips),
             sum(len(v) for v in arrivals.values()) // max(len(arrivals), 1),
             sum(len(v) for v in departures.values()) // max(len(departures), 1)))
    print("turn pairs matched: %d" % len(turn_of))

    # ---- per train-day observations -------------------------------------
    seqobs = defaultdict(list)
    for r in conn.execute("SELECT train_id, service_date, track, polled_at, line, raw "
                          "FROM observations ORDER BY polled_at"):
        try:
            it = json.loads(r["raw"])
        except (json.JSONDecodeError, TypeError):
            continue
        la, lo = it.get("GPSLATITUDE"), it.get("GPSLONGITUDE")
        gps = (str(la), str(lo)) if la and lo else None
        seqobs[(r["train_id"], r["service_date"])].append(
            ((r["track"] or "").strip(), gps, r["polled_at"], r["line"]))

    # codebook from posted rows near Penn
    votes = defaultdict(Counter)
    for obs in seqobs.values():
        for trk, gps, _, _ in obs:
            if trk.isdigit() and gps:
                try:
                    if (abs(float(gps[0]) - NYP_LAT) < 0.006
                            and abs(float(gps[1]) + 73.9918) < 0.006):
                        votes[gps][trk] += 1
                except ValueError:
                    pass
    book = {g: c.most_common(1)[0][0] for g, c in votes.items()
            if c.most_common(1)[0][1] == sum(c.values()) and sum(c.values()) >= 2}

    # ---- split by movement type -----------------------------------------
    stats = {"TURN": defaultdict(int), "LOAD-AND-GO": defaultdict(int)}
    hist = defaultdict(lambda: defaultdict(Counter))   # class -> train -> tracks
    matched_any = 0

    for (train, day), obs in seqobs.items():
        final = next((t for t, _, _, _ in obs if t.isdigit()), None)
        if not final:
            continue
        cls = "TURN" if (day, train) in turn_of else "LOAD-AND-GO"
        if (day, train) in turn_of:
            matched_any += 1
        s = stats[cls]
        s["n"] += 1
        hist[cls][train][final] += 1

        preds = [book[g] for t, g, _, _ in obs if not t and g and g in book]
        if preds:
            s["gps_cov"] += 1
            s["gps_hit"] += (preds[0] == final)

    print("\n" + "=" * 68)
    print("DOES MOVEMENT TYPE PREDICT ANYTHING?")
    print("=" * 68)
    print("%-14s %6s %10s %10s" % ("CLASS", "N", "GPS COV", "GPS ACC"))
    print("-" * 68)
    for cls in ("TURN", "LOAD-AND-GO"):
        s = stats[cls]
        if not s["n"]:
            continue
        cov = 100.0 * s["gps_cov"] / s["n"]
        acc = 100.0 * s["gps_hit"] / s["gps_cov"] if s["gps_cov"] else 0
        print("%-14s %6d %9.0f%% %9.0f%%" % (cls, s["n"], cov, acc))

    # ---- historical stability per class ---------------------------------
    print("\n" + "=" * 68)
    print("IS THE HISTORICAL PRIOR STRONGER FOR TURNS?")
    print("=" * 68)
    for cls in ("TURN", "LOAD-AND-GO"):
        hit = tot = phit = 0
        for train, cnt in hist[cls].items():
            tracks = [t for t, n in cnt.items() for _ in range(n)]
            if len(tracks) < 2:
                continue
            for i, actual in enumerate(tracks):
                others = tracks[:i] + tracks[i + 1:]
                guess = Counter(others).most_common(1)[0][0]
                tot += 1
                hit += (guess == actual)
                try:
                    phit += (math.ceil(int(guess) / 2) == math.ceil(int(actual) / 2))
                except ValueError:
                    pass
        if tot:
            print("%-14s track %4.0f%%   platform %4.0f%%   (n=%d)"
                  % (cls, 100.0 * hit / tot, 100.0 * phit / tot, tot))
        else:
            print("%-14s not enough repeat observations" % cls)

    print("\n" + "=" * 68)
    print("READ THIS AS:")
    print("  If TURN shows materially higher GPS coverage, the missing 68% is")
    print("  concentrated in load-and-go trains that arrive from the yard late,")
    print("  and the ceiling on the GPS tier is structural, not a codebook gap.")
    print("  If the historical prior is much stronger for turns, condition the")
    print("  frequency table on movement type -- it is a free split.")
    print("=" * 68)


if __name__ == "__main__":
    main()
