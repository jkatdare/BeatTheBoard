"""
Track prediction engine + web app for NY Penn Station.

    python engine.py            # serve on http://localhost:8080
    python engine.py --rebuild  # rebuild both codebooks from collected data first
    python engine.py --once     # print one board to the terminal and exit

Runtime needs only two things: live API calls and codebook.json. No history
is consulted to make a prediction, so run_logger.py is optional -- it is one of
two places --rebuild can learn the codebooks from:

    track_history.db   (run_logger.py)  full vehicle + board dump, ~230 MB/day
    benchmark.db       (benchmark.py)   board rows + circuit + truth, much smaller

--rebuild reads whichever exist and merges their votes, so the logger can be
retired without losing what it already taught the codebooks.

Serving model
-------------
A background thread polls NJT every POLL_SECONDS and caches the board; requests
are served from that cache, so the page costs no API calls of its own. The
poller is what keeps the scorecard filling when nobody is looking, and it is
only viable because a warm replica is kept running -- under scale-to-zero there
would be no process alive to run it. It sleeps between QUIET_START and
QUIET_END (Eastern), when no trains depart Penn; a request during those hours
still fetches on demand.

Every snapshot is folded into stats.py, which records what was predicted and
how it turned out, and serves the live scorecard at /api/stats.

Three slower feeds ride along with the poll, each on its own timer: NJ
Transit's rail alerts (getStationMSG, every ALERT_SECONDS) for the warning
triangle, every train's stop list (getTrainSchedule, every STOPS_SECONDS) for
the stop filter and arrival times, and the station list (getStationList, once
a day) for the search box.

Environment variables that matter for hosting:
    BIND         127.0.0.1 (default, this machine only) | 0.0.0.0 in a container
    PORT         8080
    STATS_DB     where the scorecard lives. In the container this must point at
                 a mounted volume (see setup_storage.py) -- a container
                 filesystem is ephemeral and would reset on every deploy.
    POLL_SECONDS how often the poller asks NJT (default 5); 0 disables it and
                 falls back to fetching per request.
    CACHE_SECONDS how long a request serves the last board (default: the poll
                 interval, so requests never fetch while the poller is alive).

How it predicts
---------------
Not a model. Two leaked signals in NJ TRANSIT's own RailData API, decoded with
lookup tables learned from history:

  1. CIRCUIT  getVehicleData -> ICS_TRACK_CKT. The signalling track circuit a
     train currently occupies. At Penn the circuits belong to Amtrak's A and JO
     interlockings and name the platform track (AA-A180TK -> 4, JO-AJO16TK -> 6).
     Appears once the train is routed, well before the board posts.
  2. BERTH    getTrainSchedule -> GPSLATITUDE/GPSLONGITUDE. A fixed per-track
     berth coordinate, published before TRACK is. Only exists for tracks
     1-3 and 10-14.

The two barely overlap, which is why the union is worth so much more than either.

Held-out evaluation (codebooks built on days < 2026-09-08, scored on the rest):

                       coverage   accuracy   median lead
    circuit              50.1%      99.1%      13.2 min
    berth                45.8%      98.5%      13.2 min
    UNION                67.5%      98.6%      13.2 min      flip rate 0.7%

For scale: the official board's own median lead is 9.9 min, and Boxcar Signal
measured at 50.7% coverage / 98.5% accuracy over the same week.

The engine abstains where neither signal exists. The per-train historical
prior measured 15.6% (track) / 30.0% (platform) and is shown only as context.
"""

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import njt_logger as njt
import stats

HISTORY_DB = njt.DB_PATH          # written by run_logger.py (optional)
BENCH_DB = "benchmark.db"         # written by benchmark.py
CODEBOOK_PATH = "codebook.json"

BIND = os.environ.get("BIND", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))
# The poller keeps the scorecard filling without anyone visiting the page.
# It is only viable because a warm replica is kept running; under
# scale-to-zero there would be no process alive to run it. Set to 0 to
# disable and go back to fetching only when a request arrives.
#
# How fast it can go is set by NJ Transit, not by Azure. Each poll is two
# calls (board + vehicles) and each method allows 40,000 a day, so over the
# 22 hours a day the poller runs, 2 s is the absolute floor and 5 s leaves
# room for restarts and local test runs (about 40% of the quota). Their
# feeds move every 1-2 s, so the poll rate is what a rider notices. On Azure
# the replica is billed for its allocated 0.25 vCPU every second it runs --
# at the active rate once it receives more than 1 KB/s, which any poll
# faster than ~25 s already does -- so the rate itself costs nothing extra.
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))

# How long a request serves the last board before fetching a fresh one.
# While the poller runs, requests should never need to fetch, so this
# defaults to the poll interval; without the poller, 20 s.
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "0")) or (POLL_SECONDS or 20)

# No NJ Transit departures from Penn in the small hours, so the poller
# sleeps through them. Requests are still served if anyone does visit.
QUIET_START = int(os.environ.get("QUIET_START_HOUR", "2"))   # inclusive, Eastern
QUIET_END = int(os.environ.get("QUIET_END_HOUR", "4"))       # exclusive, Eastern

# NJ Transit caps token minting at ~10 a day and every container start spends
# one, so the page shows the count. Refreshed on a timer, not per request.
USAGE_TTL = int(os.environ.get("USAGE_TTL", "600"))
USAGE = {"at": 0.0, "data": None}

GPS_MIN_N, GPS_MIN_PURITY = 3, 0.98    # chosen by held-out sweep
CKT_MIN_N, CKT_MIN_PURITY = 20, 0.95
MIN_DAYS = 3                           # distinct train-days an entry must be seen on

NYP_LAT, NYP_LON, PENN_BOX = 40.7498, -73.9918, 0.006


def _near_penn(lat, lon):
    try:
        return (abs(float(lat) - NYP_LAT) < PENN_BOX
                and abs(float(lon) - NYP_LON) < PENN_BOX)
    except (TypeError, ValueError):
        return False


def _open_ro(path):
    """Read-only handle, or None. Never creates a file: sqlite3.connect on a
    missing path would silently make an empty DB and then fail on the query."""
    if not path or not os.path.exists(path):
        return None
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA query_only=1")
    return conn


# ---------------------------------------------------------------- codebooks

def _sift(votes, days, min_n, min_purity):
    """Rows alone are weak evidence: a train parked on one circuit for 40 minutes
    is 40 rows from a single train-day. An entry must also be seen on MIN_DAYS
    distinct train-days pointing at the same track."""
    book, rejected = {}, []
    for key, counter in votes.items():
        total = sum(counter.values())
        if total < min_n:
            continue
        track, n = counter.most_common(1)[0]
        purity = n / total
        nd = len(days[key][track])
        if purity >= min_purity and nd >= MIN_DAYS:
            book[key] = {"track": track, "n": total, "days": nd, "purity": round(purity, 3)}
        else:
            rejected.append((key, dict(counter)))
    return book, rejected


