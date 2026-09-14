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
The board is fetched from NJT when a request arrives and remembered for
CACHE_SECONDS, so a burst of refreshes costs two API calls, not two per
refresh -- and nothing is fetched while nobody is looking. This is what lets
the app run on a host that switches it off when idle (Azure Container Apps
scale-to-zero): there is no background loop that needs the process alive.

Two environment variables matter for hosting:
    BIND   127.0.0.1 (default, this machine only)  |  0.0.0.0 in a container
    PORT   8080

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
import json
import math
import os
import sqlite3
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import njt_logger as njt

HISTORY_DB = njt.DB_PATH          # written by run_logger.py (optional)
BENCH_DB = "benchmark.db"         # written by benchmark.py
CODEBOOK_PATH = "codebook.json"

BIND = os.environ.get("BIND", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "20"))

GPS_MIN_N, GPS_MIN_PURITY = 3, 0.98    # chosen by held-out sweep
CKT_MIN_N, CKT_MIN_PURITY = 20, 0.95

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

def _sift(votes, min_n, min_purity):
    book, rejected = {}, []
    for key, counter in votes.items():
        total = sum(counter.values())
        if total < min_n:
            continue
        track, n = counter.most_common(1)[0]
        purity = n / total
        if purity >= min_purity:
            book[key] = {"track": track, "n": total, "purity": round(purity, 3)}
        else:
            rejected.append((key, dict(counter)))
    return book, rejected


def _votes_from_history(gvotes, cvotes):
    """run_logger.py's DB. Berth votes from posted rows at Penn; circuit votes
    from every vehicle row of a train-day, against the track it finally posted."""
    conn = _open_ro(HISTORY_DB)
    if not conn:
        return 0
    n = 0
    try:
        for lat, lon, track in conn.execute(
                "SELECT gps_lat, gps_lon, track FROM observations "
                "WHERE gps_lat IS NOT NULL AND track != '' AND at_penn = 1"):
            t = str(track).strip()
            if t.isdigit():
                gvotes[lat + "," + lon][t] += 1
                n += 1
        for ckt, track in conn.execute(
                "SELECT v.ics_track_ckt, p.track FROM vehicle_positions v "
                "JOIN track_postings p ON p.train_id = v.train_id "
                "                      AND p.service_date = v.service_date "
                "WHERE v.ics_track_ckt IS NOT NULL AND v.ics_track_ckt != '' "
                "  AND p.track != ''"):
            t = str(track).strip()
            if t.isdigit():
                cvotes[ckt][t] += 1
                n += 1
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    return n


def _votes_from_benchmark(gvotes, cvotes):
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
                n += 1
            ckt = item.get("_circuit")
            if ckt:
                cvotes[ckt][t] += 1
                n += 1
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    return n


def build_codebooks(verbose=True):
    gvotes, cvotes = defaultdict(Counter), defaultdict(Counter)
    n_hist = _votes_from_history(gvotes, cvotes)
    n_bench = _votes_from_benchmark(gvotes, cvotes)
    if not (n_hist or n_bench):
        sys.exit("No data to learn from: neither %s nor %s has observations yet."
                 % (HISTORY_DB, BENCH_DB))

    gps, grej = _sift(gvotes, GPS_MIN_N, GPS_MIN_PURITY)
    cir, crej = _sift(cvotes, CKT_MIN_N, CKT_MIN_PURITY)
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

def platform_of(track):
    try:
        t = int(track)
    except (TypeError, ValueError):
        return None
    return math.ceil(t / 2) if t <= 16 else None


def where_to_wait(track):
    """Tracks share an island in pairs (9 and 10 are one platform), so the
    useful direction is which staircase -- not a platform number, which
    confuses people when it does not match the track number."""
    p = platform_of(track)
    if not p:
        return ""
    return "Head for the Tracks %d-%d stairs" % (p * 2 - 1, p * 2)


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
        return {"track": posted, "tier": "official", "signal": None,
                "confidence": None, "note": where_to_wait(posted)}

    tid = str(item.get("TRAIN_ID") or "").strip()
    ckt = circuits.get(tid)
    hit = books["circuits"].get(ckt) if ckt else None
    if hit:
        return {"track": hit["track"], "tier": "predicted", "signal": "circuit",
                "confidence": hit["purity"], "note": where_to_wait(hit["track"])}

    lat, lon = item.get("GPSLATITUDE"), item.get("GPSLONGITUDE")
    if lat and lon:
        hit = books["gps"].get(str(lat) + "," + str(lon))
        if hit:
            return {"track": hit["track"], "tier": "predicted", "signal": "berth",
                    "confidence": hit["purity"], "note": where_to_wait(hit["track"])}

    counter = hist.get(tid)
    if counter and sum(counter.values()) >= 4:
        total = sum(counter.values())
        top = counter.most_common(3)
        return {"track": None, "tier": "history", "signal": None,
                "confidence": round(top[0][1] / total, 2),
                "candidates": [{"track": t, "share": round(n / total, 2)} for t, n in top],
                "note": "no live signal - historical tracks only"}
    return {"track": None, "tier": "none", "signal": None, "confidence": None,
            "note": "not yet posted"}


