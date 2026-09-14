"""
Score the DEPLOYED app against the real board.

    python check.py                 # poll the web app every 30s, record outcomes
    python check.py --report        # scorecard
    python check.py --every 240     # poll every 4 min instead (lets the app sleep)

What it measures
  1. Are the predictions right?  For each train, the first prediction the app
     showed is remembered; when the board posts the real track, it is scored.
  2. How much of the board are we predicting?  Every poll records how many
     trains were official / predicted / unpredicted, so the report can say
     "of the trains not yet posted, X% had a prediction" -- overall and by hour.
  3. Is the app healthy?  API errors and cold-start latency are logged too.

This reads only https://<app>/api/board. No NJT credentials needed, so it can
run from any machine. One row per train-day plus one per poll in check.db.

Cost note: while this polls every 30s the container never idles, so it never
scales to zero. That is fine for a test window; for weeks at a time use
--every 240 or more, at the price of coarser lead-time measurement.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

APP_URL = os.environ.get(
    "BOARD_URL",
    "https://beattheboard.thankfulpond-632cee48.eastus2.azurecontainerapps.io").rstrip("/")
DB = "check.db"
SERVICE_DAY_CUTOFF_HOUR = 3        # trains after midnight belong to the prior day

DDL = """
CREATE TABLE IF NOT EXISTS results (
    service_date TEXT NOT NULL,
    train_id     TEXT NOT NULL,
    operator     TEXT,
    line         TEXT,
    destination  TEXT,
    sched_dep    TEXT,
    predicted    TEXT,          -- first prediction shown (NULL = never predicted)
    signal       TEXT,          -- circuit | berth
    confidence   REAL,
    predicted_at TEXT,          -- UTC ISO
    flipped      INTEGER DEFAULT 0,
    actual       TEXT,          -- official track (NULL = not posted yet)
    posted_at    TEXT,
    sched_epoch  INTEGER,       -- scheduled departure, UTC epoch seconds
    PRIMARY KEY (service_date, train_id)
);
CREATE TABLE IF NOT EXISTS polls (
    seen_at      TEXT NOT NULL,
    n_trains     INTEGER,
    n_official   INTEGER,
    n_predicted  INTEGER,
    n_unposted   INTEGER,       -- on the board, no official track, no prediction
    api_error    TEXT,
    age          INTEGER,       -- seconds since the app last fetched from NJT
    latency_ms   INTEGER
);
"""


def connect():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    # A check.db left by the earlier credential-based checker lacks this
    # column, and CREATE TABLE IF NOT EXISTS will not add it.
    for col in ("operator TEXT", "sched_epoch INTEGER"):
        try:
            conn.execute("ALTER TABLE results ADD COLUMN " + col)
        except sqlite3.OperationalError:
            pass
    return conn


def say(msg):
    print(msg, flush=True)


def local_now():
    """Eastern time if the tz database is available, else the machine clock."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now()


def service_date(dt):
    if dt.hour < SERVICE_DAY_CUTOFF_HOUR:
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def minutes(a, b):
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def fetch_board():
    req = urllib.request.Request(APP_URL + "/api/board",
                                 headers={"User-Agent": "beattheboard-check/2.0"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=90) as resp:      # 90s: cold start
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return data, int((time.time() - t0) * 1000)


# ------------------------------------------------------------------ collect

def poll(conn):
    data, latency = fetch_board()
    now = datetime.now(timezone.utc).isoformat()
    svc = service_date(local_now())
    board, err = data.get("board"), data.get("error")
    trains = board["trains"] if board else []

    n_off = sum(1 for t in trains if t["prediction"]["tier"] in ("official", "verified"))
    n_pred = sum(1 for t in trains if t["prediction"]["tier"] == "predicted")
    n_unp = len(trains) - n_off - n_pred
    conn.execute("INSERT INTO polls VALUES (?,?,?,?,?,?,?,?)",
                 (now, len(trains), n_off, n_pred, n_unp, err, data.get("age"), latency))

    resolved = []
    for t in trains:
        tid = str(t.get("train") or "").strip()
        p = t.get("prediction") or {}
        if not tid:
            continue
        row = conn.execute("SELECT predicted, actual FROM results "
                           "WHERE service_date=? AND train_id=?", (svc, tid)).fetchone()
        meta = (t.get("operator"), t.get("line"), t.get("destination"), t.get("depart"))
        sched_epoch = t.get("depart_epoch")
        if sched_epoch is None and t.get("minutes") is not None:
            # older app without depart_epoch: the countdown was computed at the
            # app's fetch time, which is `age` seconds before this poll
            sched_epoch = int(time.time()) - int(data.get("age") or 0) + int(t["minutes"]) * 60

        if p.get("tier") in ("official", "verified"):   # both are NJT's posted track
            if row is None:
                conn.execute("INSERT INTO results (service_date, train_id, operator, line, "
                             "destination, sched_dep, actual, posted_at, sched_epoch) "
                             "VALUES (?,?,?,?,?,?,?,?,?)",
                             (svc, tid) + meta + (p["track"], now, sched_epoch))
            elif row[1] is None:
                conn.execute("UPDATE results SET actual=?, posted_at=? "
                             "WHERE service_date=? AND train_id=?", (p["track"], now, svc, tid))
                if row[0]:
                    resolved.append((tid, row[0], p["track"]))

        elif p.get("tier") == "predicted":
            if row is None:
                conn.execute("INSERT INTO results (service_date, train_id, operator, line, "
                             "destination, sched_dep, predicted, signal, confidence, predicted_at, "
                             "sched_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (svc, tid) + meta + (p["track"], p.get("signal"),
                                                  p.get("confidence"), now, sched_epoch))
            elif row[0] is None and row[1] is None:
                conn.execute("UPDATE results SET predicted=?, signal=?, confidence=?, "
                             "predicted_at=? WHERE service_date=? AND train_id=?",
                             (p["track"], p.get("signal"), p.get("confidence"), now, svc, tid))
            elif row[0] and row[1] is None and row[0] != p["track"]:
                conn.execute("UPDATE results SET flipped=1 "
                             "WHERE service_date=? AND train_id=?", (svc, tid))
        if sched_epoch is not None:
            conn.execute("UPDATE results SET sched_epoch=? WHERE service_date=? "
                         "AND train_id=? AND sched_epoch IS NULL", (sched_epoch, svc, tid))
    conn.commit()

    for tid, pred, actual in resolved:
        r = conn.execute("SELECT signal, predicted_at, posted_at FROM results "
                         "WHERE service_date=? AND train_id=?", (svc, tid)).fetchone()
        lead = minutes(r[1], r[2]) or 0
        say("   %s  %-6s predicted %-3s actual %-3s %s (%s, %.1f min early)"
            % ("HIT " if pred == actual else "MISS", tid, pred, actual,
               "   " if pred == actual else "<--", r[0], lead))

    return len(trains), n_off, n_pred, n_unp, err, latency


