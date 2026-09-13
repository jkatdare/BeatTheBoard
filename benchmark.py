"""
Head-to-head baseline: our engine vs Boxcar Signal vs the official board.

    python benchmark.py            # collect (run for days)
    python benchmark.py --report   # score whatever has been collected

Records, for every train-day at NY Penn:
  * the FIRST prediction our engine would have emitted, and when
  * the FIRST prediction boxcarsignal.io emitted, and when
  * the actual track, and when the official board posted it

Then scores coverage, top-1 accuracy, platform accuracy, lead time over the
board, and flip rate for both predictors -- plus a head-to-head on the subset
where both committed to an answer.

Design notes
------------
* Prediction logic is imported from engine.py so the benchmark scores exactly
  what the web app would show. Both signals are used: circuit (getVehicleData
  ICS_TRACK_CKT) first, berth coordinate second.
* Writes to its own benchmark.db. Safe to run alongside run_logger.py.
* The codebooks are FROZEN at startup from codebook.json. Letting them grow
  during the run would mean scoring a predictor that improves mid-benchmark.
  Rebuild deliberately (engine.py --rebuild) between runs.
* Boxcar's API is public and unauthenticated. Polled once a minute with an
  identifying User-Agent. Personal comparison only -- do not redistribute.
* Self-healing: every network error is caught and backed off. Ctrl-C to stop.
"""

import argparse
import json
import math
import os
import sqlite3
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import njt_logger as njt
import engine

DB = "benchmark.db"
BOXCAR_URL = "https://boxcarsignal.io/api/departures"
UA = "penn-track-benchmark/1.1 (personal accuracy comparison)"

NJT_EVERY = 30       # seconds
BOXCAR_EVERY = 60    # seconds -- be polite to someone else's server
SERVICE_DAY_CUTOFF_HOUR = 3

DDL = """
CREATE TABLE IF NOT EXISTS obs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    seen_at      TEXT NOT NULL,      -- UTC ISO
    source       TEXT NOT NULL,      -- 'njt' | 'boxcar'
    service_date TEXT NOT NULL,
    train_id     TEXT NOT NULL,
    line         TEXT,
    destination  TEXT,
    sched_dep    TEXT,
    track        TEXT,               -- official OR predicted, see is_official
    is_official  INTEGER,            -- 1 = posted on the real board
    region       TEXT,               -- boxcar candidate set, JSON list
    tier         TEXT,               -- ours: official | predicted/circuit | predicted/berth | none
    confidence   REAL,
    raw          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_key ON obs (service_date, train_id, source);
CREATE INDEX IF NOT EXISTS idx_obs_seen ON obs (seen_at);
"""


def service_date_for(dt):
    if dt.hour < SERVICE_DAY_CUTOFF_HOUR:
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def connect():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    return conn


def platform_of(track):
    try:
        t = int(str(track).strip())
    except (TypeError, ValueError):
        return None
    return math.ceil(t / 2) if t <= 16 else None


# ------------------------------------------------------------------ collectors

def poll_njt(conn, token, books):
    circuits = engine.fetch_circuits(token)
    payload = njt.api_post("getTrainSchedule",
                           {"token": token, "station": njt.STATION})
    seen_at = datetime.now(timezone.utc).isoformat()
    svc = service_date_for(datetime.now())
    n_pred = 0
    for item in njt.board_items(payload):
        train_id = str(item.get("TRAIN_ID") or "").strip()
        if not train_id:
            continue
        p = engine.predict(item, books, circuits, {})   # no history tier here
        is_official = 1 if p["tier"] == "official" else 0
        track = p["track"] if p["tier"] in ("official", "predicted") else None
        tier = p["tier"] + ("/" + p["signal"] if p.get("signal") else "")
        if p["tier"] == "predicted":
            n_pred += 1
        item = dict(item)
        item["_circuit"] = circuits.get(train_id)
        conn.execute(
            "INSERT INTO obs (seen_at, source, service_date, train_id, line, "
            "destination, sched_dep, track, is_official, region, tier, confidence, raw) "
            "VALUES (?,'njt',?,?,?,?,?,?,?,NULL,?,?,?)",
            (seen_at, svc, train_id, item.get("LINE"), item.get("DESTINATION"),
             item.get("SCHED_DEP_DATE"), track, is_official, tier, p["confidence"],
             json.dumps(item, separators=(",", ":"))))
    conn.commit()
    return n_pred


