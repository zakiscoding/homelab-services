"""Drive the Postgres HA lab: replication checks, failovers and chaos scenarios (stdlib only).

Runs on the iMac host (Python 3.9+, docker CLI pointed at Colima). Everything happens in the
`pg-ha-lab` compose project, on a copy of the live DB restored from a pg_dump. It never touches
homelab-services-postgres-1.

    python3 chaos.py setup --dump ../../backups/homelab-before-ha-lab-<ts>.dump
    python3 chaos.py all            # every scenario in order, writes out/results-<ts>.json
    python3 chaos.py <scenario>     # one of SCENARIOS below
    python3 chaos.py down           # remove the lab (containers + volumes)

The failover "controller" here is deliberately simple and is part of what is measured: poll
the primary with pg_isready once per second, declare it dead after 3 misses, then promote the
standby with pg_promote(). Clients find the new primary through libpq multi-host DSNs
(target_session_attrs=read-write); nothing is reconfigured in the client.
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import analyze

HERE = Path(__file__).resolve().parent
P = "pg-ha-lab"
# the iMac has the standalone docker-compose binary (its `docker compose` plugin link is stale)
DC = (["docker-compose"] if shutil.which("docker-compose") else ["docker", "compose"]) + ["-p", P, "-f", str(HERE / "compose.yml")]
RATE, SECONDS = 100, 40
RESULTS = {}


def sh(args, check=True, timeout=120, capture=True):
    r = subprocess.run(args, capture_output=capture, text=True, timeout=timeout, cwd=HERE)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} -> {r.returncode}: {(r.stderr or '')[-400:]}")
    return (r.stdout or "").strip()


def ctr(node):
    return f"{P}-{node}-1"


def psql(node, sql, check=True, timeout=30):
    return sh(["docker", "exec", ctr(node), "psql", "-U", "app", "-d", "homelab", "-Atq", "-c", sql],
              check=check, timeout=timeout)


def env_file():
    """Generate lab-only passwords once into .env (gitignored)."""
    f = HERE / ".env"
    if not f.exists():
        f.write_text(f"LAB_PG_PASSWORD={secrets.token_urlsafe(24)}\nLAB_REPL_PASSWORD={secrets.token_urlsafe(24)}\n")
        f.chmod(0o600)
    return dict(line.split("=", 1) for line in f.read_text().split())


def roles():
    """(primary, standby) by asking each node pg_is_in_recovery()."""
    r = {n: psql(n, "select pg_is_in_recovery()", check=False) for n in ("pg1", "pg2")}
    prim = [n for n, v in r.items() if v == "f"]
    stby = [n for n, v in r.items() if v == "t"]
    if len(prim) != 1:
        raise RuntimeError(f"expected exactly one primary, got {r}")
    return prim[0], (stby[0] if stby else None)


def wait_streaming(primary, standby, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = psql(primary, f"select state from pg_stat_replication where application_name='{standby}'", check=False)
        if st == "streaming":
            return round(time.time() - t0, 2)
        time.sleep(0.5)
    raise RuntimeError(f"{standby} not streaming from {primary}")


def repl_status(primary):
    q = ("select application_name, state, sync_state, coalesce(extract(epoch from replay_lag)*1000,0)::numeric(10,3), "
         "pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) from pg_stat_replication")
    rows = [r.split("|") for r in psql(primary, q).splitlines() if r]
    return [{"standby": a, "state": b, "sync": c, "replay_lag_ms": float(d), "bytes_behind": int(float(e))}
            for a, b, c, d, e in rows]


def set_sync(primary, standby_name):
    """synchronous_standby_names = standby_name ('' = async) + synchronous_commit=on, reload."""
    psql(primary, f"alter system set synchronous_standby_names = '{standby_name}'")
    psql(primary, "alter system set synchronous_commit = 'on'")
    psql(primary, "select pg_reload_conf()")
    time.sleep(1)


def workload(run, seconds=SECONDS, rate=RATE):
    return subprocess.Popen(["docker", "exec", ctr("client"), "python", "workload.py", run, str(seconds), str(rate)],
                            cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def events(run):
    raw = sh(["docker", "exec", ctr("client"), "cat", f"/out/{run}.jsonl"])
    return [json.loads(x) for x in raw.splitlines() if x.strip()]


def present(node, run):
    out = psql(node, f"select payload->>'seq' from jobs where payload->>'run' = '{run}'")
    return [int(x) for x in out.splitlines() if x]


def failover(primary, standby):
    """SIGKILL the primary, detect with 3 missed 1 s pg_isready probes, promote the standby.
    Returns timestamps (epoch s)."""
    t_kill = time.time()
    sh(["docker", "kill", ctr(primary)])
    misses = 0
    while misses < 3:
        time.sleep(1)
        ok = subprocess.run(["docker", "exec", ctr(standby), "pg_isready", "-h", primary, "-t", "1"],
                            capture_output=True).returncode == 0
        misses = 0 if ok else misses + 1
    t_detect = time.time()
    psql(standby, "select pg_promote(true, 30)")
    t_promoted = time.time()
    return {"t_kill": t_kill, "detect_s": round(t_detect - t_kill, 3), "promote_s": round(t_promoted - t_detect, 3)}


def rebuild(node, primary):
    """Throw away `node`'s data and clone it again from `primary` (pg_basebackup -R -C -S)."""
    t0 = time.time()
    sh([*DC, "--profile", "standby", "rm", "-sf", node])
    sh(["docker", "volume", "rm", "-f", f"{P}_{node}"])
    psql(primary, f"select pg_drop_replication_slot('slot_{node}') "
                  f"from pg_replication_slots where slot_name='slot_{node}'", check=False)
    os.environ[f"{node.upper()}_PRIMARY_HOST"] = primary
    # no --wait: a base backup on the iMac's Colima disk runs at only a few MB/s, which can
    # outlast the container healthcheck (found in testing); wait for streaming instead
    sh([*DC, "--profile", "standby", "up", "-d", "--no-deps", node], timeout=120)
    lag = wait_streaming(primary, node, timeout=900)
    size = psql(primary, "select pg_size_pretty(pg_database_size('homelab'))", check=False)
    return {"method": "basebackup", "rebuild_s": round(time.time() - t0, 2), "db_size": size}


