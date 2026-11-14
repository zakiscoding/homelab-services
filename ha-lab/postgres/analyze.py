"""Pure analysis of one HA-lab run (stdlib only, Python 3.9+; unit-tested in test_analyze.py).

Inputs: the workload's JSON-lines events, the fault time (epoch s, or None), and the set of
`seq` values actually present on the surviving primary afterwards.
  RPO: acknowledged commits (the client was told COMMIT succeeded) that are missing now.
  RTO: last acknowledged commit before the fault -> first acknowledged commit after it.
"""

import json
import math
import statistics


def load(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def pct(xs, p):
    """p-th percentile (nearest-rank) of a non-empty list."""
    s = sorted(xs)
    k = max(0, min(len(s) - 1, math.ceil(p / 100.0 * len(s)) - 1))
    return s[k]


def summarize(events, fault_t=None, present=None):
    acks = [e for e in events if e["ev"] == "ack"]
    errs = [e for e in events if e["ev"] == "err"]
    out = {"acked": len(acks), "errors": len(errs)}
    lat = [e["ms"] for e in acks if "ms" in e]
    if lat:
        out["commit_ms"] = {"p50": round(statistics.median(lat), 3), "p99": round(pct(lat, 99), 3),
                            "max": round(max(lat), 3)}
    if fault_t is not None and acks:
        before = [e for e in acks if e["t"] <= fault_t]
        after = [e for e in acks if e["t"] > fault_t]
        out["last_ack_before_fault_s"] = round(fault_t - before[-1]["t"], 3) if before else None
        if after:
            out["first_ack_after_fault_s"] = round(after[0]["t"] - fault_t, 3)
            # RTO = the longest write outage around the fault: the biggest gap between consecutive
            # acks from 1 s before the fault onward. (Not "first ack after the fault": a commit
            # already in flight to the dying primary can still return a few ms after the kill.)
            ts = [e["t"] for e in acks if e["t"] >= fault_t - 1.0]
            out["rto_s"] = round(max(b - a for a, b in zip(ts, ts[1:])), 3) if len(ts) > 1 else None
            out["max_commit_ms"] = max(lat) if lat else None
        else:
            out["rto_s"] = None  # never recovered within the run
        conns = [e for e in events if e["ev"] == "conn" and e["t"] > fault_t]
        out["reconnected_to"] = sorted({e["host"] for e in conns})
    if present is not None:
        acked = {e["seq"] for e in acks}
        lost = sorted(acked - set(present))
        out["present"] = len(set(present))
        out["lost_acked"] = len(lost)
        out["lost_seqs"] = lost[:50]
        # committed on the old primary but never acknowledged (crash between commit and reply)
        out["present_not_acked"] = len(set(present) - acked)
    return out


def stall(events, start_t, end_t):
    """Longest gap between consecutive acks inside [start_t, end_t] (s), e.g. a partition window."""
    ts = [start_t] + [e["t"] for e in events if e["ev"] == "ack" and start_t <= e["t"] <= end_t] + [end_t]
    return round(max(b - a for a, b in zip(ts, ts[1:])), 3)
