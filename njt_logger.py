"""
NJ TRANSIT departure-board logger.

Manufactures the historical track dataset that does not otherwise exist: NJT
exposes what is on the board *now*, never what was on the board last Tuesday.

Written against NJTRANSIT_RailData_API_V2 (the portal's Rail Real-time Data
Web API doc). Contract notes that cost real debugging time:

  * Every call is HTTP POST with a multipart/form-data body. Urlencoded bodies
    are rejected. Endpoints live under /api/TrainData/.
  * Auth is a token from getToken (form: username, password) -> {"UserToken":...}.
    Token life is measured in hours and configured per-app. Minting is rate
    limited far more tightly than data calls, so we cache the token on disk and
    reuse it across restarts.
  * Errors come back as HTTP 200 with an {"errorMessage": ...} body, NOT as a
    401/403. Quota exhaustion looks like:
        {"errorMessage": "Daily usage limit:10. Your current daily usage: 11"}
    The TEST environment's limit is tiny (10/day in the doc's example), so point
    NJT_HOST at production once approved or you will burn the quota in one poll.
  * getTrainSchedule(token, station) is the departure board. It returns a single
    object -- STATION_2CHAR / STATIONNAME / STATIONMSGS / ITEMS[] -- not a list.
    (getStationSchedule is a different, schedule-not-realtime endpoint whose
    TRACK field holds a line name at some stations. Do not confuse them.)
  * The board includes Amtrak (TRAIN_ID prefixed "A"), SEPTA ("S") and
    non-revenue equipment moves ("X"). We log all of them on purpose: Amtrak
    occupancy constrains what tracks are left for NJT, and the "X" moves are
    literally the yard/turn equipment we care about.
  * getVehicleData(token) returns ICS_TRACK_CKT -- the last identified track
    circuit ID per train ("HO-7061TK"). That is signalling-level position data
    and is the most promising early-warning signal in the whole API: an inbound
    train's circuit sequence through the tunnels and interlocking reveals the
    route being lined for it before the board posts a track.

Stdlib only. No pip install required.
"""

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HOST = os.environ.get("NJT_HOST", "https://raildata.njtransit.com")
API = HOST + "/api/TrainData"

STATION = os.environ.get("NJT_STATION", "NY")   # NY = New York Penn Station
POLL_SECONDS = int(os.environ.get("NJT_POLL_SECONDS", "30"))
DB_PATH = os.environ.get("NJT_DB", "track_history.db")

# getVehicleData is a separate call against the same daily quota. Poll it less
# often than the board unless you are actively mining the track-circuit signal.
VEHICLE_EVERY_N = int(os.environ.get("NJT_VEHICLE_EVERY_N", "2"))

TOKEN_CACHE = os.environ.get("NJT_TOKEN_CACHE", ".njt_token.json")
# NJT allows only ~10 token mints per day ("Daily usage limit:10"), so a cached
# token is reused until the API actually rejects it -- every caller re-mints
# once on AuthError -- rather than refreshed on a timer. The TTL is only a
# backstop against a token that is somehow never rejected.
TOKEN_TTL_HOURS = float(os.environ.get("NJT_TOKEN_TTL_HOURS", "168"))

# Service day rolls at 3am so late-night trains group with the correct date.
SERVICE_DAY_CUTOFF_HOUR = 3

TRACK_PLACEHOLDERS = {"", "-", "TBD", "N/A", "NONE"}


class ApiError(RuntimeError):
    """The API returned 200 with an errorMessage body."""


class AuthError(ApiError):
    """Token invalid/expired -- worth re-minting once."""


# ---------------------------------------------------------------- env loading