def rejoin(old, new):
    """Make the dead old primary a standby of `new`. First try pg_rewind (it runs crash recovery
    first, PG13+) and point primary_conninfo at `new` as the replication user; if pg_rewind
    can't (e.g. the WAL it needs before the divergence point is already recycled), fall back to
    a fresh base backup. Returns which method worked and how long it took."""
    pw = env_file()["LAB_PG_PASSWORD"]
    psql(new, f"select pg_create_physical_replication_slot('slot_{old}') "
              f"where not exists (select 1 from pg_replication_slots where slot_name='slot_{old}')")
    t0 = time.time()
    src = f"host={new} user=app dbname=homelab password={pw}"
    # pg_rewind -R would write primary_conninfo with the *source* user (app), which pg_hba
    # does not allow for replication (found in testing): write our own instead.
    script = (f"su-exec postgres pg_rewind -D $PGDATA --source-server='{src}' && "
              f"touch $PGDATA/standby.signal && chown postgres $PGDATA/standby.signal && "
              f"sed -i '/^primary_conninfo/d;/^primary_slot_name/d' $PGDATA/postgresql.auto.conf && "
              f"echo \"primary_conninfo = 'host={new} port=5432 user=replicator application_name={old}'\" >> $PGDATA/postgresql.auto.conf && "
              f"echo \"primary_slot_name = 'slot_{old}'\" >> $PGDATA/postgresql.auto.conf")
    r = subprocess.run([*DC, "--profile", "standby", "run", "--rm", "--no-deps", "--entrypoint", "sh", old, "-c", script],
                       capture_output=True, text=True, cwd=HERE, timeout=300)
    if r.returncode == 0:
        sh(["docker", "start", ctr(old)])
        try:
            lag = wait_streaming(new, old)
            return {"method": "pg_rewind", "rewind_and_restart_s": round(time.time() - t0, 2),
                    "streaming_after_s": lag}
        except RuntimeError:
            pass
    why = [l for l in (r.stdout + r.stderr).splitlines() if "pg_rewind:" in l][-2:]
    out = rebuild(old, new)
    out["pg_rewind_failed"] = why
    out["total_s"] = round(time.time() - t0, 2)
    return out