def _votes_from_history(gvotes, cvotes, gdays, cdays):
    """run_logger.py's DB. Berth votes from posted rows at Penn; circuit votes
    from every vehicle row of a train-day, against the track it finally posted."""
    conn = _open_ro(HISTORY_DB)
    if not conn:
        return 0
    n = 0
    try:
        for tid, sd, lat, lon, track in conn.execute(
                "SELECT train_id, service_date, gps_lat, gps_lon, track FROM observations "
                "WHERE gps_lat IS NOT NULL AND track != '' AND at_penn = 1"):
            t = str(track).strip()
            if t.isdigit():
                gvotes[lat + "," + lon][t] += 1
                gdays[lat + "," + lon][t].add((tid, sd))
                n += 1
        for tid, sd, ckt, track in conn.execute(
                "SELECT v.train_id, v.service_date, v.ics_track_ckt, p.track "
                "FROM vehicle_positions v "
                "JOIN track_postings p ON p.train_id = v.train_id "
                "                      AND p.service_date = v.service_date "
                "WHERE v.ics_track_ckt IS NOT NULL AND v.ics_track_ckt != '' "
                "  AND p.track != ''"):
            t = str(track).strip()
            if t.isdigit():
                cvotes[ckt][t] += 1
                cdays[ckt][t].add((tid, sd))
                n += 1
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    return n


def _votes_from_benchmark(gvotes, cvotes, gdays, cdays):
    """benchmark.py's DB. Each njt row carries the board item (with GPS) and the
    circuit the vehicle feed showed for that train at that moment (_circuit).
    Truth is the first official track for the train-day."""
    conn = _open_ro(BENCH_DB)
    if not conn:
        return 0
    n = 0
    try:
        truth = {}
        for sd, tid, track in conn.execute(
                "SELECT service_date, train_id, track FROM obs "
                "WHERE source = 'njt' AND is_official = 1 AND track IS NOT NULL "
                "ORDER BY seen_at"):
            t = str(track).strip()
            if t.isdigit():
                truth.setdefault((sd, tid), t)
        for sd, tid, is_off, raw in conn.execute(
                "SELECT service_date, train_id, is_official, raw FROM obs "
                "WHERE source = 'njt'"):
            t = truth.get((sd, tid))
            if not t:
                continue
            try:
                item = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            lat, lon = item.get("GPSLATITUDE"), item.get("GPSLONGITUDE")
            if is_off and lat and lon and _near_penn(lat, lon):
                gvotes[str(lat) + "," + str(lon)][t] += 1
                gdays[str(lat) + "," + str(lon)][t].add((tid, sd))
                n += 1
            ckt = item.get("_circuit")
            if ckt:
                cvotes[ckt][t] += 1
                cdays[ckt][t].add((tid, sd))
                n += 1
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    return n