def poll_boxcar(conn):
    req = urllib.request.Request(BOXCAR_URL, headers={
        "User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    seen_at = datetime.now(timezone.utc).isoformat()
    svc = service_date_for(datetime.now())
    n = 0
    for d in payload.get("departures", []):
        train_id = str(d.get("train_id") or "")
        if not train_id:
            continue
        region = d.get("track_region")
        conn.execute(
            "INSERT INTO obs (seen_at, source, service_date, train_id, line, "
            "destination, sched_dep, track, is_official, region, tier, confidence, raw) "
            "VALUES (?,'boxcar',?,?,?,?,?,?,?,?,?,NULL,?)",
            (seen_at, svc, train_id, d.get("line"), d.get("destination"),
             str(d.get("departure_time") or ""),
             str(d["track"]) if d.get("track") is not None else None,
             1 if d.get("is_official") else 0,
             json.dumps(region) if region else None,
             "official" if d.get("is_official") else
             ("predicted" if d.get("track") is not None else
              ("region" if region else "none")),
             json.dumps(d, separators=(",", ":"))))
        n += 1
    conn.commit()
    return n


def collect():
    njt.load_env()
    books = engine.load_codebooks()
    print("codebooks frozen: %d berth coords, %d circuits"
          % (len(books["gps"]), len(books["circuits"])))
    conn = connect()
    token = njt.get_token()

    next_njt = next_boxcar = 0.0
    errors = 0
    print("collecting -> %s   (Ctrl-C to stop)\n" % DB)
    while True:
        now = time.time()
        try:
            if now >= next_njt:
                n = poll_njt(conn, token, books)
                next_njt = now + NJT_EVERY
                total = conn.execute("SELECT COUNT(*) FROM obs").fetchone()[0]
                print("[%s] njt ok (%d live predictions) | %d rows"
                      % (datetime.now().strftime("%H:%M:%S"), n, total))
                errors = 0
        except njt.AuthError:
            print("  re-minting token")
            try:
                token = njt.get_token(force=True)
            except Exception as e:
                errors += 1
                print("  re-auth failed: %s" % e)
        except Exception as e:
            errors += 1
            print("  njt error: %s: %s" % (type(e).__name__, e))
            next_njt = time.time() + NJT_EVERY * min(2 ** errors, 20)

        try:
            if time.time() >= next_boxcar:
                poll_boxcar(conn)
                next_boxcar = time.time() + BOXCAR_EVERY
        except Exception as e:
            print("  boxcar error: %s: %s" % (type(e).__name__, e))
            next_boxcar = time.time() + BOXCAR_EVERY * 5

        time.sleep(2)


# --------------------------------------------------------------------- scoring

def report(since=None):
    """since: 'YYYY-MM-DD' -- score only service days on/after it. Use this to
    get a clean window after a codebook or code change, since rows collected
    earlier reflect the older predictor."""
    if not os.path.exists(DB):
        sys.exit("No %s yet. Run the collector first." % DB)
    conn = connect()
    conn.row_factory = sqlite3.Row

    truth, posted_at = {}, {}
    for r in conn.execute(
            "SELECT service_date, train_id, track, seen_at FROM obs "
            "WHERE source='njt' AND is_official=1 AND track IS NOT NULL "
            "ORDER BY seen_at"):
        if since and r["service_date"] < since:
            continue
        k = (r["service_date"], r["train_id"])
        truth.setdefault(k, str(r["track"]).strip())
        posted_at.setdefault(k, r["seen_at"])

    first = defaultdict(dict)
    allpreds = defaultdict(lambda: defaultdict(list))
    regions = {}
    signal_of = {}
    for r in conn.execute(
            "SELECT source, service_date, train_id, track, is_official, region, tier, seen_at "
            "FROM obs WHERE is_official=0 ORDER BY seen_at"):
        k = (r["service_date"], r["train_id"])
        if r["track"]:
            if k not in first[r["source"]]:
                first[r["source"]][k] = (str(r["track"]).strip(), r["seen_at"])
                if r["source"] == "njt":
                    signal_of[k] = (r["tier"] or "").split("/")[-1]
            allpreds[r["source"]][k].append(str(r["track"]).strip())
        elif r["region"] and r["source"] == "boxcar":
            regions.setdefault(k, (json.loads(r["region"]), r["seen_at"]))

    days = sorted({k[0] for k in truth})
    print("=" * 74)
    print("BASELINE  --  %d train-days with a known track over %d service days"
          % (len(truth), len(days)))
    if days:
        print("            %s .. %s" % (days[0], days[-1]))
    print("=" * 74)

    def score(source, label):
        preds = first[source]
        keys = [k for k in preds if k in truth]
        if not keys:
            print("\n%-16s no overlapping predictions yet" % label)
            return None
        hit = sum(1 for k in keys if preds[k][0] == truth[k])
        phit = sum(1 for k in keys
                   if platform_of(preds[k][0]) is not None
                   and platform_of(preds[k][0]) == platform_of(truth[k]))
        leads = []
        for k in keys:
            if k in posted_at:
                try:
                    leads.append((datetime.fromisoformat(posted_at[k])
                                  - datetime.fromisoformat(preds[k][1])).total_seconds() / 60)
                except ValueError:
                    pass
        leads = sorted(x for x in leads if x > -1)
        flips = sum(1 for k in keys if len(set(allpreds[source][k])) > 1)
        cov = 100.0 * len(keys) / len(truth)
        print("\n%s" % label)
        print("  coverage        %5.1f%%   (%d of %d train-days)" % (cov, len(keys), len(truth)))
        print("  track accuracy  %5.1f%%   (%d/%d)" % (100.0 * hit / len(keys), hit, len(keys)))
        print("  platform acc.   %5.1f%%" % (100.0 * phit / len(keys)))
        print("  flip rate       %5.1f%%   (changed its mind before posting)"
              % (100.0 * flips / len(keys)))
        if leads:
            print("  lead over board  %.1f min median  (p25 %.1f, p75 %.1f)"
                  % (leads[len(leads) // 2], leads[len(leads) // 4], leads[3 * len(leads) // 4]))
        print("  expected value  %5.1f%%   (coverage x accuracy)"
              % (cov * 100.0 * hit / len(keys) / 100))
        return preds

    ours = score("njt", "OUR ENGINE (circuit + berth decode)")
    if ours:
        by_sig = Counter(signal_of.get(k, "?") for k in ours if k in truth)
        acc_sig = {}
        for k in ours:
            if k in truth:
                s = signal_of.get(k, "?")
                acc_sig.setdefault(s, [0, 0])
                acc_sig[s][1] += 1
                acc_sig[s][0] += (ours[k][0] == truth[k])
        print("  by signal:      " + "   ".join(
            "%s %d (%.1f%%)" % (s, n, 100.0 * acc_sig[s][0] / acc_sig[s][1])
            for s, n in by_sig.most_common()))

    theirs = score("boxcar", "BOXCAR SIGNAL (point estimates)")

    rkeys = [k for k in regions if k in truth]
    if rkeys:
        contained = sum(1 for k in rkeys if truth[k] in [str(x) for x in regions[k][0]])
        sizes = sorted(len(regions[k][0]) for k in rkeys)
        print("\nBOXCAR SIGNAL (candidate sets)")
        print("  coverage        %5.1f%%   (%d train-days)"
              % (100.0 * len(rkeys) / len(truth), len(rkeys)))
        print("  containment     %5.1f%%   (actual track was inside the set)"
              % (100.0 * contained / len(rkeys)))
        print("  median set size %d tracks" % sizes[len(sizes) // 2])

    if ours and theirs:
        both = [k for k in ours if k in theirs and k in truth]
        if both:
            o = sum(1 for k in both if ours[k][0] == truth[k])
            t = sum(1 for k in both if theirs[k][0] == truth[k])
            print("\n" + "-" * 74)
            print("HEAD TO HEAD on %d train-days where BOTH committed" % len(both))
            print("  ours   %d/%d = %.1f%%" % (o, len(both), 100.0 * o / len(both)))
            print("  boxcar %d/%d = %.1f%%" % (t, len(both), 100.0 * t / len(both)))
            oe = sum(1 for k in both
                     if datetime.fromisoformat(ours[k][1]) < datetime.fromisoformat(theirs[k][1]))
            print("  we predicted first on %d of %d (%.0f%%)"
                  % (oe, len(both), 100.0 * oe / len(both)))
        only_ours = [k for k in ours if k not in theirs and k in truth]
        only_theirs = [k for k in theirs if k not in ours and k in truth]
        print("\n  only we predicted    : %d" % len(only_ours))
        print("  only boxcar predicted: %d" % len(only_theirs))
        if only_theirs:
            acc = sum(1 for k in only_theirs if theirs[k][0] == truth[k])
            print("     ...and they were right %d/%d = %.0f%%  <-- our coverage gap"
                  % (acc, len(only_theirs), 100.0 * acc / len(only_theirs)))
            print("     tracks: %s" % dict(Counter(truth[k] for k in only_theirs).most_common(8)))
    print("\n" + "=" * 74)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="score, do not collect")
    ap.add_argument("--since", default=None, help="with --report: only service days >= YYYY-MM-DD")
    args = ap.parse_args()
    if args.report:
        report(since=args.since)
    else:
        try:
            collect()
        except KeyboardInterrupt:
            print("\nstopped -- run  python benchmark.py --report  to score")


if __name__ == "__main__":
    main()
