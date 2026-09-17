"""
Server-side scoring: what the engine predicted, and how it turned out.

This is the method check.py used to score the app from outside, moved inside it
so the scorecard keeps accumulating without a laptop left running.

One row per train-day at NY Penn:
  * the FIRST track the engine predicted, and when it first said so
  * the actual track, and when NJ Transit posted it
  * whether the prediction changed before posting (a flip)

Two rules that keep the numbers honest:
  * Only trains seen BEFORE their track posted count toward coverage and
    accuracy. A train first seen already-posted was never predictable, and
    counting it would drag coverage down every time the app restarts.
  * The FIRST prediction is the one scored. Changing to the right answer later
    still counts as a miss, and is separately reported as a flip.

STATS_DB points at the database. In the container that must be a mounted
volume: a container filesystem is ephemeral, so without the mount every deploy
would reset the record. Rollback journalling (not WAL) because WAL needs shared
memory that SMB file shares like Azure Files do not provide.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("STATS_DB", "stats.db")

_LOCK = threading.Lock()          # one writer at a time: poller + request threads

DDL = """
CREATE TABLE IF NOT EXISTS results (
    service_date TEXT NOT NULL,
    train_id     TEXT NOT NULL,
    line         TEXT,
    destination  TEXT,
    sched_epoch  INTEGER,       -- scheduled departure, UTC epoch seconds
    predicted    TEXT,          -- first predicted track (NULL = never predicted)
    signal       TEXT,          -- circuit | berth
    confidence   REAL,
    predicted_at TEXT,          -- UTC ISO, when we first said it
    flipped      INTEGER DEFAULT 0,
    actual       TEXT,          -- official track (NULL = not posted yet)
    posted_at    TEXT,
    watched      INTEGER,       -- 1 = seen before posting; 0 = first seen already posted
    PRIMARY KEY (service_date, train_id)
);
CREATE INDEX IF NOT EXISTS idx_results_day ON results (service_date);
"""


def connect():
    folder = os.path.dirname(DB_PATH)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
    conn.executescript(DDL)
    return conn


def _iso_to_epoch(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ recording

def record(conn, rows, now_iso, service_date):
    """Fold one board snapshot into the record. Returns [(train, predicted,
    actual)] for predictions that resolved on this snapshot."""
    resolved = []
    with _LOCK:
        for t in rows:
            tid = str(t.get("train") or "").strip()
            if not tid:
                continue
            p = t.get("prediction") or {}
            tier = p.get("tier")
            meta = (t.get("line"), t.get("destination"), t.get("depart_epoch"))
            row = conn.execute(
                "SELECT predicted, actual FROM results WHERE service_date=? AND train_id=?",
                (service_date, tid)).fetchone()

            if tier in ("official", "verified"):
                if row is None:
                    # first sighting is already posted: never a fair test
                    conn.execute(
                        "INSERT INTO results (service_date, train_id, line, destination, "
                        "sched_epoch, actual, posted_at, watched) VALUES (?,?,?,?,?,?,?,0)",
                        (service_date, tid) + meta + (p.get("track"), now_iso))
                elif row[1] is None:
                    conn.execute(
                        "UPDATE results SET actual=?, posted_at=? "
                        "WHERE service_date=? AND train_id=?",
                        (p.get("track"), now_iso, service_date, tid))
                    if row[0]:
                        resolved.append((tid, row[0], p.get("track")))

            elif tier == "predicted":
                if row is None:
                    conn.execute(
                        "INSERT INTO results (service_date, train_id, line, destination, "
                        "sched_epoch, predicted, signal, confidence, predicted_at, watched) "
                        "VALUES (?,?,?,?,?,?,?,?,?,1)",
                        (service_date, tid) + meta + (p.get("track"), p.get("signal"),
                                                      p.get("confidence"), now_iso))
                elif row[0] is None and row[1] is None:
                    conn.execute(
                        "UPDATE results SET predicted=?, signal=?, confidence=?, predicted_at=? "
                        "WHERE service_date=? AND train_id=?",
                        (p.get("track"), p.get("signal"), p.get("confidence"), now_iso,
                         service_date, tid))
                elif row[0] and row[1] is None and row[0] != p.get("track"):
                    conn.execute("UPDATE results SET flipped=1 "
                                 "WHERE service_date=? AND train_id=?", (service_date, tid))

            elif row is None:
                # on the board, not posted, nothing to say yet: still a fair test
                conn.execute(
                    "INSERT INTO results (service_date, train_id, line, destination, "
                    "sched_epoch, watched) VALUES (?,?,?,?,?,1)",
                    (service_date, tid) + meta)

            if meta[2] is not None:
                conn.execute("UPDATE results SET sched_epoch=? WHERE service_date=? "
                             "AND train_id=? AND sched_epoch IS NULL",
                             (meta[2], service_date, tid))
        conn.commit()
    return resolved


def memo_for(conn, service_date):
    """train_id -> first predicted track, for today. Lets the verified badge
    survive a restart: without this the app forgets what it predicted before
    the container came back."""
    with _LOCK:
        return {tid: track for tid, track in conn.execute(
            "SELECT train_id, predicted FROM results "
            "WHERE service_date=? AND predicted IS NOT NULL", (service_date,))}


# ------------------------------------------------------------------- scorecard

def summary(conn, since=None):
    """The numbers the page shows. None of the medians are means: a handful of
    hour-long leads would pull an average well off what a rider experiences."""
    where = "actual IS NOT NULL AND (watched IS NULL OR watched = 1)"
    args = []
    if since:
        where += " AND service_date >= ?"
        args.append(since)
    with _LOCK:
        rows = conn.execute(
            "SELECT predicted, actual, predicted_at, posted_at, flipped, sched_epoch, "
            "service_date FROM results WHERE " + where, args).fetchall()

    if not rows:
        return {"scored": 0}

    pred = [r for r in rows if r[0]]
    correct = [r for r in pred if r[0] == r[1]]
    days = sorted({r[6] for r in rows})

    def med(values):
        v = sorted(x for x in values if x is not None)
        return round(v[len(v) // 2], 1) if v else None

    lead = med((_iso_to_epoch(r[3]) - _iso_to_epoch(r[2])) / 60
               for r in pred if _iso_to_epoch(r[2]) and _iso_to_epoch(r[3]))
    ours_before = med((r[5] - _iso_to_epoch(r[2])) / 60
                      for r in pred if r[5] and _iso_to_epoch(r[2]))
    njt_before = med((r[5] - _iso_to_epoch(r[3])) / 60
                     for r in rows if r[5] and _iso_to_epoch(r[3]))

    return {
        "scored": len(rows),
        "predicted": len(pred),
        "coverage": round(100.0 * len(pred) / len(rows), 1),
        "correct": len(correct),
        "accuracy": round(100.0 * len(correct) / len(pred), 1) if pred else None,
        "flips": sum(1 for r in pred if r[4]),
        "flip_rate": round(100.0 * sum(1 for r in pred if r[4]) / len(pred), 1) if pred else None,
        "lead_over_board": lead,
        "ours_before_departure": ours_before,
        "njt_before_departure": njt_before,
        "days": len(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
    }