def build_codebooks(verbose=True):
    gvotes, cvotes = defaultdict(Counter), defaultdict(Counter)
    gdays = defaultdict(lambda: defaultdict(set))
    cdays = defaultdict(lambda: defaultdict(set))
    n_hist = _votes_from_history(gvotes, cvotes, gdays, cdays)
    n_bench = _votes_from_benchmark(gvotes, cvotes, gdays, cdays)
    if not (n_hist or n_bench):
        sys.exit("No data to learn from: neither %s nor %s has observations yet."
                 % (HISTORY_DB, BENCH_DB))

    gps, grej = _sift(gvotes, gdays, GPS_MIN_N, GPS_MIN_PURITY)
    cir, crej = _sift(cvotes, cdays, CKT_MIN_N, CKT_MIN_PURITY)
    payload = {"built_at": datetime.now(timezone.utc).isoformat(),
               "sources": {HISTORY_DB: n_hist, BENCH_DB: n_bench},
               "gps": gps, "circuits": cir}
    with open(CODEBOOK_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    if verbose:
        print("sources          : %s %d votes | %s %d votes"
              % (HISTORY_DB, n_hist, BENCH_DB, n_bench))
        print("berth codebook   : %d coords, tracks %s  (%d ambiguous dropped)"
              % (len(gps), " ".join(sorted({v["track"] for v in gps.values()}, key=int)), len(grej)))
        print("circuit codebook : %d circuits, tracks %s  (%d ambiguous dropped)"
              % (len(cir), " ".join(sorted({v["track"] for v in cir.values()}, key=int)), len(crej)))
    return payload


def load_codebooks(rebuild=False):
    if rebuild or not os.path.exists(CODEBOOK_PATH):
        return build_codebooks()
    with open(CODEBOOK_PATH, "r", encoding="utf-8") as fh:
        blob = json.load(fh)
    if "entries" in blob and "gps" not in blob:      # pre-circuit format
        print("old codebook format found -- rebuilding with circuits")
        return build_codebooks()
    print("codebooks: %d berth coords, %d circuits (built %s)"
          % (len(blob["gps"]), len(blob["circuits"]), blob.get("built_at", "?")[:19]))
    return blob


def load_history():
    """Per-train track frequencies. Context only -- measured at 15.6% top-1.
    Reads the logger DB if present, else the benchmark DB, else nothing."""
    hist = defaultdict(Counter)
    conn = _open_ro(HISTORY_DB)
    if conn:
        try:
            for train_id, track in conn.execute(
                    "SELECT train_id, track FROM track_postings WHERE track != ''"):
                if str(track).strip().isdigit():
                    hist[train_id][str(track).strip()] += 1
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
    if not hist:
        conn = _open_ro(BENCH_DB)
        if conn:
            try:
                seen = set()
                for sd, tid, track in conn.execute(
                        "SELECT service_date, train_id, track FROM obs "
                        "WHERE source = 'njt' AND is_official = 1 AND track IS NOT NULL "
                        "ORDER BY seen_at"):
                    if (sd, tid) in seen:
                        continue
                    seen.add((sd, tid))
                    if str(track).strip().isdigit():
                        hist[tid][str(track).strip()] += 1
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()
    return hist


# ---------------------------------------------------------------- predicting

def now_eastern():
    """NJT times are US Eastern; the container clock is UTC. Prefer the tz
    database when present, else apply the US DST rule by hand (python:*-slim
    images ship without tzdata)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:
        utc = datetime.now(timezone.utc).replace(tzinfo=None)

        def nth_sunday(month, n):
            d = datetime(utc.year, month, 1)
            d += timedelta(days=(6 - d.weekday()) % 7)      # first Sunday
            return d + timedelta(weeks=n - 1)

        dst_start = nth_sunday(3, 2) + timedelta(hours=7)   # 2am EST = 07:00 UTC
        dst_end = nth_sunday(11, 1) + timedelta(hours=6)    # 2am EDT = 06:00 UTC
        return utc - timedelta(hours=4 if dst_start <= utc < dst_end else 5)


def fetch_vehicles(token):
    """train_id -> {ckt, lat, lon, next_stop}, from one getVehicleData call."""
    trains = njt.api_post("getVehicleData", {"token": token})
    if isinstance(trains, dict):
        trains = trains.get("TRAINS") or []
    out = {}
    for t in trains if isinstance(trains, list) else []:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("ID") or t.get("TRAIN_ID") or "").strip()
        if tid:
            out[tid] = {"ckt": t.get("ICS_TRACK_CKT") or None,
                        "lat": t.get("LATITUDE"), "lon": t.get("LONGITUDE"),
                        "next_stop": t.get("NEXT_STOP")}
    return out


def fetch_circuits(token):
    """train_id -> current ICS_TRACK_CKT (the form benchmark.py relies on)."""
    return {tid: v["ckt"] for tid, v in fetch_vehicles(token).items() if v["ckt"]}


def arrival(item, vehicle):
    """Is this train physically at Penn?  -> (True / False / None, label)
    BOARDING on the board is definitive. Otherwise the vehicle feed's reported
    position; failing that, the board row's own coordinate. None = the train
    is not reporting a position at all (not yet activated, or sitting under
    its inbound number after a turn)."""
    status = str(item.get("STATUS") or "").strip().upper()
    if status in ("BOARDING", "ALL ABOARD"):
        return True, "arrived"
    lat = lon = None
    if vehicle and vehicle.get("lat") and vehicle.get("lon"):
        lat, lon = vehicle["lat"], vehicle["lon"]
    elif item.get("GPSLATITUDE") and item.get("GPSLONGITUDE"):
        lat, lon = item["GPSLATITUDE"], item["GPSLONGITUDE"]
    if lat is None:
        return None, "no position"
    return (True, "arrived") if _near_penn(lat, lon) else (False, "en route")


def predict(item, books, circuits, hist):
    posted = (item.get("TRACK") or "").strip()
    if posted:
        return {"track": posted, "tier": "official", "signal": None, "confidence": None}

    tid = str(item.get("TRAIN_ID") or "").strip()
    ckt = circuits.get(tid)
    hit = books["circuits"].get(ckt) if ckt else None
    if hit:
        return {"track": hit["track"], "tier": "predicted", "signal": "circuit",
                "confidence": hit["purity"]}

    lat, lon = item.get("GPSLATITUDE"), item.get("GPSLONGITUDE")
    if lat and lon:
        hit = books["gps"].get(str(lat) + "," + str(lon))
        if hit:
            return {"track": hit["track"], "tier": "predicted", "signal": "berth",
                    "confidence": hit["purity"]}

    counter = hist.get(tid)
    if counter and sum(counter.values()) >= 4:
        total = sum(counter.values())
        top = counter.most_common(3)
        return {"track": None, "tier": "history", "signal": None,
                "confidence": round(top[0][1] / total, 2),
                "candidates": [{"track": t, "share": round(n / total, 2)} for t, n in top]}
    return {"track": None, "tier": "none", "signal": None, "confidence": None}


# NJT train numbers are numeric. Letter prefixes are other operators sharing the
# board -- A=Amtrak, S=SEPTA, X=non-revenue equipment moves -- none of which a
# rider here can board, so they are dropped. Set NJT_ONLY=0 to show them again.
NJT_ONLY = os.environ.get("NJT_ONLY", "1") != "0"

# The board sends truncated display names ("Northeast Corrdr", "No Jersey
# Coast"). LINECODE is stable, so map on that and fall back to what was sent.
LINE_NAMES = {
    "NE": "Northeast Corridor Line",
    "NC": "North Jersey Coast Line",
    "ME": "Morristown Line",
    "GS": "Gladstone Branch",
    "MC": "Montclair-Boonton Line",
    "RV": "Raritan Valley Line",
}


def line_name(item):
    return LINE_NAMES.get(str(item.get("LINECODE") or "").strip().upper(),
                          item.get("LINE") or "")


# ------------------------------------------------------ alerts, stops, stations

def _clock(s, fmt="%m/%d/%Y %I:%M:%S %p"):
    """'9/17/2026 6:23:50 PM' -> '6:23 PM' (or '' if unparsable)."""
    try:
        return datetime.strptime(str(s), fmt).strftime("%I:%M %p").lstrip("0")
    except (TypeError, ValueError):
        return ""


# NJ Transit's rail alerts (the same feed as their website's advisories), for
# the warning triangle. Refreshed on a timer; a failed refresh keeps the last
# list rather than blanking every warning.
ALERT_SECONDS = int(os.environ.get("ALERT_SECONDS", "60"))
ALERTS = {"at": 0.0, "items": []}

# An alert's line scope names lines in NJ Transit's own words ("*ME Line",
# "*MontClair-Boonton Line"); map to the LINECODEs on the board. The Morris
# & Essex covers both Morristown Line and Gladstone Branch trains. A blank
# scope matches nothing: better a missed banner than a triangle on every row.
LINE_SCOPES = (("northeast corridor", ("NE",)), ("coast", ("NC",)),
               ("raritan", ("RV",)), ("montclair", ("MC",)), ("gladstone", ("GS",)),
               ("morris", ("ME", "GS")), ("me line", ("ME", "GS")), ("m&e", ("ME", "GS")))
_TRAIN_NO = re.compile(r"#\s*(\d{2,4})\b")


def _scope_codes(scope):
    codes = set()
    for part in str(scope or "").split("*"):
        p = part.strip().lower()
        for needle, cs in LINE_SCOPES:
            if p and needle in p:
                codes.update(cs)
    return codes


def fetch_alerts(token):
    rows = njt.api_post("getStationMSG", {"token": token, "station": njt.STATION, "line": ""})
    out, seen = [], set()
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("MSG_ID") in seen:
            continue
        seen.add(r.get("MSG_ID"))
        # their feed garbles its dashes into " ? " ("Track 4 Unavailable ? Weekends")
        text = html.unescape(str(r.get("MSG_TEXT") or "")).replace(" ? ", " - ").strip()
        if not text:
            continue
        m = _TRAIN_NO.search(text)
        out.append({"text": text, "url": r.get("MSG_URL") or "",
                    "when": _clock(r.get("MSG_PUBDATE")),
                    # the first train named is the subject; later ones are the
                    # alternatives riders are told to take
                    "train": m.group(1) if m else None,
                    "lines": _scope_codes(r.get("MSG_LINE_SCOPE"))})
    return out


def alerts_cached(token):
    if time.time() - ALERTS["at"] >= ALERT_SECONDS:
        ALERTS["at"] = time.time()
        try:
            ALERTS["items"] = fetch_alerts(token)
        except Exception as e:
            print("alerts: %s: %s" % (type(e).__name__, e))
    return ALERTS["items"]


def alerts_for(item, alerts):
    """Delay and alert notes for one board row: its own status first, then NJ
    Transit alerts naming this train, then alerts for its whole line. The
    page shows the triangle for any of them and the first non-line note as
    the reason."""
    tid = str(item.get("TRAIN_ID") or "").strip()
    code = str(item.get("LINECODE") or "").strip().upper()
    status = str(item.get("STATUS") or "").strip().upper()
    try:
        late = int(float(item.get("SEC_LATE") or 0))
    except ValueError:
        late = 0
    out = []
    if "CANCEL" in status:
        out.append({"scope": "status", "text": "canceled", "url": "", "when": ""})
    elif late >= 300:
        out.append({"scope": "status", "text": "running %d min late" % (late // 60),
                    "url": "", "when": ""})
    elif "DELAY" in status:
        out.append({"scope": "status", "text": "delayed", "url": "", "when": ""})
    msg = str(item.get("INLINEMSG") or "").strip()
    if msg:
        out.append({"scope": "train", "text": msg, "url": "", "when": ""})
    for a in alerts:
        if a["train"] == tid:
            out.append({"scope": "train", "text": a["text"], "url": a["url"], "when": a["when"]})
    for a in alerts:
        if a["train"] is None and code in a["lines"]:
            out.append({"scope": "line", "text": a["text"], "url": a["url"], "when": a["when"]})
    return out


# Every train's stop list, for the stop filter and arrival times. The 19Rec
# board sends STOPS empty; the classic getTrainSchedule carries them, with
# NJ Transit's current projection for each stop's time.
STOPS_SECONDS = int(os.environ.get("STOPS_SECONDS", "60"))
STOPS = {"at": 0.0, "by_train": {}}


def fetch_stops(token):
    payload = njt.api_post("getTrainSchedule", {"token": token, "station": njt.STATION})
    out = {}
    for item in njt.board_items(payload):
        tid = str(item.get("TRAIN_ID") or "").strip()
        stops = item.get("STOPS")
        if not tid or not isinstance(stops, list):
            continue
        out[tid] = [{"code": str(s.get("STATION_2CHAR") or "").strip(),
                     "name": str(s.get("STATIONNAME") or "").strip(),
                     "time": _clock(s.get("TIME"), "%d-%b-%Y %I:%M:%S %p")}
                    for s in stops if isinstance(s, dict)]
    return out


def stops_cached(token):
    if time.time() - STOPS["at"] >= STOPS_SECONDS:
        STOPS["at"] = time.time()
        try:
            STOPS["by_train"] = fetch_stops(token)
        except Exception as e:
            print("stops: %s: %s" % (type(e).__name__, e))
    return STOPS["by_train"]


# The station list behind the search box: fetched once a day.
STATIONS = {"at": 0.0, "items": []}


def stations_cached(token):
    if STATIONS["items"] and time.time() - STATIONS["at"] < 86400:
        return STATIONS["items"]
    STATIONS["at"] = time.time()
    try:
        rows = njt.api_post("getStationList", {"token": token})
        items = [{"code": str(r.get("STATION_2CHAR")).strip(),
                  "name": str(r.get("STATIONNAME")).strip()}
                 for r in rows if isinstance(r, dict)
                 and r.get("STATION_2CHAR") and r.get("STATIONNAME")]
        STATIONS["items"] = sorted(items, key=lambda s: s["name"].lower())
    except Exception as e:
        print("stations: %s: %s" % (type(e).__name__, e))
    return STATIONS["items"]


# What the app saw of every train today -- (service_date, train) -> {track we
# called (or None), predicted_at, unposted_at, posted_at, watched} in epoch
# seconds -- so that when NJ Transit posts the track it can say whether the
# call held and whether it was early enough to count, and so each row can show
# how far ahead of departure the call and the posting came. Reloaded from the
# scorecard after every poll, so it survives a restart.
MEMO = {}


def service_date_eastern():
    d = now_eastern()
    if d.hour < 3:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def remember(tid, p, now=None):
    """Record first predictions; when NJ Transit posts, turn a matching call
    into 'verified' (100%) -- but only if it was provably showing at least
    stats.MIN_LEAD seconds before the posting, measured to the last poll at
    which the board was still blank (stats.proven_lead). A matching call
    closer than that is marked late and stays 'official', earning no credit;
    a wrong call is flagged whatever its timing."""
    now = now or time.time()
    key = (service_date_eastern(), tid)
    m = MEMO.get(key)
    if p["tier"] == "official":
        if m is None:
            # first seen already posted: no honest posting time, never a fair test
            MEMO[key] = {"track": None, "predicted_at": None, "unposted_at": None,
                         "posted_at": now, "watched": 0}
        else:
            if m.get("posted_at") is None:
                m["posted_at"] = now
            if m.get("track"):
                if m["track"] != p["track"]:
                    p["missed"] = m["track"]
                elif (stats.proven_lead(m["predicted_at"], m.get("unposted_at"),
                                        m.get("posted_at")) or 0) >= stats.MIN_LEAD:
                    p["tier"] = "verified"
                    p["confidence"] = 1.0
                else:
                    p["late"] = True
    else:
        if m is None:
            m = MEMO[key] = {"track": None, "predicted_at": None, "unposted_at": now,
                             "posted_at": None, "watched": 1}
        if p["tier"] == "predicted" and not m.get("track"):
            m["track"], m["predicted_at"] = p["track"], now
        if m.get("posted_at") is None:
            m["unposted_at"] = now            # the board is still blank for this train
    if len(MEMO) > 1500:                      # keep today and yesterday only
        keep = {key[0], (now_eastern() - timedelta(days=1)).strftime("%Y-%m-%d")}
        for k in [k for k in MEMO if k[0] not in keep]:
            del MEMO[k]


def leads_for(tid, depart_epoch):
    """Minutes before the scheduled departure at which we called the track
    (BTB) and at which NJ Transit posted it, or None for either. NJ Transit's
    is only given when the posting was seen happen: a train first seen already
    posted has no honest posting time."""
    m = MEMO.get((service_date_eastern(), tid))
    if not m or not depart_epoch:
        return None, None
    btb = int(round((depart_epoch - m["predicted_at"]) / 60)) if m.get("predicted_at") else None
    njt_ = (int(round((depart_epoch - m["posted_at"]) / 60))
            if m.get("posted_at") and m.get("watched", 1) else None)
    return btb, njt_


def build_board(books, hist, token):
    vehicles = fetch_vehicles(token)
    circuits = {tid: v["ckt"] for tid, v in vehicles.items() if v["ckt"]}
    # 19Rec is the endpoint NJ Transit designates for real-time use (their
    # support classes getTrainSchedule as "schedule data"). Same fields, same
    # rows, higher assured limit.
    payload = njt.api_post("getTrainSchedule19Rec",
                           {"token": token, "station": njt.STATION, "line": ""})
    alerts = alerts_cached(token)
    stops = stops_cached(token)
    stations_cached(token)
    rows = []
    for item in njt.board_items(payload):
        if NJT_ONLY and not str(item.get("TRAIN_ID", "")).strip().isdigit():
            continue
        p = predict(item, books, circuits, hist)
        tid0 = str(item.get("TRAIN_ID", "")).strip()
        remember(tid0, p)
        arrived, at = arrival(item, vehicles.get(tid0))
        sched = item.get("SCHED_DEP_DATE")
        try:
            dep = datetime.strptime(sched, "%d-%b-%Y %I:%M:%S %p")
            east_now = now_eastern()
            mins = int((dep - east_now).total_seconds() / 60)
            dep_str = dep.strftime("%I:%M %p").lstrip("0")
            # scheduled departure as UTC epoch seconds, so clients can measure
            # leads without knowing the time zone. Eastern -> UTC via the current
            # offset, exact except for a departure on the far side of a DST change.
            utc_off = datetime.now(timezone.utc).replace(tzinfo=None) - east_now
            depart_epoch = int((dep + utc_off).replace(tzinfo=timezone.utc).timestamp())
        except (ValueError, TypeError):
            mins, dep_str, depart_epoch = None, sched or "", None
        tid = str(item.get("TRAIN_ID", ""))
        lead_btb, lead_njt = leads_for(tid, depart_epoch)
        tstops = stops.get(tid, [])
        rows.append({
            "train": tid,
            "operator": ("Amtrak" if tid[:1] == "A" else "SEPTA" if tid[:1] == "S"
                         else "Non-revenue" if tid[:1] == "X" else "NJT"),
            "line": line_name(item),
            # NJ Transit's own line colour, stable per line regardless of
            # status (Amtrak's is the yellow one, and those rows are filtered out)
            "color": item.get("BACKCOLOR") or "",
            "destination": str(item.get("DESTINATION", "")).replace("&#9992", "✈"),
            "depart": dep_str,
            "depart_epoch": depart_epoch,
            "minutes": mins,
            "status": item.get("STATUS", ""),
            "late": item.get("SEC_LATE"),
            "circuit": circuits.get(tid),
            "arrived": arrived,
            "at": at,
            "stops": tstops,                             # [{code, name, time}], NY first
            "arrives": tstops[-1]["time"] if tstops else "",
            "alerts": alerts_for(item, alerts),
            "lead_btb": lead_btb,                        # minutes before departure we called it
            "lead_njt": lead_njt,                        # minutes before departure NJT posted it
            "prediction": p,
        })
    return {"station": "New York Penn Station",
            "fetched_at": now_eastern().strftime("%I:%M:%S %p").lstrip("0"),
            "trains": rows}


# -------------------------------------------------------------------- server

# Fetched on request, remembered for CACHE_SECONDS. A lock so that several
# simultaneous requests after a cold start trigger one fetch, not one each.
CACHE = {"board": None, "error": None, "at": 0.0, "token": None}
CACHE_LOCK = threading.Lock()
BOOKS, HIST = None, None
STATS = None                      # sqlite connection, opened in main()


def get_board(force=False):
    """force: fetch even if the cache is fresh. The poller runs on its own
    clock -- its cycle starts when a fetch begins, the cache's age when one
    ends -- so without this it woke just inside the cache window, skipped,
    and polled at half the rate. The quota hold below still applies."""
    with CACHE_LOCK:
        age = time.time() - CACHE["at"]
        if not force and CACHE["board"] is not None and age < CACHE_SECONDS:
            return CACHE["board"], CACHE["error"], round(age, 1)
        if time.time() < CACHE.get("hold_until", 0):
            return CACHE["board"], CACHE["error"], round(age, 1)
        for attempt in (1, 2):
            try:
                if not CACHE["token"]:
                    # attempt 1 reuses whatever is cached; attempt 2 runs only
                    # after NJT rejected that token and forces a fresh mint.
                    # Minting is limited to ~10/day, so never mint speculatively.
                    CACHE["token"] = njt.get_token(force=(attempt == 2))
                CACHE["board"] = build_board(BOOKS, HIST, CACHE["token"])
                CACHE["error"] = None
                CACHE["at"] = time.time()
                _score(CACHE["board"])
                break
            except njt.AuthError:
                CACHE["token"] = None
                if attempt == 2:
                    CACHE["error"] = "could not authenticate with NJT"
                    CACHE["at"] = time.time()
            except (Exception, SystemExit) as e:
                # keep serving the last good board, with the error shown
                CACHE["error"] = type(e).__name__ + ": " + str(e)
                CACHE["at"] = time.time()        # do not retry on every request
                if "QUOTA" in str(e):
                    # the daily mint limit is spent; every retry burns another
                    # attempt against tomorrow's count. Hold for an hour.
                    CACHE["hold_until"] = time.time() + 3600
                break
        return CACHE["board"], CACHE["error"], 0


def _score(board):
    """Fold a board snapshot into the scorecard, and reload what we predicted
    earlier today. The reload is what lets a verified badge survive a restart:
    MEMO lives in memory, the record does not."""
    if STATS is None or not board:
        return
    try:
        svc = service_date_eastern()
        for tid, pred, actual in stats.record(
                STATS, board["trains"], datetime.now(timezone.utc).isoformat(), svc):
            print("%s  %s predicted %s, actual %s"
                  % ("HIT " if pred == actual else "MISS", tid, pred, actual))
        MEMO.update({(svc, tid): trk
                     for tid, trk in stats.memo_for(STATS, svc).items()})
    except Exception as e:
        print("scorecard: %s: %s" % (type(e).__name__, e))


def token_usage():
    """Today's getToken count against its limit, or None. Failures are silent:
    this is a nicety, and it must never interfere with serving the board."""
    if USAGE["data"] is not None and time.time() - USAGE["at"] < USAGE_TTL:
        return USAGE["data"]
    if not CACHE["token"]:
        return USAGE["data"]
    USAGE["at"] = time.time()
    try:
        rows = njt.get_usage(CACHE["token"])
        if not isinstance(rows, list):
            return USAGE["data"]
        today = now_eastern()
        wanted = {today.strftime("%m/%d/%Y"), "%d/%d/%d" % (today.month, today.day, today.year)}
        used, limit = 0, 10
        for r in rows:
            if r.get("Request_Type") == "getToken":
                if r.get("Request_Date") in wanted:
                    used = int(r.get("Daily_Request_Made") or 0)
                    limit = int(r.get("Usage_Limit") or 10)
                    break
                limit = int(r.get("Usage_Limit") or limit)
        USAGE["data"] = {"used": used, "limit": limit}
    except Exception as e:
        print("usage: %s: %s" % (type(e).__name__, e))
    return USAGE["data"]


def in_quiet_hours():
    return QUIET_START <= now_eastern().hour < QUIET_END


def poller():
    while True:
        started = time.time()
        if not in_quiet_hours():
            try:
                get_board(force=True)
            except Exception as e:
                print("poller: %s: %s" % (type(e).__name__, e))
        # a steady cadence: the fetch itself takes a second or so
        time.sleep(max(0.5, POLL_SECONDS - (time.time() - started)))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/board"):
            board, error, age = get_board()
            # poll + age let the page time its next request to land just
            # after the poller's next fetch: one request per poll, no lag
            body = json.dumps({"board": board, "error": error, "age": age,
                               "poll": POLL_SECONDS})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
        elif self.path.startswith("/api/stats"):
            try:
                payload = stats.summary(STATS) if STATS else {"scored": 0}
                payload["tokens"] = token_usage()
                body = json.dumps(payload)
            except Exception as e:
                body = json.dumps({"scored": 0, "error": str(e)})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
        elif self.path.startswith("/api/stations"):
            body = json.dumps({"stations": STATIONS["items"]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "max-age=3600")
        elif self.path.startswith("/health"):
            body = "ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        else:
            body = PAGE
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        data = body.encode("utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BeatTheBoard - NY Penn</title><style>
:root{--bg:#f6f6f4;--card:#fff;--fg:#17171a;--dim:#6b6b73;--faint:#9b9ba3;--line:#e6e6e2;
--ok:#0a7d32;--okbg:#e7f4eb;--pred:#c2410c;--predbg:#fdf0e7;--warn:#8a6d1f;--warnbg:#fbf4e2;}
@media(prefers-color-scheme:dark){:root{--bg:#131316;--card:#1d1d22;--fg:#ececee;--dim:#9a9aa4;
--faint:#6c6c76;--line:#2c2c33;--ok:#4ade80;--okbg:#122a1b;--pred:#fb923c;--predbg:#33200f;
--warn:#e0be62;--warnbg:#2d2712;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased;
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:22px 16px 64px}
.brand{text-align:center;font-size:24px;font-weight:600;letter-spacing:-.02em;margin:0 0 20px}
h1{font-size:19px;font-weight:600;margin:0;letter-spacing:-.01em}
.sub{color:var(--dim);font-size:13px;margin-top:2px}
.find{position:relative;margin:16px 0 0}
.find input{width:100%;font:inherit;font-size:15px;padding:10px 40px 10px 12px;border:1px solid var(--line);
border-radius:10px;background:var(--card);color:var(--fg);outline:none}
.find input:focus{border-color:var(--faint)}
.find button{position:absolute;right:5px;top:5px;width:32px;height:32px;border:none;background:transparent;
color:var(--faint);font-size:22px;line-height:1;cursor:pointer;border-radius:8px}
.find button:hover{background:var(--line);color:var(--fg)}
.menu{position:absolute;left:0;right:0;top:100%;z-index:5;background:var(--card);border:1px solid var(--line);
border-radius:10px;margin-top:4px;box-shadow:0 8px 24px rgba(0,0,0,.14);overflow:hidden}
.menu:empty{display:none}
.opt{padding:9px 12px;font-size:14px;cursor:pointer}
.opt:hover{background:var(--bg)}
.tally{color:var(--faint);font-size:12px;margin:14px 0 10px}
.err{background:var(--warnbg);color:var(--warn);padding:10px 12px;border-radius:8px;
font-size:13px;margin-bottom:12px}
.empty{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;
color:var(--dim);font-size:14px}
.board{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.row{display:flex;align-items:center;gap:14px;padding:13px 16px 13px 13px;
border-bottom:1px solid var(--line);border-left:4px solid transparent}
.row:last-child{border-bottom:none}
.main{flex:1;min-width:0}
.dest{font-size:15px;font-weight:500;letter-spacing:-.01em;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.alert{color:var(--warn);cursor:pointer;margin-left:7px;font-size:15px}
.meta{font-size:12.5px;color:var(--dim);margin-top:2px}
.note{font-size:12px;color:var(--faint);margin-top:3px}
.miss,.warnline{font-size:12px;color:var(--warn);margin-top:3px}
.alerts{font-size:12px;color:var(--dim);margin-top:6px;padding:8px 10px;background:var(--warnbg);
border-radius:8px;white-space:normal}
.alerts div+div{margin-top:5px}
.alerts a{color:inherit}
.faint{color:var(--faint)}
.dot{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:5px;
vertical-align:1px;background:var(--faint)}
.dot.on{background:var(--ok)}
.right{text-align:right;flex-shrink:0;min-width:74px}
.trk{font-size:38px;font-weight:600;line-height:.92;letter-spacing:-.035em;
font-variant-numeric:tabular-nums}
.trk.ok{color:var(--ok)}.trk.pred{color:var(--pred)}.trk.off{color:var(--faint);font-size:30px}
.tier{font-size:11.5px;margin-top:4px;letter-spacing:.01em}
.tier.ok{color:var(--ok)}.tier.pred{color:var(--pred)}
.tier.warn{color:var(--warn)}.tier.off{color:var(--faint)}
.cand{font-size:12px;color:var(--faint);margin-top:3px}
h2{font-size:11px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;
color:var(--faint);margin:30px 0 10px}
h2 span{text-transform:none;letter-spacing:0;font-weight:400}
.sgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1px;
background:var(--line);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.sitem{background:var(--card);padding:12px 14px}
.sk{font-size:11px;color:var(--faint);letter-spacing:.04em;text-transform:uppercase}
.sv{font-size:21px;font-weight:600;margin-top:3px;letter-spacing:-.02em;
font-variant-numeric:tabular-nums}
.sn{font-size:11.5px;color:var(--faint);margin-top:1px;min-height:15px}
.hd{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.board{background:none;border:none;border-radius:0;overflow:visible}
.row{background:var(--card);border:1px solid var(--line);border-left-width:5px;
border-radius:12px;margin-bottom:9px;padding:15px 17px 15px 14px}
.row:last-child{margin-bottom:0;border-bottom:1px solid var(--line)}
.dest{font-size:16.5px}
.trk{font-size:46px}
.trk.off{font-size:34px}
.right{min-width:84px}
.tokens{margin-top:10px;font-size:12px;color:var(--faint)}
.tokens.warn{color:var(--warn)}
.tokens b{font-variant-numeric:tabular-nums;font-weight:600}
footer{margin-top:26px;font-size:12px;color:var(--faint);line-height:1.75}
footer b{color:var(--dim);font-weight:500}
</style></head><body><div class="wrap">
<div class="brand">BeatTheBoard</div>
<div class="hd">
<div><h1>NY Penn Station Departures</h1><div class="sub" id="sub">loading</div></div>
</div>
<div class="find">
<input id="stop" type="text" placeholder="Your stop - start typing, e.g. Summit" autocomplete="off" spellcheck="false" aria-label="Your stop">
<button id="clear" type="button" title="Show all trains" aria-label="Show all trains">&times;</button>
<div class="menu" id="menu"></div>
</div>
<div class="tally" id="tally"></div>
<div id="err"></div>
<div class="board" id="rows"></div>

<h2>Report card <span id="scorewhen"></span></h2>
<div class="sgrid" id="sgrid"></div>
<div class="tokens" id="tokens"></div>

<footer>
<div><b>confirmed</b> - we called it at least __MIN_LEAD__ s before NJ Transit posted the same track. Closer than that is not counted as beating the board.</div>
<div><b>predicted</b> - our call; NJ Transit has not posted yet, so nothing has confirmed it. Capped at 99%: without their announcement it is never certain.</div>
<div><b>on the board</b> - NJ Transit's posted track, with no confirmed call of ours behind it. Either we never predicted it, we called it too late to count, or we got it wrong - and it says so underneath.</div>
<div><b>BTB +x mins</b> - how long before the scheduled departure we called the track. <b>NJT +x mins</b> - how long before departure NJ Transit posted it.</div>
<div><b>\u26a0\ufe0e</b> - a delay, or an NJ Transit alert for this train or its whole line. Tap it for the details.</div>
<div><b>usually</b> - where this train has gone on past days. Context, not a prediction.</div>
<div>Type your stop above to see only the trains that stop there, with the time they get there. It is remembered on this device. The dot shows whether the train is reporting from Penn yet. Always confirm on the station display before boarding.</div>
</footer></div>
<script>
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
let STATIONS = [], STOP = null, BOARD = null;
const OPEN = new Set();                       // trains whose alert details are expanded
const inp = document.getElementById('stop'), menu = document.getElementById('menu');
// the home stop is remembered on this device
try{ STOP = JSON.parse(localStorage.getItem('btb-stop') || 'null'); }catch(e){ STOP = null; }
if(STOP && STOP.code && STOP.name) inp.value = STOP.name; else STOP = null;
fetch('/api/stations').then(r=>r.json()).then(s=>{ STATIONS = s.stations || []; }).catch(e=>{});

function setStop(s){
  STOP = s; inp.value = s ? s.name : ''; menu.innerHTML = '';
  try{ if(s) localStorage.setItem('btb-stop', JSON.stringify(s)); else localStorage.removeItem('btb-stop'); }catch(e){}
  render();
}
function matches(q){
  q = q.trim().toLowerCase(); if(!q) return [];
  const pre = STATIONS.filter(s=>s.name.toLowerCase().startsWith(q));
  const mid = STATIONS.filter(s=>!s.name.toLowerCase().startsWith(q) && s.name.toLowerCase().includes(q));
  return pre.concat(mid).slice(0, 12);
}
function showMenu(){
  menu.innerHTML = matches(inp.value).map(s=>'<div class="opt" data-code="'+esc(s.code)+'">'+esc(s.name)+'</div>').join('');
}
inp.addEventListener('input', ()=>{ showMenu(); if(!inp.value.trim() && STOP) setStop(null); });
inp.addEventListener('focus', showMenu);
inp.addEventListener('keydown', e=>{
  if(e.key==='Enter'){ const m = matches(inp.value); if(m.length) setStop(m[0]); inp.blur(); e.preventDefault(); }
  else if(e.key==='Escape'){ menu.innerHTML = ''; inp.blur(); }
});
menu.addEventListener('pointerdown', e=>{
  const o = e.target.closest('.opt'); if(!o) return; e.preventDefault();
  setStop(STATIONS.find(s=>s.code===o.dataset.code) || null); inp.blur();
});
document.addEventListener('click', e=>{ if(!e.target.closest('.find')) menu.innerHTML = ''; });
document.getElementById('clear').addEventListener('click', ()=>{ setStop(null); inp.focus(); });
document.getElementById('rows').addEventListener('click', e=>{
  const a = e.target.closest('.alert'); if(!a) return;
  const id = a.dataset.train; if(OPEN.has(id)) OPEN.delete(id); else OPEN.add(id); render();
});

const unit = n => Math.abs(n)===1 ? 'min' : 'mins';
const signed = n => (n>=0?'+':'')+n;
function render(){
  if(!BOARD) return;
  const b = BOARD;
  document.getElementById('sub').textContent = 'updated ' + b.fetched_at;
  // a train whose stop list has not loaded yet is kept rather than hidden
  const list = STOP ? b.trains.filter(t => !(t.stops && t.stops.length) || t.stops.some(s=>s.code===STOP.code)) : b.trains;
  let off=0,pred=0,none=0;
  list.forEach(t=>{const k=t.prediction.tier;
    if(k==='official'||k==='verified')off++; else if(k==='predicted')pred++; else none++;});
  document.getElementById('tally').textContent =
    list.length+(list.length===1?' train':' trains')+(STOP?' to '+STOP.name:'')+
    ' \u00b7 '+off+' on the board \u00b7 '+pred+' predicted \u00b7 '+none+' waiting';
  if(!list.length){
    document.getElementById('rows').innerHTML = '<div class="empty">No trains to '+esc(STOP.name)+' on the board right now.</div>';
    return;
  }
  document.getElementById('rows').innerHTML = list.map(t=>{
    const p=t.prediction; let num='--', ncls='off', tier='', tcls='off', extra='';
    const al = t.alerts || [];
    const prob = al.find(a=>a.scope!=='line');
    if(prob) extra += '<div class="warnline">'+esc(prob.text)+'</div>';
    const lead = [];
    if(t.lead_btb!==null && t.lead_btb!==undefined) lead.push('BTB '+signed(t.lead_btb)+' '+unit(t.lead_btb));
    if(t.lead_njt!==null && t.lead_njt!==undefined) lead.push('NJT '+signed(t.lead_njt)+' '+unit(t.lead_njt));
    else if(lead.length && p.tier!=='official' && p.tier!=='verified') lead.push('NJT not posted yet');
    if(lead.length) extra += '<div class="note">'+lead.join(' \u00b7 ')+'</div>';
    if(p.tier==='verified'){ num=p.track; ncls='ok'; tcls='ok'; tier='confirmed'; }
    else if(p.tier==='official'){ num=p.track; ncls='ok'; tcls='off'; tier='on the board';
      if(p.missed) extra+='<div class="miss">we predicted '+esc(p.missed)+' - that was wrong</div>';
      else if(p.late) extra+='<div class="cand">we called it too, but under __MIN_LEAD__ s before NJ Transit did - not counted</div>'; }
    else if(p.tier==='predicted'){ num=p.track; ncls='pred'; tcls='pred';
      tier='predicted'+(p.confidence?' '+Math.min(99,Math.round(p.confidence*100))+'%':''); }
    else if(p.tier==='history'){ tcls='off'; tier='no signal';
      extra+='<div class="cand">usually '+p.candidates.map(c=>esc(c.track)+' ('+Math.round(c.share*100)+'%)').join(', ')+'</div>'; }
    else { tier='not posted'; }
    if(al.length) extra += '<div class="alerts"'+(OPEN.has(t.train)?'':' hidden')+'>'+al.map(a=>'<div>'+esc(a.text)+
      (a.when?' <span class="faint">\u00b7 posted '+esc(a.when)+'</span>':'')+
      (a.url?' <a href="'+esc(a.url)+'" target="_blank" rel="noopener">details</a>':'')+'</div>').join('')+'</div>';
    const mins = t.minutes===null?'':(t.minutes<=0?'now':'in '+t.minutes+' min');
    let arr = '';
    if(STOP){ const s=(t.stops||[]).find(s=>s.code===STOP.code); if(s && s.time) arr='arrives '+esc(STOP.name)+' at '+esc(s.time); }
    else if(t.arrives) arr = 'arrives at '+esc(t.arrives);
    const meta = [esc(t.line), t.depart?'departs at '+esc(t.depart):'', mins, arr].filter(Boolean).join(' \u00b7 ');
    const icon = al.length ? '<span class="alert" data-train="'+esc(t.train)+'" title="Delay or alert - tap for details">\u26a0\ufe0e</span>' : '';
    return '<div class="row" style="border-left-color:'+(esc(t.color)||'transparent')+'">'+
      '<div class="main"><div class="dest">'+esc(t.destination)+icon+'</div>'+
      '<div class="meta"><span class="dot'+(t.arrived?' on':'')+'"></span>'+meta+'</div>'+
      extra+'</div>'+
      '<div class="right"><div class="trk '+ncls+'">'+esc(num)+'</div>'+
      '<div class="tier '+tcls+'">'+esc(tier)+'</div></div></div>';
  }).join('');
}
let due = __TICK_MS__;   // ms until the next board fetch
async function tick(){
  try{
    const d = await (await fetch('/api/board')).json();
    // ask again just after the poller's next fetch lands: each poll shows
    // within about half a second, at one request per poll. If the poller is
    // late or asleep (quiet hours), fall back to a plain interval.
    due = (d.poll > 0 && d.age < d.poll) ? Math.max(400, (d.poll - d.age) * 1000 + 500) : __TICK_MS__;
    document.getElementById('err').innerHTML = d.error ? '<div class="err">'+esc(d.error)+'</div>' : '';
    if(d.board){ BOARD = d.board; render(); }
  }catch(e){ due = __TICK_MS__; }
  finally{ setTimeout(tick, due); }
}
async function score(){
  try{
    const s = await (await fetch('/api/stats')).json();
    const tk = document.getElementById('tokens');
    if(s.tokens){
      const used = s.tokens.used, lim = s.tokens.limit || 10;
      tk.className = 'tokens' + (used >= lim - 3 ? ' warn' : '');
      tk.innerHTML = 'API tokens minted today <b>' + used + ' / ' + lim + '</b>' +
        (used >= lim ? ' \u00b7 limit reached, predictions resume after midnight' : '');
    } else { tk.textContent = ''; }
    const g = document.getElementById('sgrid');
    if(!s.scored){
      g.innerHTML='<div class="sitem"><div class="sk">Nothing scored yet</div>'+
        '<div class="sn">Numbers appear once trains start posting.</div></div>';
      return;
    }
    const us = d => d ? d.slice(5,7)+'-'+d.slice(8,10)+'-'+d.slice(0,4) : '';
    document.getElementById('scorewhen').textContent =
      '\u00b7 '+s.days+(s.days===1?' day':' days')+' \u00b7 '+us(s.first_day)+' to '+us(s.last_day);
    const m = v => (v===null||v===undefined)?'--':v+' min';
    const it = (k,v,n) => '<div class="sitem"><div class="sk">'+k+'</div><div class="sv">'+v+
      '</div><div class="sn">'+(n||'')+'</div></div>';
    g.innerHTML =
      it('Coverage', s.coverage+'%', s.predicted+' of '+s.scored+' trains called '+s.min_lead+'+ s before the board'+
         (s.late ? ' \u00b7 '+s.late+' called too late to count' : '')) +
      it('Accuracy', s.accuracy===null?'--':s.accuracy+'%',
         s.correct+' of '+s.predicted+' predictions correct') +
      it('BeatTheBoard', m(s.ours_median), 'median before departure \u00b7 mean '+m(s.ours_mean)) +
      it('NJ Transit board', m(s.njt_median), 'median before departure \u00b7 mean '+m(s.njt_mean));
  }catch(e){}
}
tick();
score(); setInterval(score, 30000);
</script></body></html>"""

# __TICK_MS__ is the page's fallback interval; normally it times itself to
# the poller (see tick() in the script above).
PAGE = (PAGE.replace("__TICK_MS__", str(max(2, POLL_SECONDS or 15) * 1000))
            .replace("__MIN_LEAD__", str(stats.MIN_LEAD)))


def main():
    global BOOKS, HIST, STATS
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    njt.load_env()
    BOOKS = load_codebooks(rebuild=args.rebuild)
    HIST = load_history()
    print("history: %d trains" % len(HIST))

    if args.once:
        board = build_board(BOOKS, HIST, njt.get_token())
        print("\n%-7s %-22s %-9s %-7s %-12s %-10s %s"
              % ("TRAIN", "DESTINATION", "DEPARTS", "TRACK", "ARRIVED", "SOURCE", "CIRCUIT"))
        print("-" * 88)
        for t in board["trains"]:
            p = t["prediction"]
            src = p["tier"] + ("/" + p["signal"] if p.get("signal") else "")
            print("%-7s %-22s %-9s %-7s %-12s %-10s %s"
                  % (t["train"], t["destination"][:22], t["depart"],
                     p["track"] or "--", t["at"], src, t.get("circuit") or ""))
        return

    try:
        STATS = stats.connect()
        svc = service_date_eastern()
        MEMO.update({(svc, tid): trk for tid, trk in stats.memo_for(STATS, svc).items()})
        print("scorecard: %s (%d departures on record today)"
              % (stats.DB_PATH, len(MEMO)))
    except Exception as e:
        print("scorecard unavailable (%s: %s) -- serving without it"
              % (type(e).__name__, e))
        STATS = None

    if POLL_SECONDS:
        threading.Thread(target=poller, daemon=True).start()
        print("polling every %ds, quiet %02d:00-%02d:00 Eastern"
              % (POLL_SECONDS, QUIET_START, QUIET_END))

    print("\nserving http://%s:%d   (Ctrl-C to stop)"
          % ("localhost" if BIND == "127.0.0.1" else BIND, PORT))
    HTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