def scoreline(conn):
    n_res = conn.execute("SELECT COUNT(*) FROM results WHERE actual IS NOT NULL").fetchone()[0]
    n_pred = conn.execute("SELECT COUNT(*) FROM results WHERE actual IS NOT NULL "
                          "AND predicted IS NOT NULL").fetchone()[0]
    n_hit = conn.execute("SELECT COUNT(*) FROM results WHERE actual IS NOT NULL "
                         "AND predicted = actual").fetchone()[0]
    cov = 100.0 * n_pred / n_res if n_res else 0
    acc = 100.0 * n_hit / n_pred if n_pred else 0
    return "resolved %d | predicted %d (%.0f%%) | correct %.1f%%" % (n_res, n_pred, cov, acc)


def collect(every):
    conn = connect()
    say("checking %s every %ds -> %s   (Ctrl-C to stop)\n" % (APP_URL, every, DB))
    errors = 0
    while True:
        try:
            n, off, pred, unp, err, ms = poll(conn)
            board = "board %2d: %2d official, %2d predicted, %2d unpredicted" % (n, off, pred, unp)
            say("[%s] %s | %4dms%s | %s"
                % (datetime.now().strftime("%H:%M:%S"), board, ms,
                   "  APP ERROR: " + err if err else "", scoreline(conn)))
            errors = 0
            time.sleep(every)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            errors += 1
            say("   fetch failed: %s: %s" % (type(e).__name__, e))
            time.sleep(min(every * 2 ** errors, 600))


# ------------------------------------------------------------------- report

