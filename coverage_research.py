"""
Where does the uncovered third of departures go, and what would recover it?

    python coverage_research.py

Everything is scored the honest way: codebooks are built on the first 70% of
service days and every number below is measured on the remaining days only.
Read-only against track_history.db.

Experiments
  A. Bucket the misses: for held-out departures we did NOT predict, what was
     available before the board posted? Nothing, an ambiguous circuit, a rare
     circuit, or a usable circuit that simply arrived too late.
  B. Circuit BIGRAMS: a single approach circuit may map to several tracks, but
     the sequence (previous circuit, current circuit) may not.
  C. Threshold sweep: how much coverage does a looser codebook buy, at what
     accuracy cost.
  D. Platform-level tier: when a circuit is ambiguous between tracks that share
     an island (7 and 8), it is still a perfectly good "which staircase" answer.
  E. Elimination as a tie-breaker: ambiguous circuit -> candidate set -> remove
     tracks currently occupied by posted trains -> unique?
  F. Timing on the weak tracks (5-10): does the usable signal arrive before or
     after the board posts?
  G. Direction-conditioned circuits: does (circuit, direction) purify anything?
  H. Two fields never used: CAPACITY[] and the GPS values on the weak tracks.
"""

import io
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB = "track_history.db"
CKT_MIN_N, CKT_MIN_PURITY = 20, 0.95
WEAK = {"5", "6", "7", "8", "9", "10"}


def platform(t):
    try:
        return math.ceil(int(t) / 2)
    except (TypeError, ValueError):
        return None


def ts(s):
    return datetime.fromisoformat(s)