# NJT train numbers are numeric. Letter prefixes are other operators sharing the
# board -- A=Amtrak, S=SEPTA, X=non-revenue equipment moves -- none of which a
# rider here can board, so they are dropped. Set NJT_ONLY=0 to show them again.
NJT_ONLY = os.environ.get("NJT_ONLY", "1") != "0"


# What the app told people earlier today, so that when NJ Transit posts the
# track it can say whether the prediction held. In-process only: it resets when
# the container restarts (scale-to-zero), after which already-posted trains just
# show as official until the next prediction is made.
MEMO = {}


def service_date_eastern():
    d = now_eastern()
    if d.hour < 3:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def remember(tid, p):
    """Record first predictions; turn a matching official posting into
    'verified' (100%), or flag the earlier prediction on a miss."""
    key = (service_date_eastern(), tid)
    if p["tier"] == "predicted":
        MEMO.setdefault(key, p["track"])
    elif p["tier"] == "official" and key in MEMO:
        if MEMO[key] == p["track"]:
            p["tier"] = "verified"
            p["confidence"] = 1.0
        else:
            p["missed"] = MEMO[key]
    if len(MEMO) > 1500:                      # keep today and yesterday only
        keep = {key[0], (now_eastern() - timedelta(days=1)).strftime("%Y-%m-%d")}
        for k in [k for k in MEMO if k[0] not in keep]:
            del MEMO[k]


