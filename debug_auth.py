"""
Isolate the getToken 500. Tries the request several ways and prints the status
plus the RESPONSE BODY for each (urllib hides the body on HTTPError, which is
why the original failure was opaque).

Never prints the password. Run:  python debug_auth.py
"""

import json
import time
import urllib.error
import urllib.request

import njt_logger as njt

njt.load_env()
USER = njt.os.environ.get("NJT_USERNAME", "")
PW = njt.os.environ.get("NJT_PASSWORD", "")
HOST = njt.HOST

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def multipart(fields):
    boundary = "----njt" + str(int(time.time() * 1000))
    parts = []
    for k, v in fields.items():
        parts.append("--" + boundary)
        parts.append('Content-Disposition: form-data; name="' + k + '"')
        parts.append("")
        parts.append(str(v))
    parts.append("--" + boundary + "--")
    parts.append("")
    return ("\r\n".join(parts).encode(),
            "multipart/form-data; boundary=" + boundary)


def urlencoded(fields):
    import urllib.parse
    return (urllib.parse.urlencode(fields).encode(),
            "application/x-www-form-urlencoded")


def as_json(fields):
    return json.dumps(fields).encode(), "application/json"


def attempt(label, url, encoder, ua, extra_headers=None):
    body, ctype = encoder({"username": USER, "password": PW})
    headers = {"Content-Type": ctype, "Accept": "text/plain"}
    if ua:
        headers["User-Agent"] = ua
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        # The body is the whole point -- urllib swallows it by default.
        text = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:
        print("  " + label.ljust(46) + " EXC  " + type(e).__name__ + ": " + str(e)[:90])
        return None

    redacted = text.replace(PW, "***") if PW else text
    got_token = '"UserToken"' in text or '"usertoken"' in text.lower()
    flag = "  <-- TOKEN OK" if got_token else ""
    print("  " + label.ljust(46) + " " + str(status).ljust(5)
          + redacted[:120].replace("\n", " ") + flag)
    return text if got_token else None


print("host:     " + HOST)
print("username: " + USER)
print("password: " + ("set, " + str(len(PW)) + " chars" if PW else "!! EMPTY !!"))

if not USER or not PW:
    raise SystemExit("\nFill NJT_USERNAME and NJT_PASSWORD in .env first.")

print("\n--- A. encoding variants (default python UA) ---")
attempt("multipart -> /api/TrainData/getToken", HOST + "/api/TrainData/getToken", multipart, None)
attempt("urlencoded -> /api/TrainData/getToken", HOST + "/api/TrainData/getToken", urlencoded, None)
attempt("json -> /api/TrainData/getToken", HOST + "/api/TrainData/getToken", as_json, None)

print("\n--- B. same, with a browser User-Agent ---")
attempt("multipart + browser UA", HOST + "/api/TrainData/getToken", multipart, BROWSER_UA)
attempt("urlencoded + browser UA", HOST + "/api/TrainData/getToken", urlencoded, BROWSER_UA)

print("\n--- C. alternate token paths ---")
for path in ("/api/getToken", "/api/TrainData/getTokenJSON", "/api/Usage/getToken"):
    attempt("multipart -> " + path, HOST + path, multipart, BROWSER_UA)

print("\n--- D. is the host reachable at all? ---")
for probe in ("/swagger/index.html", "/api/TrainData/getStationList"):
    try:
        req = urllib.request.Request(HOST + probe, headers={"User-Agent": BROWSER_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            print("  GET " + probe.ljust(42) + " " + str(r.status)
                  + "  (" + str(len(r.read())) + " bytes)")
    except urllib.error.HTTPError as e:
        print("  GET " + probe.ljust(42) + " " + str(e.code) + "  " + e.reason)
    except Exception as e:
        print("  GET " + probe.ljust(42) + " EXC  " + type(e).__name__ + ": " + str(e)[:70])

print("""
Reading the results:
  * Any row marked TOKEN OK -> that is the working combination; tell me which.
  * All 500 but section D reachable -> server-side; most likely the account does
    not have RailData approved yet, or the portal username differs from the
    email address you log in with. Check the portal dashboard for a separate
    username field and for API status = Approved (not Pending).
  * A 200 with an errorMessage body -> credentials/approval problem, not encoding.
""")
