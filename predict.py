"""
Live track predictor for NY Penn.

Built on an empirical finding, not a model: getTrainSchedule publishes a
GPSLATITUDE/GPSLONGITUDE pair that is a fixed per-track berth coordinate, and it
populates BEFORE the TRACK field does. So the departure track can simply be
decoded rather than predicted, for any train whose GPS has appeared.

    python predict.py            # one shot
    python predict.py --watch    # refresh every 30s

Prediction tiers, strongest first:
    OFFICIAL  TRACK is populated -- just report it, never override
    GPS       coordinate matches a known per-track berth point (observed 22/22)
    HISTORY   per-train modal track from track_postings (needs weeks of data)
    --        nothing to say; abstain rather than guess
"""

import argparse
import json
import math
import sqlite3
import sys
import time
from collections import Counter, defaultdict

import njt_logger as njt

DB = njt.DB_PATH
NYP_LAT, NYP_LON = 40.7498, -73.9918
# Codebook entries must sit near Penn. Guards against junk fixes such as the
# observed 40.2969,-73.9880 (~50 km south) that would otherwise poison the map.
PENN_BOX = 0.006

MIN_CODEBOOK_SIGHTINGS = 2   # ignore coordinates seen only once
MIN_PURITY = 1.0             # a coordinate must map to exactly one track


def near_penn(lat, lon):
    try:
        return (abs(float(lat) - NYP_LAT) < PENN_BOX
                and abs(float(lon) - NYP_LON) < PENN_BOX)
    except (TypeError, ValueError):
        return False


def build_codebook(conn):
    """coordinate -> track, learned from rows where the track was already posted."""
    votes = defaultdict(Counter)
    for track, raw in conn.execute(
            "SELECT track, raw FROM observations "
            "WHERE track IS NOT NULL AND track != ''"):
        t = str(track).strip()
        if not t.isdigit():
            continue
        try:
            item = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        lat, lon = item.get("GPSLATITUDE"), item.get("GPSLONGITUDE")
        if lat and lon and near_penn(lat, lon):
            votes[(str(lat), str(lon))][t] += 1

    book, rejected = {}, 0
    for coord, counter in votes.items():
        total = sum(counter.values())
        track, n = counter.most_common(1)[0]
        if total >= MIN_CODEBOOK_SIGHTINGS and n / total >= MIN_PURITY:
            book[coord] = track
        else:
            rejected += 1
    return book, rejected


def build_history(conn):
    """Fallback: per-train modal track. Thin until you have weeks of days."""
    hist = defaultdict(Counter)
    for train_id, track in conn.execute(
            "SELECT train_id, track FROM track_postings WHERE track != ''"):
        if str(track).strip().isdigit():
            hist[train_id][str(track).strip()] += 1
    return hist


def platform_of(track):
    try:
        return math.ceil(int(track) / 2)
    except (TypeError, ValueError):
        return None


def predict_one(item, book, hist):
    """-> (track, tier, confidence_note)"""
    posted = (item.get("TRACK") or "").strip()
    if posted:
        return posted, "OFFICIAL", ""

    lat, lon = item.get("GPSLATITUDE"), item.get("GPSLONGITUDE")
    if lat and lon:
        hit = book.get((str(lat), str(lon)))
        if hit:
            return hit, "GPS", "berthed"
        if near_penn(lat, lon):
            return None, "AT-PENN", "at Penn, coordinate not in codebook yet"

    counter = hist.get(item.get("TRAIN_ID"))
    if counter:
        total = sum(counter.values())
        track, n = counter.most_common(1)[0]
        if total >= 3:
            return track, "HISTORY", "%d/%d past runs" % (n, total)
    return None, "--", ""


def run_once(conn, token, book, hist):
    payload = njt.api_post("getTrainSchedule",
                           {"token": token, "station": njt.STATION})
    items = njt.board_items(payload)

    print("\n%-7s %-19s %-22s %-7s %-9s %s"
          % ("TRAIN", "LINE", "DESTINATION", "TRACK", "SOURCE", "NOTE"))
    print("-" * 92)

    gained = 0
    for item in items:
        track, tier, note = predict_one(item, book, hist)
        if tier in ("GPS", "HISTORY"):
            gained += 1
        plat = platform_of(track) if track else None
        shown = str(track) if track else "--"
        if plat and tier != "OFFICIAL":
            shown += " (P%d)" % plat
        print("%-7s %-19s %-22s %-7s %-9s %s"
              % (str(item.get("TRAIN_ID", ""))[:7],
                 str(item.get("LINE", ""))[:19],
                 str(item.get("DESTINATION", ""))[:22].replace("&#9992", "*"),
                 shown, tier, note))

    official = sum(1 for i in items if (i.get("TRACK") or "").strip())
    print("\n%d trains | %d already official | %d predicted ahead of the board"
          % (len(items), official, gained))
    return gained


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="refresh every 30s")
    args = ap.parse_args()

    njt.load_env()
    conn = sqlite3.connect(DB)

    book, rejected = build_codebook(conn)
    hist = build_history(conn)
    print("codebook: %d coordinates -> track (%d rejected as ambiguous/rare)"
          % (len(book), rejected))
    print("history:  %d trains with past postings" % len(hist))
    if len(book) < 5:
        print("\n! Codebook is thin. Run the logger longer -- coordinates are only")
        print("  learned from trains observed AFTER their track posted.")

    token = njt.get_token()
    try:
        while True:
            try:
                run_once(conn, token, book, hist)
            except njt.AuthError:
                token = njt.get_token(force=True)
                continue
            if not args.watch:
                break
            time.sleep(30)
            # Cheap refresh: new postings teach new coordinates.
            book, _ = build_codebook(conn)
            hist = build_history(conn)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
