"""
Lightweight accuracy check: our predictions vs the real posted track.

    python check.py            # run; prints a line per poll and each result as it resolves
    python check.py --report   # scorecard from what has been collected

What it does, and nothing more:
  * every 30s, two API calls (vehicle feed + NY board) -- same as the engine
  * remembers the FIRST prediction we made for each train-day, and when
  * when the board posts the real track, compares and records the outcome
  * one small row per train-day in check.db; no raw JSON, no Boxcar, no dumps

Prediction logic is imported from engine.py, so this scores exactly what the
web app shows. Codebook is frozen at startup (run engine.py --rebuild to refresh).
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import njt_logger as njt
import engine

DB = "check.db"
POLL = 30

DDL = """
CREATE TABLE IF NOT EXISTS results (
    service_date TEXT NOT NULL,
    train_id     TEXT NOT NULL,
    line         TEXT,
    destination  TEXT,
    sched_dep    TEXT,
    predicted    TEXT,        -- our first prediction (NULL = we never predicted)
    signal       TEXT,        -- circuit | berth
    confidence   REAL,
    predicted_at TEXT,        -- UTC ISO
    flipped      INTEGER DEFAULT 0,   -- a later prediction disagreed before posting
    actual       TEXT,        -- official track (NULL = not posted yet)
    posted_at    TEXT,
    PRIMARY KEY (service_date, train_id)
);
"""


def connect():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    return conn


def say(msg):
    print(msg, flush=True)


def minutes(a, b):
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ collect

def poll(conn, token, books):
    circuits = engine.fetch_circuits(token)
    payload = njt.api_post("getTrainSchedule", {"token": token, "station": njt.STATION})
    now = datetime.now(timezone.utc).isoformat()
    svc = njt.service_date_for(datetime.now())
    resolved = []

    for item in njt.board_items(payload):
        tid = str(item.get("TRAIN_ID") or "").strip()
        if not tid:
            continue
        p = engine.predict(item, books, circuits, {})
        row = conn.execute("SELECT predicted, actual FROM results "
                           "WHERE service_date=? AND train_id=?", (svc, tid)).fetchone()

        if p["tier"] == "official":
            if row is None:
                # never predicted; still counts in the denominator
                conn.execute("INSERT INTO results (service_date, train_id, line, destination, "
                             "sched_dep, actual, posted_at) VALUES (?,?,?,?,?,?,?)",
                             (svc, tid, item.get("LINE"), item.get("DESTINATION"),
                              item.get("SCHED_DEP_DATE"), p["track"], now))
            elif row[1] is None:
                conn.execute("UPDATE results SET actual=?, posted_at=? "
                             "WHERE service_date=? AND train_id=?", (p["track"], now, svc, tid))
                if row[0]:
                    resolved.append((tid, row[0], p["track"]))

        elif p["tier"] == "predicted":
            if row is None:
                conn.execute("INSERT INTO results (service_date, train_id, line, destination, "
                             "sched_dep, predicted, signal, confidence, predicted_at) "
                             "VALUES (?,?,?,?,?,?,?,?,?)",
                             (svc, tid, item.get("LINE"), item.get("DESTINATION"),
                              item.get("SCHED_DEP_DATE"), p["track"], p["signal"],
                              p["confidence"], now))
            elif row[0] is None and row[1] is None:
                conn.execute("UPDATE results SET predicted=?, signal=?, confidence=?, "
                             "predicted_at=? WHERE service_date=? AND train_id=?",
                             (p["track"], p["signal"], p["confidence"], now, svc, tid))
            elif row[0] and row[1] is None and row[0] != p["track"]:
                conn.execute("UPDATE results SET flipped=1 "
                             "WHERE service_date=? AND train_id=?", (svc, tid))
    conn.commit()

    for tid, pred, actual in resolved:
        r = conn.execute("SELECT signal, predicted_at, posted_at FROM results "
                         "WHERE service_date=? AND train_id=?", (svc, tid)).fetchone()
        lead = minutes(r[1], r[2])
        say("   %s  %-6s predicted %-3s actual %-3s %s  (%s, %.1f min early)"
            % ("HIT " if pred == actual else "MISS", tid, pred, actual,
               "" if pred == actual else "<--", r[0], lead if lead is not None else 0))
    return len(resolved)


def scoreline(conn):
    n_res = conn.execute("SELECT COUNT(*) FROM results WHERE actual IS NOT NULL").fetchone()[0]
    n_pred = conn.execute("SELECT COUNT(*) FROM results WHERE actual IS NOT NULL "
                          "AND predicted IS NOT NULL").fetchone()[0]
    n_hit = conn.execute("SELECT COUNT(*) FROM results WHERE actual IS NOT NULL "
                         "AND predicted = actual").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM results WHERE actual IS NULL "
                           "AND predicted IS NOT NULL").fetchone()[0]
    cov = 100.0 * n_pred / n_res if n_res else 0
    acc = 100.0 * n_hit / n_pred if n_pred else 0
    return "resolved %d | predicted %d (%.0f%%) | correct %d (%.1f%%) | pending %d" % (
        n_res, n_pred, cov, n_hit, acc, pending)


def collect():
    njt.load_env()
    books = engine.load_codebooks()
    conn = connect()
    token = njt.get_token()
    say("checking every %ds -> %s   (Ctrl-C to stop)\n" % (POLL, DB))
    errors = 0
    while True:
        try:
            poll(conn, token, books)
            say("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), scoreline(conn)))
            errors = 0
            time.sleep(POLL)
        except njt.AuthError:
            say("   re-minting token")
            try:
                token = njt.get_token(force=True)
            except Exception as e:
                errors += 1
                say("   re-auth failed: %s" % e)
                time.sleep(POLL * min(2 ** errors, 20))
        except Exception as e:
            errors += 1
            say("   error: %s: %s" % (type(e).__name__, e))
            time.sleep(POLL * min(2 ** errors, 20))


# ------------------------------------------------------------------- report

def report():
    if not os.path.exists(DB):
        sys.exit("No %s yet. Run  python check.py  first." % DB)
    conn = connect()
    rows = conn.execute("SELECT service_date, train_id, line, predicted, signal, "
                        "predicted_at, actual, posted_at, flipped FROM results "
                        "WHERE actual IS NOT NULL ORDER BY posted_at").fetchall()
    if not rows:
        sys.exit("Nothing resolved yet.")
    days = sorted({r[0] for r in rows})
    pred = [r for r in rows if r[3]]
    hits = [r for r in pred if r[3] == r[6]]
    miss = [r for r in pred if r[3] != r[6]]
    leads = sorted(x for x in (minutes(r[5], r[7]) for r in pred) if x is not None)

    print("=" * 66)
    print("PREDICTIONS vs REAL TRACKS  --  %d train-days, %s .. %s" % (len(rows), days[0], days[-1]))
    print("=" * 66)
    print("coverage   %5.1f%%   (%d of %d departures got a prediction)"
          % (100.0 * len(pred) / len(rows), len(pred), len(rows)))
    print("accuracy   %5.1f%%   (%d right, %d wrong)"
          % (100.0 * len(hits) / max(len(pred), 1), len(hits), len(miss)))
    print("flips      %5.1f%%   (%d changed before posting)"
          % (100.0 * sum(1 for r in pred if r[8]) / max(len(pred), 1), sum(1 for r in pred if r[8])))
    if leads:
        print("lead       median %.1f min   p25 %.1f   p75 %.1f   max %.0f"
              % (leads[len(leads) // 2], leads[len(leads) // 4], leads[3 * len(leads) // 4], leads[-1]))

    print("\nby signal:")
    for sig in ("circuit", "berth"):
        s = [r for r in pred if r[4] == sig]
        if s:
            print("   %-8s %4d predictions   %5.1f%% accurate"
                  % (sig, len(s), 100.0 * sum(1 for r in s if r[3] == r[6]) / len(s)))

    print("\nby track (coverage of departures that actually used it):")
    from collections import Counter
    used, got = Counter(r[6] for r in rows), Counter(r[6] for r in pred)
    for t in sorted(used, key=lambda x: int(x) if x.isdigit() else 99):
        print("   track %-3s %3d/%-3d %4.0f%%" % (t, got[t], used[t], 100.0 * got[t] / used[t]))

    if miss:
        print("\nmisses:")
        for r in miss:
            print("   %s  %-6s %-18s predicted %-3s actual %-3s (%s)"
                  % (r[0], r[1], (r[2] or "")[:18], r[3], r[6], r[4]))
    print("=" * 66)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.report:
        report()
    else:
        try:
            collect()
        except KeyboardInterrupt:
            say("\nstopped -- run  python check.py --report")


if __name__ == "__main__":
    main()
