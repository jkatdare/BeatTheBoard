"""
Server-side scoring: what the engine predicted, and how it turned out.

This is the method check.py used to score the app from outside, moved inside it
so the scorecard keeps accumulating without a laptop left running.

One row per train-day at NY Penn:
  * the FIRST track the engine predicted, and when it first said so
  * the last poll at which NJ Transit's board still showed no track
  * the actual track, and when NJ Transit posted it
  * whether the prediction changed before posting (a flip)

Rules that keep the numbers honest:
  * Only trains seen BEFORE their track posted count toward coverage and
    accuracy. A train first seen already-posted was never predictable, and
    counting it would drag coverage down every time the app restarts.
  * The FIRST prediction is the one scored. Changing to the right answer later
    still counts as a miss, and is separately reported as a flip.
  * A prediction only counts as beating the board if it was showing at least
    MIN_LEAD seconds before NJ Transit posted. The board is sampled, so the
    exact posting moment is never known: it happened somewhere between the
    last poll that showed no track and the first that did. The lead is
    therefore measured to the LAST poll at which the track was still blank
    (unposted_at - predicted_at), which is the lead that can be proven. A
    call that shows up one poll before the posting proves nothing and does
    not count. Late calls are left out of coverage and accuracy alike and
    reported separately, so nothing is hidden.

Storage
-------
The working table lives in an in-memory SQLite database. It is snapshotted to
a JSON file (write to a temp file, then atomic rename) whenever a prediction
or posting is recorded, and otherwise at most every SNAPSHOT_SECONDS, and
that file is reloaded at startup. STATS_DB names the snapshot; a ".db" suffix
is rewritten to ".json".

Why not just put a SQLite file on the mounted share: SQLite locks the file
with byte-range locks, and SMB shares like Azure Files do not honour them --
the first connect() fails with "database is locked". Plain file writes work
fine there, which is all a JSON snapshot needs.
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime

_raw = os.environ.get("STATS_DB", "stats.db")
DB_PATH = _raw[:-3] + ".json" if _raw.endswith(".db") else _raw   # the snapshot

# A call has to be showing this long before NJ Transit posts to count as
# having beaten the board. Measured as the proven lead (see proven_lead).
MIN_LEAD = int(os.environ.get("MIN_LEAD_SECONDS", "30"))

# Rows written before unposted_at existed were sampled every 30 s. For those,
# the proven lead is the measured lead minus one such interval.
LEGACY_POLL = 30

SNAPSHOT_SECONDS = 30             # quiet polls are persisted no more often than this
_LAST_SNAPSHOT = 0.0

_LOCK = threading.Lock()          # one writer at a time: poller + request threads

COLUMNS = ["service_date", "train_id", "line", "destination", "sched_epoch",
           "predicted", "signal", "confidence", "predicted_at", "unposted_at",
           "flipped", "actual", "posted_at", "watched"]

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
    unposted_at  TEXT,          -- UTC ISO, last poll at which the board still showed no track
    flipped      INTEGER DEFAULT 0,
    actual       TEXT,          -- official track (NULL = not posted yet)
    posted_at    TEXT,          -- UTC ISO, first poll at which the board showed it
    watched      INTEGER,       -- 1 = seen before posting; 0 = first seen already posted
    PRIMARY KEY (service_date, train_id)
);
CREATE INDEX IF NOT EXISTS idx_results_day ON results (service_date);
"""