def ensure_pair():
    """Before each scenario: one primary, and the other node streaming from it (rebuild if not)."""
    prim, stby = None, None
    for n in ("pg1", "pg2"):
        if subprocess.run(["docker", "exec", ctr(n), "pg_isready", "-q"], capture_output=True).returncode == 0:
            if psql(n, "select pg_is_in_recovery()", check=False) == "f":
                prim = n
    if prim is None:
        raise RuntimeError("no primary is up")
    other = "pg2" if prim == "pg1" else "pg1"
    st = psql(prim, f"select state from pg_stat_replication where application_name='{other}'", check=False)
    if st != "streaming":
        rebuild(other, prim)
    for n in ("pg1", "pg2"):  # every scenario starts async with default WAL retention
        psql(n, "alter system reset synchronous_standby_names", check=False)
    psql(prim, "select pg_reload_conf()")
    return prim, other


# ------------------------------------------------------------------ scenarios

def sc_baseline_async(tag):
    prim, stby = roles()
    set_sync(prim, "")
    run = f"async-{tag}"
    w = workload(run)
    time.sleep(SECONDS / 2)
    mid = repl_status(prim)
    w.wait()
    s = analyze.summarize(events(run), present=present(prim, run))
    return {"primary": prim, "replication": mid, **s}


def sc_baseline_sync(tag):
    prim, stby = roles()
    set_sync(prim, stby)
    run = f"sync-{tag}"
    w = workload(run)
    time.sleep(SECONDS / 2)
    mid = repl_status(prim)
    w.wait()
    s = analyze.summarize(events(run), present=present(prim, run))
    set_sync(prim, "")
    return {"primary": prim, "replication": mid, **s}


def _failover_scenario(tag, sync):
    prim, stby = roles()
    set_sync(prim, stby if sync else "")
    run = f"failover-{'sync' if sync else 'async'}-{tag}"
    w = workload(run, seconds=45)
    time.sleep(15)
    ts = failover(prim, stby)
    w.wait()
    s = analyze.summarize(events(run), fault_t=ts["t_kill"], present=present(stby, run))
    # the promoted node keeps synchronous_standby_names only if it was set there; reset to async
    set_sync(stby, "")
    try:
        rj = rejoin(prim, stby)
    except Exception as ex:  # keep the failover numbers even if the rejoin breaks
        rj = {"error": str(ex)[-400:]}
    return {"killed": prim, "promoted": stby, **ts, **s, "rejoin": rj}


def sc_failover_async(tag):
    return _failover_scenario(tag, sync=False)


def sc_failover_sync(tag):
    return _failover_scenario(tag, sync=True)


def _partition(tag, sync, down_s=10):
    prim, stby = roles()
    set_sync(prim, stby if sync else "")
    run = f"partition-{'sync' if sync else 'async'}-{tag}"
    net = f"{P}_default"
    w = workload(run, seconds=35)
    time.sleep(10)
    t0 = time.time()
    sh(["docker", "network", "disconnect", net, ctr(stby)])
    time.sleep(down_s)
    sh(["docker", "network", "connect", "--alias", stby, net, ctr(stby)])
    t1 = time.time()
    w.wait()
    ev = events(run)
    s = analyze.summarize(ev, present=present(prim, run))
    stall = analyze.stall(ev, t0, t1 + 15)
    resync = wait_streaming(prim, stby)
    set_sync(prim, "")
    return {"primary": prim, "standby_cut_s": round(t1 - t0, 2), "longest_write_stall_s": stall,
            "streaming_again_after_s": resync, **s}


def sc_partition_async(tag):
    return _partition(tag, sync=False)


def sc_partition_sync(tag):
    return _partition(tag, sync=True)