def rule(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


conn = sqlite3.connect(DB)
conn.execute("PRAGMA query_only=1")

# ------------------------------------------------------------------ load
truth, posted = {}, {}
for tid, sd, trk, pa in conn.execute(
        "SELECT train_id, service_date, track, posted_at FROM track_postings WHERE track != ''"):
    if str(trk).strip().isdigit():
        truth[(tid, sd)] = str(trk).strip()
        posted[(tid, sd)] = pa

days = sorted({sd for _, sd in truth})
cut = days[int(len(days) * 0.7)]
trn = {k for k in truth if k[1] < cut}
tst = {k for k in truth if k[1] >= cut}
print("service days %d | codebook days < %s (%d train-days) | held-out %d train-days"
      % (len(days), cut, len(trn), len(tst)))

# circuit sequences per train-day (consecutive duplicates collapsed)
seq = defaultdict(list)
for tid, sd, ckt, direction, pa in conn.execute(
        "SELECT v.train_id, v.service_date, v.ics_track_ckt, v.direction, v.polled_at "
        "FROM vehicle_positions v JOIN track_postings p "
        "  ON p.train_id = v.train_id AND p.service_date = v.service_date "
        "WHERE v.ics_track_ckt IS NOT NULL AND v.ics_track_ckt != '' AND p.track != '' "
        "ORDER BY v.polled_at"):
    k = (tid, sd)
    if not seq[k] or seq[k][-1][0] != ckt:
        seq[k].append((ckt, direction, pa))
print("vehicle circuit sequences for %d train-days" % len(seq))

# board rows with GPS, and board occupancy (posted tracks) per poll
gps_rows = defaultdict(list)
occupancy = defaultdict(set)
for tid, sd, la, lo, trk, pa in conn.execute(
        "SELECT train_id, service_date, gps_lat, gps_lon, track, polled_at FROM observations "
        "WHERE gps_lat IS NOT NULL OR (track IS NOT NULL AND track != '') ORDER BY polled_at"):
    t = (trk or "").strip()
    if t.isdigit():
        occupancy[pa].add(t)
    if la:
        gps_rows[(tid, sd)].append(((la, lo), t, pa))
occ_times = sorted(occupancy)
print("board polls with occupancy info: %d" % len(occ_times))


def occupied_at(when):
    """posted tracks on the board at the poll closest before `when`."""
    lo, hi = 0, len(occ_times)
    while lo < hi:
        mid = (lo + hi) // 2
        if occ_times[mid] <= when:
            lo = mid + 1
        else:
            hi = mid
    return occupancy[occ_times[lo - 1]] if lo else set()


# ------------------------------------------------------------ codebooks
uni_votes, bi_votes, dir_votes = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
for k in trn:
    t = truth[k]
    prev = None
    for ckt, direction, _ in seq.get(k, []):
        uni_votes[ckt][t] += 1
        dir_votes[(ckt, direction)][t] += 1
        if prev:
            bi_votes[(prev, ckt)][t] += 1
        prev = ckt
gvotes = defaultdict(Counter)
for k in trn:
    for g, t, _ in gps_rows.get(k, []):
        if t:
            gvotes[g][t] += 1


def sift(votes, min_n, min_p):
    book = {}
    for key, c in votes.items():
        n = sum(c.values())
        t, m = c.most_common(1)[0]
        if n >= min_n and m / n >= min_p:
            book[key] = t
    return book


uni = sift(uni_votes, CKT_MIN_N, CKT_MIN_PURITY)
gbook = sift(gvotes, 3, 0.98)


def classify(ckt):
    c = uni_votes.get(ckt)
    if not c:
        return "unseen"
    n = sum(c.values())
    if ckt in uni:
        return "pure"
    return "rare" if n < CKT_MIN_N else "ambiguous"


def first_pred(k, book_uni, book_gps, book_bi=None):
    """earliest prediction before posting: (track, when, source) or None"""
    best = None
    prev = None
    for ckt, _, pa in seq.get(k, []):
        if pa >= posted[k]:
            break
        if ckt in book_uni:
            best = (book_uni[ckt], pa, "circuit")
            break
        if book_bi and prev and (prev, ckt) in book_bi:
            best = (book_bi[(prev, ckt)], pa, "bigram")
            break
        prev = ckt
    for g, t, pa in gps_rows.get(k, []):
        if t or pa >= posted[k]:
            continue
        if g in book_gps and (best is None or pa < best[1]):
            best = (book_gps[g], pa, "berth")
            break
    return best


base = {k: first_pred(k, uni, gbook) for k in tst}
covered = {k for k, v in base.items() if v}
hit = sum(1 for k in covered if base[k][0] == truth[k])
print("\nBASELINE (current engine, held-out): coverage %.1f%%  accuracy %.1f%%"
      % (100.0 * len(covered) / len(tst), 100.0 * hit / max(len(covered), 1)))
missed = [k for k in tst if k not in covered]

# ------------------------------------------------------------------ A
rule("A. WHERE DO THE %d UNCOVERED DEPARTURES GO?" % len(missed))
buckets = Counter()
late_lead = []
for k in missed:
    pre = [c for c in seq.get(k, []) if c[2] < posted[k]]
    post = [c for c in seq.get(k, []) if c[2] >= posted[k]]
    kinds = {classify(c[0]) for c in pre}
    if not seq.get(k):
        buckets["no vehicle rows at all (train never in vehicle feed)"] += 1
    elif not pre:
        buckets["vehicle rows only AFTER the board posted"] += 1
    elif "ambiguous" in kinds:
        buckets["had an AMBIGUOUS circuit before posting (thrown away)"] += 1
    elif "rare" in kinds:
        buckets["had only RARE circuits before posting (n < 20, will grow)"] += 1
    elif "unseen" in kinds:
        buckets["had only circuits never seen in training"] += 1
    else:
        buckets["other"] += 1
    if any(c[0] in uni for c in post):
        first_pure = next(c for c in post if c[0] in uni)
        late_lead.append((ts(first_pure[2]) - ts(posted[k])).total_seconds() / 60)
for b, n in buckets.most_common():
    print("  %3d  (%4.1f%%)  %s" % (n, 100.0 * n / len(missed), b))
if late_lead:
    late_lead.sort()
    print("\n  of those, %d got a usable circuit AFTER posting, median %.1f min after"
          % (len(late_lead), late_lead[len(late_lead) // 2]))

# ------------------------------------------------------------------ B
rule("B. CIRCUIT BIGRAMS (previous circuit, current circuit)")
bi = sift(bi_votes, CKT_MIN_N, CKT_MIN_PURITY)
bi_new = {p for p in bi if p[1] not in uni}          # pairs whose current circuit is not already pure
print("bigram codebook: %d pairs, of which %d resolve a circuit that is NOT pure on its own"
      % (len(bi), len(bi_new)))
withbi = {k: first_pred(k, uni, gbook, bi) for k in tst}
cov_b = {k for k, v in withbi.items() if v}
hit_b = sum(1 for k in cov_b if withbi[k][0] == truth[k])
gain = cov_b - covered
gain_hit = sum(1 for k in gain if withbi[k][0] == truth[k])
print("held-out with bigrams: coverage %.1f%% (+%.1f pts)  accuracy %.1f%%"
      % (100.0 * len(cov_b) / len(tst), 100.0 * len(gain) / len(tst), 100.0 * hit_b / max(len(cov_b), 1)))
print("  the newly covered %d: %d right (%.0f%%)" % (len(gain), gain_hit, 100.0 * gain_hit / max(len(gain), 1)))
if gain:
    print("  new coverage by track: %s" % dict(sorted(Counter(truth[k] for k in gain).items(), key=lambda kv: int(kv[0]))))

# ------------------------------------------------------------------ C
rule("C. THRESHOLD SWEEP (single circuits + berth, held-out)")
print("  %-6s %-8s %9s %9s %9s" % ("min_n", "purity", "coverage", "accuracy", "cov*acc"))
for mn in (20, 10, 5):
    for mp in (0.95, 0.90, 0.85):
        b = sift(uni_votes, mn, mp)
        preds = {k: first_pred(k, b, gbook) for k in tst}
        c = {k for k, v in preds.items() if v}
        h = sum(1 for k in c if preds[k][0] == truth[k])
        cov, acc = 100.0 * len(c) / len(tst), 100.0 * h / max(len(c), 1)
        print("  %-6d %-8.2f %8.1f%% %8.1f%% %8.1f%%%s" % (mn, mp, cov, acc, cov * acc / 100,
              "   <- current" if (mn, mp) == (20, 0.95) else ""))

# ------------------------------------------------------------------ D
rule("D. PLATFORM-LEVEL TIER FOR AMBIGUOUS CIRCUITS")
plat_book = {}
for ckt, c in uni_votes.items():
    n = sum(c.values())
    if ckt in uni or n < CKT_MIN_N:
        continue
    pc = Counter()
    for t, m in c.items():
        pc[platform(t)] += m
    p, m = pc.most_common(1)[0]
    if m / n >= 0.95:
        plat_book[ckt] = p
print("ambiguous circuits that are nonetheless PURE at platform level: %d" % len(plat_book))
plat_hits = plat_n = 0
plat_gain_tracks = Counter()
for k in missed:
    for ckt, _, pa in seq.get(k, []):
        if pa >= posted[k]:
            break
        if ckt in plat_book:
            plat_n += 1
            plat_hits += (plat_book[ckt] == platform(truth[k]))
            plat_gain_tracks[truth[k]] += 1
            break
print("held-out: %d of the %d uncovered departures get a platform (+%.1f pts of board), %.1f%% correct"
      % (plat_n, len(missed), 100.0 * plat_n / len(tst), 100.0 * plat_hits / max(plat_n, 1)))
if plat_gain_tracks:
    print("  by track: %s" % dict(sorted(plat_gain_tracks.items(), key=lambda kv: int(kv[0]))))

# ------------------------------------------------------------------ E
rule("E. ELIMINATION AS A TIE-BREAKER ON AMBIGUOUS CIRCUITS")
elim_n = elim_hit = elim_set = 0
for k in missed:
    for ckt, _, pa in seq.get(k, []):
        if pa >= posted[k]:
            break
        c = uni_votes.get(ckt)
        if not c or ckt in uni or sum(c.values()) < CKT_MIN_N:
            continue
        n = sum(c.values())
        cands = {t for t, m in c.items() if m / n >= 0.10}
        elim_set += 1
        left = cands - occupied_at(pa)
        if len(left) == 1:
            elim_n += 1
            elim_hit += (next(iter(left)) == truth[k])
        break
print("uncovered departures with an ambiguous circuit (candidate set) : %d" % elim_set)
print("  collapsed to ONE track by removing occupied tracks         : %d (%.1f%% of them), %.0f%% correct"
      % (elim_n, 100.0 * elim_n / max(elim_set, 1), 100.0 * elim_hit / max(elim_n, 1)))

# ------------------------------------------------------------------ F
rule("F. TIMING ON THE WEAK TRACKS 5-10")
for t in sorted(WEAK, key=int):
    ks = [k for k in tst if truth[k] == t]
    if not ks:
        continue
    any_lead, pure_lead, none = [], [], 0
    for k in ks:
        s = seq.get(k, [])
        if not s:
            none += 1
            continue
        any_lead.append((ts(posted[k]) - ts(s[0][2])).total_seconds() / 60)
        pure = [c for c in s if c[0] in uni]
        if pure:
            pure_lead.append((ts(posted[k]) - ts(pure[0][2])).total_seconds() / 60)
    def med(x):
        return ("%+.1f" % sorted(x)[len(x) // 2]) if x else "  --"
    print("  track %-3s n=%-3d  no vehicle rows %2d | first ANY circuit %s min before posting | first PURE circuit %s min before (n=%d)"
          % (t, len(ks), none, med(any_lead), med(pure_lead), len(pure_lead)))
print("  (negative = the signal arrived after the board had already posted)")

# ------------------------------------------------------------------ G
rule("G. DIRECTION-CONDITIONED CIRCUITS")
dbook = sift(dir_votes, CKT_MIN_N, CKT_MIN_PURITY)
newly = {c for c, d in dbook if c not in uni}
print("circuits that are ambiguous alone but PURE once split by direction: %d" % len(newly))
if newly:
    for c in list(newly)[:8]:
        print("   %-16s %s" % (c, {d: dict(dir_votes[(c, d)].most_common(2)) for (cc, d) in dbook if cc == c}))

# ------------------------------------------------------------------ H
rule("H. UNUSED FIELDS")
caps = Counter()
for (raw,) in conn.execute("SELECT raw FROM observations WHERE track != '' LIMIT 40000"):
    try:
        v = json.loads(raw).get("CAPACITY")
    except Exception:
        continue
    caps[json.dumps(v)[:60] if v else "empty"] += 1
print("CAPACITY[] values seen (top 5): %s" % caps.most_common(5))
weak_gps = Counter()
for k, rows in gps_rows.items():
    if truth.get(k) in WEAK:
        for g, t, _ in rows:
            weak_gps[(g, "posted" if t else "pre-posting")] += 1
print("GPS coordinates seen on trains that used tracks 5-10 (top 6):")
for (g, phase), n in weak_gps.most_common(6):
    print("   %s,%s  %-12s %d" % (g[0], g[1], phase, n))
print("\n" + "=" * 72)
