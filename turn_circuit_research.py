"""
Can the INBOUND train's circuit predict the OUTBOUND train's track?

    python turn_circuit_research.py

91% of uncovered departures have no circuit under their own train number
before the board posts. But a turning train is already sitting at Penn under
its inbound number, on a platform circuit we can decode -- and a turn departs
from the track it arrived on. Two ways to pair inbound to outbound:

  1. GTFS timing (turn_match.py): an arrival 15-75 min before the departure,
     same route preferred.
  2. Learned from data: on training days, which inbound number was sitting on
     the outbound's eventual track just before it posted? Pairs that recur
     across days are real equipment cycles.

"Uncovered" means uncovered by the engine as deployed: neither a pure circuit
under the train's own number nor a berth coordinate before the board posted.
All numbers are held-out (codebooks and pairs from the first 70% of days).
"""

import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

DB = "track_history.db"
CKT_MIN_N, CKT_MIN_PURITY = 20, 0.95
GPS_MIN_N, GPS_MIN_PURITY = 3, 0.98
PAIR_MIN_DAYS = 3
LOOKBACK_MIN = 90


def ts(s):
    return datetime.fromisoformat(s)


def mins(a, b):
    return (ts(a) - ts(b)).total_seconds() / 60


conn = sqlite3.connect(DB)
conn.execute("PRAGMA query_only=1")

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

# every train's circuit sequence (collapsed, for ordering) and per-row votes
# (uncollapsed, to match how the engine builds its codebook)
allseq = defaultdict(list)
rowvotes = defaultdict(Counter)
for tid, sd, ckt, pa in conn.execute(
        "SELECT train_id, service_date, ics_track_ckt, polled_at FROM vehicle_positions "
        "WHERE ics_track_ckt IS NOT NULL AND ics_track_ckt != '' ORDER BY polled_at"):
    k = (tid, sd)
    if not allseq[k] or allseq[k][-1][0] != ckt:
        allseq[k].append((ckt, pa))
    if k in trn:
        rowvotes[ckt][truth[k]] += 1
pure = {c: cnt.most_common(1)[0][0] for c, cnt in rowvotes.items()
        if sum(cnt.values()) >= CKT_MIN_N and cnt.most_common(1)[0][1] / sum(cnt.values()) >= CKT_MIN_PURITY}

# berth codebook and pre-posting GPS per departure
gps_rows = defaultdict(list)
gvotes = defaultdict(Counter)
for tid, sd, la, lo, trk, pa, at_penn in conn.execute(
        "SELECT train_id, service_date, gps_lat, gps_lon, track, polled_at, at_penn "
        "FROM observations WHERE gps_lat IS NOT NULL ORDER BY polled_at"):
    k = (tid, sd)
    if k not in truth:
        continue
    t = (trk or "").strip()
    if t.isdigit() and at_penn == 1 and k in trn:
        gvotes[(la, lo)][t] += 1
    if not t:
        gps_rows[k].append(((la, lo), pa))
gbook = {g: cnt.most_common(1)[0][0] for g, cnt in gvotes.items()
         if sum(cnt.values()) >= GPS_MIN_N and cnt.most_common(1)[0][1] / sum(cnt.values()) >= GPS_MIN_PURITY}
print("circuit sequences %d train-days | pure circuits %d | berth coords %d | held-out departures %d"
      % (len(allseq), len(pure), len(gbook), len(tst)))


def engine_pred(k):
    for ckt, pa in allseq.get(k, []):
        if pa >= posted[k]:
            break
        if ckt in pure:
            return pure[ckt]
    for g, pa in gps_rows.get(k, []):
        if pa >= posted[k]:
            break
        if g in gbook:
            return gbook[g]
    return None


cov = {k: engine_pred(k) for k in tst}
covered = [k for k in tst if cov[k] is not None]
uncovered = [k for k in tst if cov[k] is None]
print("engine, held-out: coverage %.1f%%  accuracy %.1f%%  -> %d uncovered\n"
      % (100.0 * len(covered) / len(tst),
         100.0 * sum(1 for k in covered if cov[k] == truth[k]) / max(len(covered), 1), len(uncovered)))

# who was sitting on which platform circuit, when (by day)
on_ckt = defaultdict(list)
for (tid, sd), s in allseq.items():
    for ckt, pa in s:
        if ckt in pure:
            on_ckt[sd].append((pa, tid, pure[ckt]))
for sd in on_ckt:
    on_ckt[sd].sort()