def sc_slot_wal(tag):
    """Pause the standby while the primary writes ~100 MB of WAL under a 16 MB
    max_slot_wal_keep_size: the slot is invalidated and the standby cannot catch up."""
    prim, stby = roles()
    psql(prim, "alter system set max_slot_wal_keep_size = '16MB'")
    psql(prim, "alter system set wal_keep_size = '0'")
    psql(prim, "select pg_reload_conf()")
    sh(["docker", "pause", ctr(stby)])
    psql(prim, "create table if not exists lab_bloat (id int, pad text)")
    t0 = time.time()
    psql(prim, "insert into lab_bloat select g, repeat('x', 200) from generate_series(1, 400000) g", timeout=900)
    psql(prim, "checkpoint", timeout=600)
    psql(prim, "select pg_switch_wal()", timeout=120)
    psql(prim, "checkpoint", timeout=600)
    slot = psql(prim, f"select wal_status, coalesce(safe_wal_size,-1) from pg_replication_slots where slot_name='slot_{stby}'")
    wal_mb = psql(prim, "select (sum(size)/1048576)::int from pg_ls_waldir()")
    show = psql(prim, "show max_slot_wal_keep_size")
    sh(["docker", "unpause", ctr(stby)])
    time.sleep(8)
    st = psql(prim, f"select coalesce(state,'none') from pg_stat_replication where application_name='{stby}'", check=False)
    logs = subprocess.run(["docker", "logs", "--since", "15s", ctr(stby)], capture_output=True, text=True)
    err = [l for l in (logs.stdout + logs.stderr).splitlines() if "removed" in l or "FATAL" in l][:3]
    # repair only if it is really broken: rebuild the standby from a fresh base backup
    t1 = time.time()
    rb = rebuild(stby, prim) if st != "streaming" else {"method": "none needed"}
    psql(prim, "alter system set max_slot_wal_keep_size = '1GB'")
    psql(prim, "alter system set wal_keep_size = '128MB'")
    psql(prim, "drop table lab_bloat")
    psql(prim, "select pg_reload_conf()")
    return {"primary": prim, "max_slot_wal_keep_size": show, "write_s": round(t1 - t0, 2),
            "slot_after": slot, "pg_wal_mb": wal_mb,
            "standby_state_after_unpause": st or "not connected", "standby_errors": err,
            "repair": rb, "write_to_repaired_s": round(time.time() - t1, 2)}


SCENARIOS = {
    "baseline_async": sc_baseline_async, "baseline_sync": sc_baseline_sync,
    "failover_async": sc_failover_async, "failover_sync": sc_failover_sync,
    "partition_async": sc_partition_async, "partition_sync": sc_partition_sync,
    "slot_wal": sc_slot_wal,
}


def setup(dump):
    e = env_file()
    os.environ.update(e)
    t0 = time.time()
    sh([*DC, "up", "-d", "--build", "--wait", "pg1", "client"], timeout=600)
    # restore the copy of the live DB into the new primary, before the standby clones it
    with open(dump, "rb") as f:
        r = subprocess.run(["docker", "exec", "-i", ctr("pg1"), "pg_restore", "-U", "app", "-d", "homelab",
                            "--no-owner", "--no-acl"], stdin=f, capture_output=True, text=False)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode()[-400:])
    sh([*DC, "--profile", "standby", "up", "-d", "--build", "--wait", "pg2"], timeout=600)
    prim, stby = roles()
    lag = wait_streaming(prim, stby)
    jobs = psql(prim, "select count(*) from jobs")
    print(json.dumps({"up_s": round(time.time() - t0, 1), "primary": prim, "standby": stby,
                      "streaming_after_s": lag, "jobs_rows_restored": int(jobs),
                      "standby_jobs": int(psql(stby, "select count(*) from jobs"))}, indent=1))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.environ.update(env_file())
    if cmd == "setup":
        return setup(sys.argv[sys.argv.index("--dump") + 1])
    if cmd == "down":
        return sh([*DC, "--profile", "standby", "down", "-v", "--rmi", "local"], timeout=120)
    names = list(SCENARIOS) if cmd == "all" else cmd.split(",")
    tag = time.strftime("%H%M%S")
    for n in names:
        print(f"=== {n}", flush=True)
        t0 = time.time()
        try:
            ensure_pair()
            RESULTS[n] = {**SCENARIOS[n](tag), "wall_s": round(time.time() - t0, 1)}
        except Exception as ex:  # record and keep going: a broken scenario is a result too
            RESULTS[n] = {"error": str(ex)[-500:], "wall_s": round(time.time() - t0, 1)}
        print(json.dumps({k: v for k, v in RESULTS[n].items() if k not in ("lost_seqs",)}, indent=1), flush=True)
    out = HERE / "out" / f"results-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(RESULTS, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