def load_env(path=".env"):
    """Minimal .env parser -- avoids a python-dotenv dependency."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip("\"").strip("'"))


# ------------------------------------------------------------------- http/api

def _multipart(fields):
    """The API requires multipart/form-data and rejects urlencoded bodies."""
    boundary = "----njtlogger" + str(int(time.time() * 1000))
    parts = []
    for key, value in fields.items():
        parts.append("--" + boundary)
        parts.append('Content-Disposition: form-data; name="' + key + '"')
        parts.append("")
        parts.append(str(value))
    parts.append("--" + boundary + "--")
    parts.append("")
    return "\r\n".join(parts).encode("utf-8"), "multipart/form-data; boundary=" + boundary


def api_post(method, fields):
    body, content_type = _multipart(fields)
    req = urllib.request.Request(
        API + "/" + method,
        data=body,
        headers={"Content-Type": content_type, "Accept": "text/plain"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # The API also returns errorMessage bodies on 4xx/5xx, and urllib
        # discards the body unless we read it off the exception. Without this
        # a bad username surfaces as a bare "HTTP Error 500" instead of
        # {"errorMessage": "Missing user account."}.
        raw = e.read().decode("utf-8", "replace")
        if not raw.strip():
            raise


    if not raw.strip() or raw.strip().lower() == "null":
        # Documented behaviour when the token is empty or the wrong length.
        raise AuthError(method + " returned null (empty/malformed token)")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError("Non-JSON from " + method + ": " + raw[:300])

    # Errors arrive as HTTP 200 with an errorMessage body.
    if isinstance(payload, dict):
        err = payload.get("errorMessage") or payload.get("ErrorMessage")
        if err:
            if "usage limit" in str(err).lower():
                raise ApiError("QUOTA: " + str(err))
            raise AuthError(str(err))
    return payload


# --------------------------------------------------------------------- tokens

def get_usage(token, username=None):
    """Per-method request counts for recent days, from NJ Transit's own
    counters. Lives under /api/Usage/ rather than /api/TrainData/, so it cannot
    go through api_post(). The getToken row is the one worth watching: its
    limit is ~10 a day, and every container start spends one."""
    user = username or os.environ.get("NJT_USERNAME", "")
    body, content_type = _multipart({"username": user, "token": token})
    req = urllib.request.Request(
        HOST + "/api/Usage/getUsage", data=body,
        headers={"Content-Type": content_type, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def mint_token():
    user, pw = os.environ.get("NJT_USERNAME"), os.environ.get("NJT_PASSWORD")
    if not user or not pw:
        sys.exit("Missing NJT_USERNAME / NJT_PASSWORD -- see .env.example")
    payload = api_post("getToken", {"username": user, "password": pw})
    if isinstance(payload, dict):
        for key in ("UserToken", "userToken", "usertoken", "token", "Token"):
            if payload.get(key):
                return payload[key]
    raise ApiError("No token in getToken response: " + str(payload)[:300])


def load_cached_token():
    """Reuse across restarts -- minting is the tightly-limited call."""
    if not os.path.exists(TOKEN_CACHE):
        return None
    try:
        with open(TOKEN_CACHE, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(blob["minted_at"])).total_seconds() / 3600
        if age_h < TOKEN_TTL_HOURS and blob.get("token"):
            print("reusing cached token (age " + str(round(age_h, 1)) + "h)")
            return blob["token"]
    except Exception:
        pass
    return None


def save_cached_token(token):
    tmp = TOKEN_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"token": token,
                   "minted_at": datetime.now(timezone.utc).isoformat()}, fh)
    os.replace(tmp, TOKEN_CACHE)


def get_token(force=False):
    if not force:
        cached = load_cached_token()
        if cached:
            return cached
    token = mint_token()
    save_cached_token(token)
    print("minted a fresh token")
    return token


# ------------------------------------------------------------------ db schema

DDL = """
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    polled_at     TEXT NOT NULL,     -- UTC ISO8601, one value per poll cycle
    station       TEXT NOT NULL,
    train_id      TEXT,
    service_date  TEXT,
    line          TEXT,
    line_abbrev   TEXT,
    destination   TEXT,
    sched_dep     TEXT,
    track         TEXT,              -- empty until the board posts it
    status        TEXT,
    sec_late      INTEGER,
    last_modified TEXT,
    -- Live equipment position, present on every board row (undocumented in the
    -- V2 PDF, which shows STOPS as null and omits GPS entirely). This is the
    -- berth signal: when GPS sits at Penn (~40.7498, -73.9918) the consist is
    -- already in the station, which is a far stronger track cue than any prior.
    gps_lat       TEXT,
    gps_lon       TEXT,
    gps_time      TEXT,
    at_penn       INTEGER,           -- 1 if GPS is within ~400m of NY Penn
    station_position TEXT,
    connecting_train_id TEXT,
    n_stops       INTEGER,           -- STOPS[] is populated live, not null
    raw           TEXT NOT NULL      -- verbatim JSON for this departure row
);
CREATE INDEX IF NOT EXISTS idx_obs_train_day ON observations (train_id, service_date);
CREATE INDEX IF NOT EXISTS idx_obs_polled    ON observations (polled_at);

-- Derived: the moment a track first appeared for a given train-day.
-- Rebuildable from observations; kept live for convenience.
CREATE TABLE IF NOT EXISTS track_postings (
    train_id         TEXT NOT NULL,
    service_date     TEXT NOT NULL,
    station          TEXT NOT NULL,
    line             TEXT,
    destination      TEXT,
    sched_dep        TEXT,
    track            TEXT NOT NULL,
    posted_at        TEXT NOT NULL,  -- when we first saw a track
    lead_seconds     INTEGER,        -- sched_dep - posted_at, the money column
    sec_late_at_post INTEGER,
    PRIMARY KEY (train_id, service_date, station)
);