def sitting_before(k):
    """other trains on a Penn platform circuit within LOOKBACK_MIN before k
    posted, most recent first: [(time, train, track)]"""
    tid, sd = k
    out = [(pa, other, trk) for pa, other, trk in on_ckt.get(sd, [])
           if other != tid and pa < posted[k] and mins(posted[k], pa) <= LOOKBACK_MIN]
    return sorted(out, reverse=True)


def score(label, partner_of):
    n_pair = n_sig = n_hit = 0
    leads, by_track = [], Counter()
    for k in uncovered:
        inb = partner_of(k)
        if not inb:
            continue
        n_pair += 1
        s = [(c, pa) for c, pa in allseq.get((inb, k[1]), []) if pa < posted[k] and c in pure]
        if not s:
            continue
        n_sig += 1
        c, pa = s[-1]                              # last platform circuit the inbound sat on
        n_hit += (pure[c] == truth[k])
        leads.append(mins(posted[k], pa))
        by_track[truth[k]] += 1
    print("%s" % label)
    print("  uncovered departures with a partner              : %d" % n_pair)
    print("  ...whose partner sat on a pure circuit in time   : %d   (+%.1f pts of board coverage)"
          % (n_sig, 100.0 * n_sig / len(tst)))
    print("  ...and that track was RIGHT                      : %d   (%.0f%%)"
          % (n_hit, 100.0 * n_hit / max(n_sig, 1)))
    if leads:
        leads.sort()
        print("  lead over the board: median %.1f min  p25 %.1f  p75 %.1f"
              % (leads[len(leads) // 2], leads[len(leads) // 4], leads[3 * len(leads) // 4]))
        print("  new coverage by track: %s" % dict(sorted(by_track.items(), key=lambda kv: int(kv[0]))))
    return n_sig, n_hit


# ---------------------------------------------------------- 1. GTFS pairing
print("=" * 72)
print("1. GTFS-TIMED TURN PAIRS (arrival 15-75 min before, same route preferred)")
print("=" * 72)
try:
    import turn_match
    trips, gseq, svc = turn_match.load_gtfs()
    turn_of, _, _ = turn_match.build_turn_map(trips, gseq, svc, set(days))
    print("GTFS pairs available: %d" % len(turn_of))
    score("", lambda k: turn_of.get((k[1], k[0])))
except Exception as e:
    print("GTFS pairing unavailable: %s: %s" % (type(e).__name__, e))

# ------------------------------------------------------- 2. learned pairing
print("\n" + "=" * 72)
print("2. PAIRS LEARNED FROM DATA (which inbound was on the outbound's track, day after day)")
print("=" * 72)
pair_votes = defaultdict(Counter)
for k in trn:
    for pa, other, trk in sitting_before(k):
        if trk == truth[k]:
            pair_votes[k[0]][other] += 1
            break
pairs = {}
for out, cnt in pair_votes.items():
    inb, n = cnt.most_common(1)[0]
    if n >= PAIR_MIN_DAYS and n / sum(cnt.values()) >= 0.6:
        pairs[out] = inb
print("outbound numbers with a stable partner: %d  (seen >=%d days, >=60%% consistent)"
      % (len(pairs), PAIR_MIN_DAYS))
print("examples: %s" % ", ".join("%s<-%s" % (o, i) for o, i in sorted(pairs.items())[:10]))
print()
n_sig, n_hit = score("", lambda k: pairs.get(k[0]))

agree = tot = 0
for k in tst:
    inb = pairs.get(k[0])
    if not inb:
        continue
    s = [(c, pa) for c, pa in allseq.get((inb, k[1]), []) if pa < posted[k] and c in pure]
    if s:
        tot += 1
        agree += (pure[s[-1][0]] == truth[k])
print("\nsanity, ALL held-out departures with a learned partner: partner's track == actual track %d/%d (%.0f%%)"
      % (agree, tot, 100.0 * agree / max(tot, 1)))

# ---------------------------------------------------------- 3. what is left
print("\n" + "=" * 72)
print("3. WHAT IS LEFT")
print("=" * 72)
left = [k for k in uncovered if k[0] not in pairs]
any_sitting = sum(1 for k in left if sitting_before(k))
never_in_feed = sum(1 for k in left if not allseq.get(k))
print("uncovered with no learned partner : %d" % len(left))
print("  never appear in the vehicle feed: %d" % never_in_feed)
print("  some other train WAS sitting on a platform circuit in the prior 90 min: %d" % any_sitting)
print("  (i.e. a turn whose cycle we have not seen enough days of, or a yard move)")
print("=" * 72)
