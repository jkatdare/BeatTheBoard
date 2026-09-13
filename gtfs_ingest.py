"""
Fetch and audit NJ TRANSIT's static GTFS-RAIL feed.

This does NOT replace the RailData logger. GTFS carries no track or platform
data for NJT -- TRACK exists only on RailData's departure board. GTFS is here
for one reason: to derive each Penn departure's MOVEMENT TYPE
(turn / drop-and-go / load-and-go), which is the real determinant of track
assignment and the input to the hard physical constraints:

    drop-and-go   -> cannot use stub tracks 1-4
    >= 10 cars    -> cannot use tracks 1-6 (only 8 eastern doors open)

Usage:
    python gtfs_ingest.py                 # download via API, then audit
    python gtfs_ingest.py path/to.zip     # audit a zip you downloaded yourself

The audit prints what the feed actually contains rather than trusting docs --
in particular whether stops.txt has platform-level entries and whether
block_id links an arriving trip to the departing trip it becomes.
"""

import csv
import json
import io
import os
import sys
import zipfile
from collections import Counter, defaultdict

import njt_logger as njt

ZIP_PATH = "njt_gtfs_rail.zip"
NYP_NAME_HINTS = ("new york", "penn")

# Endpoints seen in the wild. The portal is inconsistent about where the rail
# GTFS lives, so try in order rather than guessing one.
CANDIDATES = [
    ("GTFSRAIL", "getGTFS"),
    ("GTFSRT", "getGTFS"),
    ("GTFS", "getGTFS"),
]


