"""
One-shot smoke test. Spends 3 API calls to prove out the whole chain before
committing to a long logging run:

    1. mint a token           (getToken)
    2. pull the NY Penn board (getTrainSchedule)
    3. pull vehicle positions (getVehicleData)

It prints the RAW first record from each so we can verify the real field names
against what njt_logger.py expects, rather than trusting the PDF.

    python test_connection.py
"""

import json
import sys

import njt_logger as njt


def show(title, obj, limit=1400):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)
    text = json.dumps(obj, indent=2)
    print(text[:limit] + ("\n... (truncated)" if len(text) > limit else ""))


def main():
    njt.load_env()

    user = njt.os.environ.get("NJT_USERNAME")
    if not user:
        sys.exit("No NJT_USERNAME found. Fill in .env first (see .env.example).")
    print("host:     " + njt.HOST)
    print("username: " + user)
    print("station:  " + njt.STATION)
    if "test" in njt.HOST:
        print("\n!! WARNING: pointed at the TEST host, whose daily quota is tiny.")
        print("   Set NJT_HOST=https://raildata.njtransit.com in .env for real logging.\n")

    # ---- 1. token -------------------------------------------------------
    try:
        token = njt.get_token()
    except Exception as e:
        sys.exit("TOKEN FAILED: " + type(e).__name__ + ": " + e.__str__())
    print("\n[ok] token acquired: " + str(token)[:14] + "...")

    # ---- 2. departure board --------------------------------------------
    try:
        payload = njt.api_post("getTrainSchedule",
                               {"token": token, "station": njt.STATION})
    except Exception as e:
        sys.exit("BOARD FAILED: " + type(e).__name__ + ": " + e.__str__())

    items = njt.board_items(payload)
    print("[ok] board returned " + str(len(items)) + " trains")

    if items:
        show("RAW first departure -- verify these field names", items[0])

        print("\nParsed by njt_logger's extractors:")
        header = ("TRAIN".ljust(8) + "LINE".ljust(20) + "DEST".ljust(24)
                  + "TRACK".ljust(7) + "LATE".ljust(7) + "STATUS")
        print(header)
        print("-" * len(header))
        posted = 0
        for row in items[:14]:
            track = njt.pick(row, "TRACK") or ""
            if str(track).strip():
                posted += 1
            print(str(njt.pick(row, "TRAIN_ID") or "?").ljust(8)
                  + str(njt.pick(row, "LINE") or "")[:19].ljust(20)
                  + str(njt.pick(row, "DESTINATION") or "")[:23].ljust(24)
                  + (str(track).strip() or "--").ljust(7)
                  + str(njt.as_int(njt.pick(row, "SEC_LATE")) or 0).ljust(7)
                  + str(njt.pick(row, "STATUS") or ""))

        total_posted = sum(1 for r in items if str(njt.pick(r, "TRACK") or "").strip())
        print("\n" + str(total_posted) + " of " + str(len(items))
              + " trains currently have a track posted.")
        print("The ones WITHOUT a track are exactly what we are trying to predict.")

        # Are Amtrak / SEPTA / non-revenue moves visible here?
        prefixes = {}
        for row in items:
            tid = str(njt.pick(row, "TRAIN_ID") or "")
            kind = ("Amtrak" if tid[:1] == "A" else
                    "SEPTA" if tid[:1] == "S" else
                    "non-revenue" if tid[:1] == "X" else "NJT")
            prefixes[kind] = prefixes.get(kind, 0) + 1
        print("operator mix on this board: "
              + ", ".join(k + "=" + str(v) for k, v in sorted(prefixes.items())))

    # ---- 3. vehicle positions ------------------------------------------
    try:
        trains = njt.api_post("getVehicleData", {"token": token})
    except Exception as e:
        print("\n[warn] getVehicleData failed: " + type(e).__name__ + ": " + e.__str__())
        print("       You may not have that method approved. The board logger")
        print("       still works; set NJT_VEHICLE_EVERY_N=0 in .env to skip it.")
        trains = None

    if trains is not None:
        if isinstance(trains, dict):
            trains = trains.get("TRAINS") or []
        print("\n[ok] vehicle feed returned " + str(len(trains)) + " active trains")
        if trains:
            show("RAW first vehicle -- note ICS_TRACK_CKT", trains[0], limit=700)
            ckts = [njt.pick(t, "ICS_TRACK_CKT") for t in trains]
            ckts = [c for c in ckts if c]
            print("sample track circuits: " + ", ".join(str(c) for c in ckts[:12]))

    print("\n" + "=" * 68)
    print("All good. Start logging with:  python njt_logger.py")
    print("=" * 68)


if __name__ == "__main__":
    main()