def report():
    if not os.path.exists(DB):
        sys.exit("No %s yet. Run  python check.py  first." % DB)
    conn = connect()

    # ---- 1. accuracy on resolved train-days
    rows = conn.execute("SELECT service_date, train_id, operator, line, predicted, signal, "
                        "predicted_at, actual, posted_at, flipped, sched_dep, sched_epoch "
                        "FROM results WHERE actual IS NOT NULL ORDER BY posted_at").fetchall()
    print("=" * 68)
    if not rows:
        print("Nothing resolved yet -- no train has posted since checking began.")
    else:
        days = sorted({r[0] for r in rows})
        pred = [r for r in rows if r[4]]
        hits = [r for r in pred if r[4] == r[7]]
        miss = [r for r in pred if r[4] != r[7]]
        leads = sorted(x for x in (minutes(r[6], r[8]) for r in pred) if x is not None and x > -1)
        print("ARE THE PREDICTIONS RIGHT?   %d train-days, %s .. %s" % (len(rows), days[0], days[-1]))
        print("=" * 68)
        print("coverage   %5.1f%%   (%d of %d departures had a prediction before posting)"
              % (100.0 * len(pred) / len(rows), len(pred), len(rows)))
        print("accuracy   %5.1f%%   (%d right, %d wrong)"
              % (100.0 * len(hits) / max(len(pred), 1), len(hits), len(miss)))
        print("flips      %5.1f%%   (%d changed before posting)"
              % (100.0 * sum(1 for r in pred if r[9]) / max(len(pred), 1), sum(1 for r in pred if r[9])))
        if leads:
            print("lead       median %.1f min   p25 %.1f   p75 %.1f   max %.0f"
                  % (leads[len(leads) // 2], leads[len(leads) // 4], leads[3 * len(leads) // 4], leads[-1]))

        def sched_epoch_of(r):
            if r[11] is not None:
                return r[11]
            s = r[10] or ""
            try:   # rows from before this column, machine-local time
                try:                                   # first checker: "13-Sep-2026 05:43:00 PM"
                    d = datetime.strptime(s, "%d-%b-%Y %I:%M:%S %p")
                except ValueError:                     # app-based checker: "5:43 PM" + service date
                    d = datetime.strptime(r[0] + " " + s, "%Y-%m-%d %I:%M %p")
                    if d.hour < SERVICE_DAY_CUTOFF_HOUR:
                        d += timedelta(days=1)
                return int(d.replace(tzinfo=datetime.now().astimezone().tzinfo).timestamp())
            except ValueError:
                return None

        def epoch(iso):
            try:
                return datetime.fromisoformat(iso).timestamp()
            except (TypeError, ValueError):
                return None

        def q(x, p):
            return x[min(len(x) - 1, int(len(x) * p))]

        board_lead = sorted((se - epoch(r[8])) / 60 for r in rows
                            for se in [sched_epoch_of(r)] if se and epoch(r[8]))
        ours_lead = sorted((se - epoch(r[6])) / 60 for r in pred
                           for se in [sched_epoch_of(r)] if se and epoch(r[6]))
        if board_lead or ours_lead:
            print("\nminutes before scheduled departure:")
        if board_lead:
            print("   NJ Transit posts the track   median %5.1f   p25 %5.1f   p75 %5.1f   (n=%d)"
                  % (q(board_lead, .5), q(board_lead, .25), q(board_lead, .75), len(board_lead)))
        if ours_lead:
            print("   our prediction appears       median %5.1f   p25 %5.1f   p75 %5.1f   (n=%d)"
                  % (q(ours_lead, .5), q(ours_lead, .25), q(ours_lead, .75), len(ours_lead)))
        print("\nby signal:")
        for sig in ("circuit", "berth"):
            s = [r for r in pred if r[5] == sig]
            if s:
                print("   %-8s %4d   %5.1f%% accurate"
                      % (sig, len(s), 100.0 * sum(1 for r in s if r[4] == r[7]) / len(s)))
        print("\nby track (share of departures on that track we predicted):")
        used, got = Counter(r[7] for r in rows), Counter(r[7] for r in pred)
        for t in sorted(used, key=lambda x: int(x) if str(x).isdigit() else 99):
            print("   track %-3s %3d/%-3d %4.0f%%" % (t, got[t], used[t], 100.0 * got[t] / used[t]))
        if miss:
            print("\nmisses:")
            for r in miss:
                print("   %s  %-6s %-18s predicted %-3s actual %-3s (%s)"
                      % (r[0], r[1], (r[3] or "")[:18], r[4], r[7], r[5]))

    # ---- 2. how much of the board are we predicting, live
    polls = conn.execute("SELECT seen_at, n_trains, n_official, n_predicted, n_unposted, "
                         "api_error, latency_ms FROM polls ORDER BY seen_at").fetchall()
    print("\n" + "=" * 68)
    print("HOW MUCH OF THE BOARD ARE WE PREDICTING?   %d polls" % len(polls))
    print("=" * 68)
    if polls:
        open_polls = [p for p in polls if (p[3] + p[4]) > 0]
        tot_pred = sum(p[3] for p in open_polls)
        tot_open = sum(p[3] + p[4] for p in open_polls)
        print("of trains NOT yet posted, had a prediction : %5.1f%%   (%d of %d train-polls)"
              % (100.0 * tot_pred / max(tot_open, 1), tot_pred, tot_open))
        print("average board: %.1f trains = %.1f official + %.1f predicted + %.1f unpredicted"
              % tuple(sum(p[i] for p in polls) / len(polls) for i in (1, 2, 3, 4)))
        by_hour = {}
        for p in polls:
            try:
                h = datetime.fromisoformat(p[0]).astimezone().hour
            except ValueError:
                continue
            b = by_hour.setdefault(h, [0, 0])
            b[0] += p[3]
            b[1] += p[3] + p[4]
        print("\nby local hour (share of unposted trains with a prediction):")
        for h in sorted(by_hour):
            if by_hour[h][1]:
                print("   %02d:00  %5.1f%%  (%d)" % (h, 100.0 * by_hour[h][0] / by_hour[h][1], by_hour[h][1]))
        errs = sum(1 for p in polls if p[5])
        lat = sorted(p[6] for p in polls if p[6] is not None)
        print("\napp health: %d/%d polls returned an error; latency median %d ms, max %d ms"
              % (errs, len(polls), lat[len(lat) // 2] if lat else 0, lat[-1] if lat else 0))
    print("=" * 68)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--every", type=int, default=30, help="seconds between polls (default 30)")
    args = ap.parse_args()
    if args.report:
        report()
    else:
        try:
            collect(args.every)
        except KeyboardInterrupt:
            say("\nstopped -- run  python check.py --report")


if __name__ == "__main__":
    main()
