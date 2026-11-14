"""Job-queue workload for the Postgres HA lab, shaped like homelab-services api/jobs.py.

Producer: INSERT one `infer` job per transaction (payload {"seq": n, "run": RUN}); after COMMIT
returns, the seq is *acknowledged* and logged. Consumer: claim the oldest pending job with
SELECT ... FOR UPDATE SKIP LOCKED, mark it running, then succeeded (two transactions, like the
API's claim + report). Both connect through libpq multi-host DSN with
target_session_attrs=read-write, so after a failover they find the new primary by themselves.

Every event is one JSON line in /out/<run>.jsonl (fsync'd):
  {"t": epoch, "ev": "ack", "seq": n}            producer commit returned
  {"t": epoch, "ev": "err", "who": ..., "msg": ...}   any error (then reconnect)
  {"t": epoch, "ev": "conn", "who": ..., "host": ...} (re)connected, to which node
  {"t": epoch, "ev": "done", "seq": n, "lat_ms": [...]} final, with commit latencies sample

Usage (in the client container): python workload.py RUN SECONDS [RATE_PER_S]
"""

import json
import os
import sys
import threading
import time

import psycopg

DSN = os.environ["LAB_DSN"]
RUN, SECONDS = sys.argv[1], float(sys.argv[2])
RATE = float(sys.argv[3]) if len(sys.argv) > 3 else 100.0
OUT = open(f"/out/{RUN}.jsonl", "a", buffering=1)
LOCK = threading.Lock()
STOP = time.time() + SECONDS


def log(**kw):
    with LOCK:
        OUT.write(json.dumps({"t": time.time(), **kw}) + "\n")
        OUT.flush()
        os.fsync(OUT.fileno())


def connect(who):
    while time.time() < STOP:
        try:
            c = psycopg.connect(DSN, autocommit=False)
            log(ev="conn", who=who, host=c.info.host)
            return c
        except psycopg.Error as e:
            log(ev="err", who=who, msg=str(e).splitlines()[0][:160])
            time.sleep(0.2)
    return None


def producer():
    conn, seq, lat = connect("producer"), 0, []
    period = 1.0 / RATE
    while conn and time.time() < STOP:
        t0 = time.perf_counter()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO jobs (type, status, priority, payload) VALUES ('infer', 'pending', 50, %s)",
                            (json.dumps({"seq": seq + 1, "run": RUN}),))
            conn.commit()
            seq += 1
            dt = (time.perf_counter() - t0) * 1e3
            lat.append(round(dt, 3))
            log(ev="ack", seq=seq, ms=round(dt, 3))
        except psycopg.Error as e:
            log(ev="err", who="producer", msg=str(e).splitlines()[0][:160])
            try:
                conn.close()
            except Exception:
                pass
            conn = connect("producer")
            continue
        time.sleep(max(0.0, period - (time.perf_counter() - t0)))
    log(ev="done", seq=seq, lat_ms=lat[-5000:])


def consumer():
    conn = connect("consumer")
    while conn and time.time() < STOP:
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT id FROM jobs WHERE type='infer' AND status='pending' AND payload->>'run' = %s
                               ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED""", (RUN,))
                row = cur.fetchone()
                if row:
                    cur.execute("UPDATE jobs SET status='running', locked_by='lab', started_at=now(), "
                                "lease_expires_at=now()+interval '5 minutes' WHERE id=%s", row)
            conn.commit()
            if row:
                with conn.cursor() as cur:
                    cur.execute("UPDATE jobs SET status='succeeded', finished_at=now(), lease_expires_at=NULL "
                                "WHERE id=%s", row)
                conn.commit()
            else:
                time.sleep(0.01)
        except psycopg.Error as e:
            log(ev="err", who="consumer", msg=str(e).splitlines()[0][:160])
            try:
                conn.close()
            except Exception:
                pass
            conn = connect("consumer")


threads = [threading.Thread(target=producer), threading.Thread(target=consumer)]
for t in threads:
    t.start()
for t in threads:
    t.join()
