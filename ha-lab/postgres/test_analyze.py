"""python -m pytest ha-lab/postgres/test_analyze.py"""

import analyze


def ev(t, n, ms=1.0):
    return {"t": t, "ev": "ack", "seq": n, "ms": ms}


def test_rpo_and_rto():
    events = [ev(1.0, 1), ev(2.0, 2), ev(3.0, 3), {"t": 3.5, "ev": "err", "who": "producer", "msg": "x"},
              {"t": 9.0, "ev": "conn", "who": "producer", "host": "pg2"}, ev(9.5, 4, ms=6000.0)]
    s = analyze.summarize(events, fault_t=3.2, present=[1, 2, 4, 5])
    assert s["acked"] == 4 and s["errors"] == 1
    assert s["lost_acked"] == 1 and s["lost_seqs"] == [3]
    assert s["present_not_acked"] == 1          # seq 5 committed, never acknowledged
    assert s["rto_s"] == 6.5                    # longest ack gap from t=2.2: 3.0 -> 9.5
    assert s["first_ack_after_fault_s"] == 6.3
    assert s["reconnected_to"] == ["pg2"]
    assert s["commit_ms"]["max"] == 6000.0


def test_no_recovery():
    s = analyze.summarize([ev(1.0, 1)], fault_t=2.0, present=[1])
    assert s["rto_s"] is None and s["lost_acked"] == 0


def test_pct_and_stall():
    assert analyze.pct(list(range(1, 101)), 99) == 99
    assert analyze.pct([5.0], 50) == 5.0
    events = [ev(10.0, 1), ev(10.1, 2), ev(14.0, 3), ev(14.1, 4)]
    assert analyze.stall(events, 10.0, 15.0) == 3.9


def test_rto_ignores_in_flight_commit():
    # a commit lands 0.05 s after the kill (it was already in flight), then a 4 s outage
    events = [ev(9.9, 1), ev(10.05, 2), ev(14.05, 3), ev(14.06, 4)]
    s = analyze.summarize(events, fault_t=10.0, present=[1, 2, 3, 4])
    assert s["first_ack_after_fault_s"] == 0.05
    assert s["rto_s"] == 4.0