def group_token(group):
    """Tokens are scoped PER API GROUP. A TrainData token returns
    {"errorMessage":"Invalid token."} against GTFSRT endpoints, and minting
    through njt_logger would also clobber the logger's cached token. So mint
    here, against this group, and keep it out of the shared cache."""
    import urllib.request
    body, ctype = njt._multipart({
        "username": njt.os.environ.get("NJT_USERNAME", ""),
        "password": njt.os.environ.get("NJT_PASSWORD", ""),
    })
    req = urllib.request.Request(
        njt.HOST + "/api/" + group + "/getToken",
        data=body, headers={"Content-Type": ctype, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        return payload.get("UserToken")
    except Exception:
        return None


def download(_unused=None):
    import urllib.request
    for group, method in CANDIDATES:
        token = group_token(group)
        if not token:
            print("  %-34s no token for this group" % (group + "/" + method))
            continue
        url = njt.HOST + "/api/" + group + "/" + method
        body, ctype = njt._multipart({"token": token})
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": ctype, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                blob = resp.read()
        except Exception as e:
            print("  %-34s %s" % (url.split("/api/")[1], type(e).__name__))
            continue
        if blob[:2] == b"PK":
            with open(ZIP_PATH, "wb") as fh:
                fh.write(blob)
            print("  downloaded %s (%.1f MB) from %s"
                  % (ZIP_PATH, len(blob) / 1e6, url))
            return ZIP_PATH
        print("  %-34s not a zip (%d bytes): %s"
              % (url.split("/api/")[1], len(blob), blob[:90]))
    return None


def read_table(zf, name):
    try:
        raw = zf.read(name).decode("utf-8-sig", "replace")
    except KeyError:
        return None
    return list(csv.DictReader(io.StringIO(raw)))


def audit(path):
    zf = zipfile.ZipFile(path)
    print("\nfiles: " + ", ".join(sorted(zf.namelist())))

    stops = read_table(zf, "stops.txt") or []
    trips = read_table(zf, "trips.txt") or []
    times = read_table(zf, "stop_times.txt") or []
    print("\nstops %d | trips %d | stop_times %d" % (len(stops), len(trips), len(times)))

    # --- 1. Does anything resemble a platform/track? -------------------------
    print("\n" + "=" * 66)
    print("1. IS THERE ANY TRACK/PLATFORM DATA?")
    print("=" * 66)
    cols = set()
    for tbl in (stops, trips, times):
        if tbl:
            cols |= set(tbl[0].keys())
    track_cols = [c for c in sorted(cols)
                  if any(w in c.lower() for w in ("track", "platform", "parent"))]
    print("columns mentioning track/platform/parent: %s"
          % (", ".join(track_cols) if track_cols else "NONE"))

    nyp = [s for s in stops
           if all(h in (s.get("stop_name", "")).lower() for h in NYP_NAME_HINTS)]
    print("\nNY Penn stop entries: %d" % len(nyp))
    for s in nyp[:6]:
        print("   id=%-8s name=%-28s platform_code=%r parent=%r"
              % (s.get("stop_id"), s.get("stop_name", "")[:28],
                 s.get("platform_code", ""), s.get("parent_station", "")))
    if len(nyp) <= 1:
        print("   -> single station stop, no platform-level detail. As expected:")
        print("      GTFS cannot supply track labels. Keep using RailData.")

    # --- 2. Is block_id usable for turn-linking? -----------------------------
    print("\n" + "=" * 66)
    print("2. DOES block_id LINK AN ARRIVAL TO ITS DEPARTURE?")
    print("=" * 66)
    if trips and "block_id" in trips[0]:
        blocks = Counter(t.get("block_id") for t in trips if t.get("block_id"))
        multi = sum(1 for v in blocks.values() if v > 1)
        print("trips %d | distinct block_id %d | blocks with >1 trip %d"
              % (len(trips), len(blocks), multi))
        sample = [t for t in trips if t.get("block_id")][:5]
        for t in sample:
            print("   block_id=%-10s trip_id=%-14s short_name=%s"
                  % (t.get("block_id"), t.get("trip_id", "")[:14],
                     t.get("trip_short_name", "")))
        if multi < len(blocks) * 0.1:
            print("\n   -> block_id is ~1:1 with trips, so it does NOT chain an")
            print("      arrival to its outbound turn. Derive turns from timing")
            print("      at NY Penn instead (see section 3).")
    else:
        print("no block_id column at all.")

    # --- 3. Derive movement type from timing at NY Penn ----------------------
    print("\n" + "=" * 66)
    print("3. MOVEMENT TYPE (the part GTFS is actually good for)")
    print("=" * 66)
    nyp_ids = {s["stop_id"] for s in nyp}
    if not nyp_ids or not times:
        print("cannot derive without stop_times + a NY Penn stop id")
        zf.close()
        return

    by_trip = defaultdict(list)
    for st in times:
        by_trip[st["trip_id"]].append(st)
    arrive_at_nyp, depart_from_nyp = [], []
    for trip_id, sts in by_trip.items():
        try:
            sts.sort(key=lambda x: int(x.get("stop_sequence", 0)))
        except ValueError:
            continue
        if not sts:
            continue
        if sts[-1]["stop_id"] in nyp_ids:
            arrive_at_nyp.append((trip_id, sts[-1].get("arrival_time")))
        if sts[0]["stop_id"] in nyp_ids:
            depart_from_nyp.append((trip_id, sts[0].get("departure_time")))
    print("trips TERMINATING at NY Penn : %d" % len(arrive_at_nyp))
    print("trips ORIGINATING at NY Penn : %d" % len(depart_from_nyp))
    print("\nA departure whose scheduled time falls 18-60 min after some arrival")
    print("is a probable TURN (Tri-Venture scheduled turn dwell is 22 min, min 18).")
    print("Those are the ones whose track is inherited from the inbound.")
    print("Everything else is load-and-go from the yard -- and per FRA those")
    print("dominate the PM peak, which is exactly where prediction is hardest.")
    zf.close()


def main():
    if len(sys.argv) > 1:
        audit(sys.argv[1])
        return
    if os.path.exists(ZIP_PATH):
        print("using existing " + ZIP_PATH)
        audit(ZIP_PATH)
        return
    njt.load_env()
    print("trying GTFS endpoints...")
    path = download(njt.get_token())
    if path:
        audit(path)
    else:
        print("\nCould not fetch automatically. Download the GTFS-RAIL zip from")
        print("the portal dashboard, then run:  python gtfs_ingest.py <path.zip>")


if __name__ == "__main__":
    main()
