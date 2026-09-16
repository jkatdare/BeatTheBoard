"""
Show your NJ Transit API usage, per method per day, straight from NJ Transit.

    python usage.py            # last few days
    python usage.py --all      # everything they return

The one to watch is getToken: its limit is 10 per day, and every fresh
container start or new deploy mints one. The data methods are 40,000 per day.
This call itself counts as one getUsage (limit 40,000) and reuses the cached
token, so it never mints.
"""

import argparse
import json
import sys
import urllib.request
from collections import defaultdict

import njt_logger as njt


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    njt.load_env()
    token = njt.get_token()
    body, ctype = njt._multipart({"username": njt.os.environ["NJT_USERNAME"], "token": token})
    req = urllib.request.Request(njt.HOST + "/api/Usage/getUsage", data=body,
                                 headers={"Content-Type": ctype, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        rows = json.loads(resp.read().decode("utf-8", "replace"))
    if isinstance(rows, dict) and rows.get("errorMessage"):
        sys.exit("NJT says: " + rows["errorMessage"])

    by_day = defaultdict(list)
    for r in rows:
        by_day[r.get("Request_Date", "?")].append(r)
    def ymd(d):                       # "09/16/2026" -> ("2026", "09", "16")
        p = d.split("/")
        return (p[2], p[0], p[1]) if len(p) == 3 else (d,)
    days = sorted(by_day, key=ymd, reverse=True)
    if not args.all:
        days = days[:4]

    for day in days:
        print("\n" + day)
        for r in sorted(by_day[day], key=lambda r: r.get("Request_Type", "")):
            made, limit = int(r.get("Daily_Request_Made", 0)), int(r.get("Usage_Limit", 0))
            flag = "   <-- token minting, 10/day" if r.get("Request_Type") == "getToken" else ""
            warn = "  !! AT LIMIT" if limit and made >= limit else (
                   "  ! close" if limit and made >= 0.8 * limit else "")
            print("   %-24s %6d / %-6d  last %s%s%s"
                  % (r.get("Request_Type"), made, limit, r.get("Last_Req_Made", "")[:19], flag, warn))


if __name__ == "__main__":
    main()