def build_board(books, hist, token):
    vehicles = fetch_vehicles(token)
    circuits = {tid: v["ckt"] for tid, v in vehicles.items() if v["ckt"]}
    payload = njt.api_post("getTrainSchedule",
                           {"token": token, "station": njt.STATION})
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
        rows.append({
            "train": tid,
            "operator": ("Amtrak" if tid[:1] == "A" else "SEPTA" if tid[:1] == "S"
                         else "Non-revenue" if tid[:1] == "X" else "NJT"),
            "line": item.get("LINE", ""),
            "destination": str(item.get("DESTINATION", "")).replace("&#9992", "✈"),
            "depart": dep_str,
            "depart_epoch": depart_epoch,
            "minutes": mins,
            "status": item.get("STATUS", ""),
            "late": item.get("SEC_LATE"),
            "circuit": circuits.get(tid),
            "arrived": arrived,
            "at": at,
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


def get_board():
    with CACHE_LOCK:
        age = time.time() - CACHE["at"]
        if CACHE["board"] is not None and age < CACHE_SECONDS:
            return CACHE["board"], CACHE["error"], round(age)
        for attempt in (1, 2):
            try:
                if not CACHE["token"]:
                    CACHE["token"] = njt.get_token()
                CACHE["board"] = build_board(BOOKS, HIST, CACHE["token"])
                CACHE["error"] = None
                CACHE["at"] = time.time()
                break
            except njt.AuthError:
                CACHE["token"] = None            # re-mint once, then give up
                if attempt == 2:
                    CACHE["error"] = "could not authenticate with NJT"
            except (Exception, SystemExit) as e:
                # keep serving the last good board, with the error shown
                CACHE["error"] = type(e).__name__ + ": " + str(e)
                CACHE["at"] = time.time()        # do not retry on every request
                break
        return CACHE["board"], CACHE["error"], 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/board"):
            board, error, age = get_board()
            body = json.dumps({"board": board, "error": error, "age": age})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
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
<title>Penn Track Engine</title><style>
:root{--bg:#f7f7f5;--card:#fff;--fg:#1a1a18;--dim:#6b6b66;--line:#e4e4e0;
--official:#0a7d32;--official-bg:#e8f5ec;--pred:#1257a8;--pred-bg:#e8f0fb;
--hist:#8a6d1f;--hist-bg:#fbf4e2;--none:#8a8a85;}
@media(prefers-color-scheme:dark){:root{--bg:#16161a;--card:#1e1e24;--fg:#ececf0;
--dim:#9a9aa4;--line:#2e2e36;--official:#4ade80;--official-bg:#132a1c;
--pred:#7cb0f5;--pred-bg:#12233c;--hist:#e0be62;--hist-bg:#2e2712;--none:#71717a;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
.wrap{max-width:940px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:20px;margin:0 0 2px}.sub{color:var(--dim);font-size:13px;margin-bottom:18px}
.stats{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:8px 12px;font-size:12px}.stat b{display:block;font-size:17px;margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden}
th{text-align:left;font-size:11px;letter-spacing:.05em;text-transform:uppercase;
color:var(--dim);padding:10px 12px;border-bottom:1px solid var(--line);font-weight:600}
td{padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
.trk{font-size:19px;font-weight:700;letter-spacing:-.02em}
.badge{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.05em;
padding:2px 6px;border-radius:4px;text-transform:uppercase;margin-top:3px;margin-right:4px}
.b-official{background:var(--official-bg);color:var(--official)}
.b-predicted{background:var(--pred-bg);color:var(--pred)}
.b-history{background:var(--hist-bg);color:var(--hist)}
.b-verified{background:var(--official-bg);color:var(--official)}
.pill{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.05em;padding:3px 8px;border-radius:4px;text-transform:uppercase;white-space:nowrap;margin-top:2px}
.p-on{background:var(--official-bg);color:var(--official)}
.p-off{background:var(--line);color:var(--dim)}
.miss{font-size:12px;color:var(--hist);margin-top:3px}
.dim{color:var(--dim)}.note{font-size:12px;color:var(--dim);margin-top:3px}
.dash{color:var(--none);font-size:19px}
.cand{font-size:12px;color:var(--dim)}
.op{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.04em}
.err{background:#fde8e8;color:#9b1c1c;padding:10px 12px;border-radius:8px;margin-bottom:14px}
footer{margin-top:22px;font-size:12px;color:var(--dim);line-height:1.7}
</style></head><body><div class="wrap">
<h1>NY Penn - Track Engine</h1>
<div class="sub" id="sub">loading...</div>
<div class="stats" id="stats"></div>
<div id="err"></div>
<table><thead><tr><th>Train</th><th>Destination</th><th>Departs</th><th>Track</th><th>Arrived</th></tr></thead>
<tbody id="rows"></tbody></table>
<footer>
<div><b>predicted</b> (blue, with a percentage) - we have made a call; NJ Transit has not posted the track yet, so nothing has confirmed or denied it.</div>
<div><b>verified</b> (green, 100%) - we predicted it, then NJ Transit posted the same track. Prediction confirmed.</div>
<div><b>official</b> (green, no percentage) - NJ Transit has posted the track and we have no confirmed prediction to show for it. That is two cases: we never predicted this train (no signal), or we predicted it wrong - in which case an amber line underneath says "we predicted 12 - that was wrong."</div>
<div><b>history</b> - what this train number has done on past days. Context only, not a prediction.</div>
<div>Always confirm on the station display before boarding.</div>
</footer></div>
<script>
async function tick(){
  try{
    const r = await fetch('/api/board'); const d = await r.json();
    document.getElementById('err').innerHTML = d.error ? '<div class="err">'+d.error+'</div>' : '';
    if(!d.board){return;}
    const b = d.board;
    document.getElementById('sub').textContent = b.station + ' - updated ' + b.fetched_at;
    let off=0,pred=0,ver=0,none=0;
    b.trains.forEach(t=>{const k=t.prediction.tier;
      if(k==='official'||k==='verified'){off++; if(k==='verified')ver++;}
      else if(k==='predicted')pred++; else none++;});
    document.getElementById('stats').innerHTML =
      '<div class="stat">Trains<b>'+b.trains.length+'</b></div>'+
      '<div class="stat">On the board<b>'+off+'</b></div>'+
      '<div class="stat">Predicted early<b>'+pred+'</b></div>'+
      '<div class="stat">Verified<b>'+ver+'</b></div>'+
      '<div class="stat">No signal<b>'+none+'</b></div>';
    document.getElementById('rows').innerHTML = b.trains.map(t=>{
      const p=t.prediction; let cell;
      if(p.track){
        cell='<div class="trk">'+p.track+'</div><span class="badge b-'+p.tier+'">'+
          p.tier+(p.confidence?' '+Math.round(p.confidence*100)+'%':'')+'</span>'+
          (p.note?'<div class="note">'+p.note+'</div>':'')+
          (p.missed?'<div class="miss">we predicted '+p.missed+' - that was wrong</div>':'');
      } else if(p.tier==='history'){
        cell='<div class="dash">--</div><span class="badge b-history">history</span>'+
          '<div class="cand">usually '+p.candidates.map(c=>c.track+' ('+Math.round(c.share*100)+'%)').join(', ')+'</div>';
      } else {
        cell='<div class="dash">--</div><div class="note">not yet posted</div>';
      }
      const mins = t.minutes===null?'':(t.minutes<=0?'<b>now</b>':t.minutes+' min');
      return '<tr><td><b>'+t.train+'</b><div class="op">'+t.operator+'</div></td>'+
        '<td>'+t.destination+'<div class="note">'+t.line+'</div></td>'+
        '<td>'+t.depart+'<div class="note">'+mins+'</div></td>'+
        '<td>'+cell+'</td>'+
        '<td><span class="pill '+(t.arrived?'p-on':'p-off')+'">'+t.at+'</span></td></tr>';
    }).join('');
  }catch(e){}
}
tick(); setInterval(tick, 15000);
</script></body></html>"""


def main():
    global BOOKS, HIST
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

    print("\nserving http://%s:%d   (board fetched on request, cached %ds; Ctrl-C to stop)"
          % ("localhost" if BIND == "127.0.0.1" else BIND, PORT, CACHE_SECONDS))
    HTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