-- Signalling-level positions. ICS_TRACK_CKT is the leading indicator worth mining.
CREATE TABLE IF NOT EXISTS vehicle_positions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    polled_at      TEXT NOT NULL,
    service_date   TEXT,
    train_id       TEXT,
    train_line     TEXT,
    direction      TEXT,
    ics_track_ckt  TEXT,
    next_stop      TEXT,
    sec_late       INTEGER,
    latitude       TEXT,
    longitude      TEXT,
    last_modified  TEXT,
    raw            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_veh_train ON vehicle_positions (train_id, service_date);
CREATE INDEX IF NOT EXISTS idx_veh_ckt   ON vehicle_positions (ics_track_ckt);
"""


# Columns added after logging had already started. CREATE TABLE IF NOT EXISTS
# will not add them to an existing DB, so ALTER explicitly and then backfill
# from the raw JSON -- which is exactly why every row is stored verbatim.
MIGRATIONS = [
    ("observations", "gps_lat", "TEXT"),
    ("observations", "gps_lon", "TEXT"),
    ("observations", "gps_time", "TEXT"),
    ("observations", "at_penn", "INTEGER"),
    ("observations", "station_position", "TEXT"),
    ("observations", "connecting_train_id", "TEXT"),
    ("observations", "n_stops", "INTEGER"),
]

NYP_LAT, NYP_LON = 40.7498, -73.9918


def at_penn(lat, lon):
    """Rough 400m box around NY Penn. Good enough to tell 'berthed' from
    'still in New Jersey'; not trying to be a geofence."""
    try:
        return 1 if (abs(float(lat) - NYP_LAT) < 0.004
                     and abs(float(lon) - NYP_LON) < 0.005) else 0
    except (TypeError, ValueError):
        return None


def migrate(conn):
    for table, col, coltype in MIGRATIONS:
        try:
            conn.execute("ALTER TABLE " + table + " ADD COLUMN " + col + " " + coltype)
        except sqlite3.OperationalError:
            pass  # already present
    conn.commit()


def backfill(conn):
    """Recompute the newer derived columns for rows logged before they existed."""
    rows = conn.execute(
        "SELECT id, raw FROM observations WHERE gps_lat IS NULL AND raw IS NOT NULL"
    ).fetchall()
    if not rows:
        return 0
    for row_id, raw in rows:
        try:
            item = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        lat, lon = pick(item, "GPSLATITUDE"), pick(item, "GPSLONGITUDE")
        stops = item.get("STOPS")
        conn.execute(
            "UPDATE observations SET gps_lat=?, gps_lon=?, gps_time=?, at_penn=?, "
            "station_position=?, connecting_train_id=?, n_stops=? WHERE id=?",
            (lat, lon, pick(item, "GPSTIME"), at_penn(lat, lon),
             pick(item, "STATION_POSITION"), pick(item, "CONNECTING_TRAIN_ID"),
             len(stops) if isinstance(stops, list) else None, row_id),
        )
    conn.commit()
    return len(rows)


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    migrate(conn)
    filled = backfill(conn)
    if filled:
        print("backfilled " + str(filled) + " earlier rows from raw JSON")
    return conn


# ------------------------------------------------------------ field extraction

def pick(row, *names):
    """Case-insensitive, alias-tolerant field lookup."""
    lowered = {k.lower(): v for k, v in row.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v not in (None, ""):
            return v
    return None


def as_int(value):
    try:
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def service_date_for(dt):
    if dt.hour < SERVICE_DAY_CUTOFF_HOUR:
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def parse_sched(value):
    """Documented format is '30-May-2024 11:56:00 AM'."""
    if not value:
        return None
    for fmt in ("%d-%b-%Y %I:%M:%S %p", "%d-%b-%Y %H:%M:%S",
                "%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------- recording

def record_board(conn, items, polled_at):
    now_local = datetime.now()
    svc_date = service_date_for(now_local)
    new_postings = []

    for row in items:
        if not isinstance(row, dict):
            continue
        train_id = pick(row, "TRAIN_ID")
        track = pick(row, "TRACK")
        sched = pick(row, "SCHED_DEP_DATE")
        line = pick(row, "LINE")
        line_ab = pick(row, "LINEABBREVIATION", "LINECODE")
        dest = pick(row, "DESTINATION")
        status = pick(row, "STATUS")
        late = as_int(pick(row, "SEC_LATE"))
        modified = pick(row, "LAST_MODIFIED")

        lat, lon = pick(row, "GPSLATITUDE"), pick(row, "GPSLONGITUDE")
        stops = row.get("STOPS")
        conn.execute(
            "INSERT INTO observations "
            "(polled_at, station, train_id, service_date, line, line_abbrev, "
            " destination, sched_dep, track, status, sec_late, last_modified, "
            " gps_lat, gps_lon, gps_time, at_penn, station_position, "
            " connecting_train_id, n_stops, raw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (polled_at, STATION, train_id, svc_date, line, line_ab, dest, sched,
             track, status, late, modified,
             lat, lon, pick(row, "GPSTIME"), at_penn(lat, lon),
             pick(row, "STATION_POSITION"), pick(row, "CONNECTING_TRAIN_ID"),
             len(stops) if isinstance(stops, list) else None,
             json.dumps(row, separators=(",", ":"))),
        )

        clean = str(track).strip().upper() if track is not None else ""
        if train_id and clean and clean not in TRACK_PLACEHOLDERS:
            sched_dt = parse_sched(sched)
            lead = int((sched_dt - now_local).total_seconds()) if sched_dt else None
            cur = conn.execute(
                "INSERT OR IGNORE INTO track_postings "
                "(train_id, service_date, station, line, destination, sched_dep, "
                " track, posted_at, lead_seconds, sec_late_at_post) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (train_id, svc_date, STATION, line, dest, sched,
                 str(track).strip(), polled_at, lead, late),
            )
            if cur.rowcount:
                mins = (str(lead // 60) + "m") if lead is not None else "?"
                new_postings.append(str(train_id) + "->" + str(track).strip()
                                    + " (T-" + mins + ")")

    conn.commit()
    return new_postings


def record_vehicles(conn, trains, polled_at):
    svc_date = service_date_for(datetime.now())
    for row in trains:
        if not isinstance(row, dict):
            continue
        conn.execute(
            "INSERT INTO vehicle_positions "
            "(polled_at, service_date, train_id, train_line, direction, "
            " ics_track_ckt, next_stop, sec_late, latitude, longitude, "
            " last_modified, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (polled_at, svc_date, pick(row, "ID", "TRAIN_ID"),
             pick(row, "TRAIN_LINE"), pick(row, "DIRECTION"),
             pick(row, "ICS_TRACK_CKT"), pick(row, "NEXT_STOP"),
             as_int(pick(row, "SEC_LATE")), pick(row, "LATITUDE"),
             pick(row, "LONGITUDE"), pick(row, "LAST_MODIFIED"),
             json.dumps(row, separators=(",", ":"))),
        )
    conn.commit()


def board_items(payload):
    """getTrainSchedule returns one station object; be tolerant of a list."""
    if isinstance(payload, dict):
        return payload.get("ITEMS") or payload.get("Items") or []
    if isinstance(payload, list):
        items = []
        for entry in payload:
            if isinstance(entry, dict):
                items.extend(entry.get("ITEMS") or entry.get("Items") or [])
        return items
    return []


# ------------------------------------------------------------------- main loop

def main():
    load_env()
    conn = connect()
    token = get_token()
    print("[" + datetime.now().strftime("%H:%M:%S") + "] logging " + STATION
          + " every " + str(POLL_SECONDS) + "s -> " + DB_PATH
          + "  (host " + HOST + ")")

    cycle = 0
    errors = 0
    while True:
        polled_at = datetime.now(timezone.utc).isoformat()
        try:
            items = board_items(api_post("getTrainSchedule",
                                         {"token": token, "station": STATION}))
            postings = record_board(conn, items, polled_at)

            veh = 0
            if VEHICLE_EVERY_N and cycle % VEHICLE_EVERY_N == 0:
                trains = api_post("getVehicleData", {"token": token})
                if isinstance(trains, dict):
                    trains = trains.get("TRAINS") or []
                if isinstance(trains, list):
                    record_vehicles(conn, trains, polled_at)
                    veh = len(trains)

            total = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            note = ("  NEW: " + ", ".join(postings)) if postings else ""
            print("[" + datetime.now().strftime("%H:%M:%S") + "] "
                  + str(len(items)).rjust(3) + " trains | "
                  + str(veh).rjust(3) + " veh | "
                  + str(total).rjust(7) + " obs" + note)
            errors = 0
            cycle += 1

        except AuthError as e:
            print("auth: " + str(e) + " -- re-minting")
            try:
                token = get_token(force=True)
                errors = 0
            except Exception as auth_err:
                errors += 1
                print("re-auth failed: " + str(auth_err))
        except ApiError as e:
            errors += 1
            print(str(e))
            if str(e).startswith("QUOTA"):
                print("daily quota exhausted -- sleeping 1h")
                time.sleep(3600)
                errors = 0
                continue
        except urllib.error.HTTPError as e:
            errors += 1
            print("HTTP " + str(e.code) + ": " + str(e.reason))
        except Exception as e:  # keep the logger alive above all else
            errors += 1
            print("error: " + type(e).__name__ + ": " + str(e))

        time.sleep(POLL_SECONDS * min(2 ** errors, 20) if errors else POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