def connect():
    """In-memory table, seeded from the snapshot if there is one."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(DDL)
    if os.path.exists(DB_PATH):
        try:
            with open(DB_PATH, "r", encoding="utf-8") as fh:
                rows = json.load(fh).get("results", [])
            conn.executemany(
                "INSERT OR REPLACE INTO results (%s) VALUES (%s)"
                % (", ".join(COLUMNS), ", ".join("?" * len(COLUMNS))),
                [tuple(r.get(c) for c in COLUMNS) for r in rows])
            conn.commit()
        except (OSError, ValueError, sqlite3.DatabaseError) as e:
            print("scorecard: could not load %s (%s: %s) -- starting empty"
                  % (DB_PATH, type(e).__name__, e))
    return conn


def _snapshot(conn):
    rows = [dict(zip(COLUMNS, r)) for r in conn.execute(
        "SELECT %s FROM results" % ", ".join(COLUMNS))]
    folder = os.path.dirname(DB_PATH)
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"saved_at": datetime.utcnow().isoformat() + "Z",
                   "results": rows}, fh, separators=(",", ":"))
    os.replace(tmp, DB_PATH)


def _iso_to_epoch(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def proven_lead(predicted_at, unposted_at, posted_at=None):
    """Seconds the call was provably showing before the posting, or None if
    there was no call. All arguments are epoch seconds (or None)."""
    if predicted_at is None:
        return None
    if unposted_at is not None:
        return unposted_at - predicted_at
    if posted_at is not None:                 # row from before unposted_at existed
        return posted_at - predicted_at - LEGACY_POLL
    return 0.0


# ------------------------------------------------------------------ recording

def record(conn, rows, now_iso, service_date):
    """Fold one board snapshot into the record and persist it. Returns
    [(train, predicted, actual)] for predictions that resolved this time."""
    global _LAST_SNAPSHOT
    resolved = []
    changed = False                 # something worth persisting immediately
    with _LOCK:
        for t in rows:
            tid = str(t.get("train") or "").strip()
            if not tid:
                continue
            p = t.get("prediction") or {}
            tier = p.get("tier")
            meta = (t.get("line"), t.get("destination"), t.get("depart_epoch"))
            row = conn.execute(
                "SELECT predicted, actual, flipped FROM results "
                "WHERE service_date=? AND train_id=?", (service_date, tid)).fetchone()

            if tier in ("official", "verified"):
                if row is None:
                    # first sighting is already posted: never a fair test
                    conn.execute(
                        "INSERT INTO results (service_date, train_id, line, destination, "
                        "sched_epoch, actual, posted_at, watched) VALUES (?,?,?,?,?,?,?,0)",
                        (service_date, tid) + meta + (p.get("track"), now_iso))
                    changed = True
                elif row[1] is None:
                    conn.execute(
                        "UPDATE results SET actual=?, posted_at=? "
                        "WHERE service_date=? AND train_id=?",
                        (p.get("track"), now_iso, service_date, tid))
                    changed = True
                    if row[0]:
                        resolved.append((tid, row[0], p.get("track")))
            else:
                if tier == "predicted":
                    if row is None:
                        conn.execute(
                            "INSERT INTO results (service_date, train_id, line, destination, "
                            "sched_epoch, predicted, signal, confidence, predicted_at, "
                            "unposted_at, watched) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                            (service_date, tid) + meta + (p.get("track"), p.get("signal"),
                                                          p.get("confidence"), now_iso, now_iso))
                        changed = True
                    elif row[0] is None and row[1] is None:
                        conn.execute(
                            "UPDATE results SET predicted=?, signal=?, confidence=?, predicted_at=? "
                            "WHERE service_date=? AND train_id=?",
                            (p.get("track"), p.get("signal"), p.get("confidence"), now_iso,
                             service_date, tid))
                        changed = True
                    elif row[0] and row[1] is None and row[0] != p.get("track") and not row[2]:
                        conn.execute("UPDATE results SET flipped=1 "
                                     "WHERE service_date=? AND train_id=?", (service_date, tid))
                        changed = True
                elif row is None:
                    # on the board, not posted, nothing to say yet: still a fair test
                    conn.execute(
                        "INSERT INTO results (service_date, train_id, line, destination, "
                        "sched_epoch, unposted_at, watched) VALUES (?,?,?,?,?,?,1)",
                        (service_date, tid) + meta + (now_iso,))
                    changed = True
                if row is not None and row[1] is None:
                    # still blank on the board at this poll: this is the moment
                    # any lead is measured to
                    conn.execute("UPDATE results SET unposted_at=? "
                                 "WHERE service_date=? AND train_id=?",
                                 (now_iso, service_date, tid))

            if meta[2] is not None:
                conn.execute("UPDATE results SET sched_epoch=? WHERE service_date=? "
                             "AND train_id=? AND sched_epoch IS NULL",
                             (meta[2], service_date, tid))
        conn.commit()
        now = time.time()
        if changed or now - _LAST_SNAPSHOT >= SNAPSHOT_SECONDS:
            try:
                _snapshot(conn)
                _LAST_SNAPSHOT = now
            except OSError as e:
                print("scorecard: snapshot failed (%s: %s)" % (type(e).__name__, e))
    return resolved


def memo_for(conn, service_date):
    """train_id -> {track, predicted_at, unposted_at, posted_at, watched}
    (epochs) for every train on record today, predicted or not. Lets the
    badges and the per-train lead times survive a restart: without this the
    app forgets what it saw before the container came back."""
    with _LOCK:
        return {tid: {"track": track,
                      "predicted_at": _iso_to_epoch(pa),
                      "unposted_at": _iso_to_epoch(ua),
                      "posted_at": _iso_to_epoch(po),
                      "watched": 1 if watched is None else watched}
                for tid, track, pa, ua, po, watched in conn.execute(
                    "SELECT train_id, predicted, predicted_at, unposted_at, posted_at, "
                    "watched FROM results WHERE service_date=?", (service_date,))}


# ------------------------------------------------------------------- scorecard

def _median(values):
    v = sorted(x for x in values if x is not None)
    return round(v[len(v) // 2], 1) if v else None


def _mean(values):
    v = [x for x in values if x is not None]
    return round(sum(v) / len(v), 1) if v else None


def summary(conn, since=None):
    """The scorecard. Lead times are reported as median AND mean: the
    distribution is right-skewed (a set that sits at the platform for hours
    gives a multi-hour "lead"), so the mean overstates what a rider typically
    gets while the median is the honest typical case. Both are shown so the
    skew is visible rather than hidden."""
    where = "actual IS NOT NULL AND (watched IS NULL OR watched = 1)"
    args = []
    if since:
        where += " AND service_date >= ?"
        args.append(since)
    with _LOCK:
        rows = conn.execute(
            "SELECT predicted, actual, predicted_at, posted_at, flipped, sched_epoch, "
            "service_date, unposted_at FROM results WHERE " + where, args).fetchall()
    if not rows:
        return {"scored": 0}

    called = [r for r in rows if r[0]]
    # only calls provably showing MIN_LEAD before the posting count
    pred = [r for r in called
            if (proven_lead(_iso_to_epoch(r[2]), _iso_to_epoch(r[7]), _iso_to_epoch(r[3]))
                or 0) >= MIN_LEAD]
    correct = [r for r in pred if r[0] == r[1]]
    days = sorted({r[6] for r in rows})

    ours = [(r[5] - _iso_to_epoch(r[2])) / 60 for r in pred if r[5] and _iso_to_epoch(r[2])]
    njt = [(r[5] - _iso_to_epoch(r[3])) / 60 for r in rows if r[5] and _iso_to_epoch(r[3])]
    lead = [(_iso_to_epoch(r[3]) - _iso_to_epoch(r[2])) / 60
            for r in pred if _iso_to_epoch(r[2]) and _iso_to_epoch(r[3])]

    return {
        "scored": len(rows),                                   # trains seen before posting, now posted
        "predicted": len(pred),                                # calls that beat the board by MIN_LEAD+
        "late": len(called) - len(pred),                       # calls too close to the posting to count
        "min_lead": MIN_LEAD,
        "coverage": round(100.0 * len(pred) / len(rows), 1),
        "correct": len(correct),
        "accuracy": round(100.0 * len(correct) / len(pred), 1) if pred else None,
        "flips": sum(1 for r in pred if r[4]),
        "ours_median": _median(ours), "ours_mean": _mean(ours),   # minutes before departure
        "njt_median": _median(njt), "njt_mean": _mean(njt),
        "lead_median": _median(lead),                             # ours minus NJT, per train
        "days": len(days),
        "first_day": days[0], "last_day": days[-1],
    }
