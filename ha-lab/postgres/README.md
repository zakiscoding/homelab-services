# Postgres streaming replication, failover and chaos tests for the job queue

[homelab-services](../../README.md) / `ha-lab/postgres/`

The homelab job queue lives in one Postgres 16 on the iMac (`homelab-services-postgres-1`). This lab
answers what a standby would buy it: how long writes stop when the primary dies, whether
acknowledged jobs survive, and what synchronous replication costs. Everything runs on a **copy**:

1. `pg_dump -Fc` of the live `homelab` DB first, kept in `backups/homelab-before-ha-lab-<ts>.dump`.
2. Restored into a separate compose project (`pg-ha-lab`) with a primary (`pg1`) and a hot standby
   (`pg2`), cloned from it with `pg_basebackup` and streaming through a replication slot.

The live database was never touched: no restarts, no config changes, no pg_hba edits.

| File | What it does |
|---|---|
| [compose.yml](compose.yml) | `pg1`, `pg2` (Postgres 16, `--data-checksums`), and a `client`. No published ports, no bind mounts: Colima on the iMac doesn't mount the external drive into its VM, so bind-mounted files show up as empty directories. Scripts are baked into the images instead |
| [node/node.sh](node/node.sh) | Node entrypoint. An empty data dir with no `PRIMARY_HOST` runs initdb, creates the replication role and sets WAL retention. An empty data dir with `PRIMARY_HOST` runs `pg_basebackup -R -C -S slot_<node>` |
| [client/workload.py](client/workload.py) | The job-queue workload, shaped like `api/jobs.py`. A producer INSERTs one `infer` job per transaction at 100/s and logs each acknowledged commit. A consumer claims with `FOR UPDATE SKIP LOCKED` and completes it. Both connect with a libpq multi-host DSN (`host=pg1,pg2 target_session_attrs=read-write`), so they find the new primary on their own |
| [chaos.py](chaos.py) | Driver, stdlib only. `setup` restores the dump, then each scenario runs. Failover = SIGKILL the primary, detect with 3 missed 1 s `pg_isready` probes, `pg_promote()`, then rejoin the old primary with `pg_rewind`, or a fresh base backup if rewind can't |
| [analyze.py](analyze.py) | RPO = acknowledged commits missing afterwards. RTO = longest gap between acknowledged commits around the fault. Unit-tested in [test_analyze.py](test_analyze.py) |

## Results

Measured 2026-11-14 on the iMac (M1, 8 GB; Colima VM 4 vCPU / 6 GB on the external USB drive),
on a copy of the live DB, 100 job INSERTs/s plus a SKIP LOCKED consumer. Raw JSON:
[results/](results/).

### Replication cost

| | commits | commit p50 | commit p99 | standby replay lag |
|---|---:|---:|---:|---:|
| async streaming | 988 | 1.34 ms | 85 ms | < 1 ms (0–45 ms sampled) |
| **sync** (`synchronous_standby_names = pg2`) | 749 | **1.90 ms** | 143 ms | 0.9 ms |

Synchronous commit adds about **0.6 ms at the median**: one network round trip to the standby
plus its WAL flush. The large p99 in both modes comes from the disk, not replication (see
*What broke*).

### Failover (primary SIGKILLed under load)

| | detect | promote | **write outage (RTO)** | acked commits lost (RPO) | client errors | rejoin of old primary |
|---|---:|---:|---:|---:|---:|---|
| async | 6.3 s | 1.8 s | **8.0 s** | **0** of 961 | 35 (all retried) | `pg_rewind`, streaming 11.7 s later |
| sync | 5.0 s | 1.0 s | **6.0 s** | **0** of 1,168 | 34 (all retried) | `pg_rewind`, streaming 9.6 s later |

- **Detection is most of the outage.** 3 missed 1 s probes plus `pg_isready` timeouts took 5–6 s.
  Promotion took 1–2 s. A real failover manager (Patroni, pg_auto_failover) makes the same
  trade: shorter timeouts mean faster failover but more false ones.
- **The clients needed no reconfiguration.** The multi-host DSN with
  `target_session_attrs=read-write` skipped the dead node and the read-only standby, and found
  `pg2` as soon as it was promoted. The errors are the retries during the gap.
- **Async lost nothing this time, by luck rather than design.** Replay lag was under a
  millisecond, so the standby had every commit when the primary died. Async promises nothing:
  any commit acknowledged in the lag window would be lost. Sync is the version where 0 is
  guaranteed, and it measured the same 0.
- **The old primary comes back as a standby without a full copy.** `pg_rewind` rewinds its
  diverged WAL to the new timeline. Its pass took 9.6–11.7 s, against about 150 s for a fresh
  base backup of the DB (560 MB at that point, with the WAL test table) on this disk.

### Network partition between primary and standby (10–12 s)

| | longest write stall | acked commits lost | standby back to streaming |
|---|---:|---:|---:|
| async | 2.3 s (async commits never wait for the standby; same tail as baseline) | 0 | 0.05 s after reconnect |
| **sync** | **13.5 s**: every commit waited for the standby | 0 | 0.04 s after reconnect |

**This is the price of synchronous replication.** With one sync standby, losing the standby stops
all writes on the primary, so the primary is only as available as the network to the standby.
The usual fixes are `ANY 1 (pg2, pg3)` with two standbys, or a manager that drops to async when
the standby is gone. The second trades the zero-loss guarantee back for availability.

### A hung standby vs WAL retention (`max_slot_wal_keep_size`)

The standby was frozen with `docker pause` (hung, not dead) while the primary wrote ~100 MB of WAL
under a 16 MB `max_slot_wal_keep_size`. From the primary's log:

```
15:10:45 LOG:  terminating process 659 to release replication slot "slot_pg2"
15:10:45 DETAIL:  The slot's restart_lsn 0/4F4F7F68 exceeds the limit by 78676120 bytes.
15:21:12 LOG:  invalidating obsolete replication slot "slot_pg2"
```
and from the standby once it was unpaused:
```
15:21:27 FATAL:  could not receive data from WAL stream: ERROR:  requested WAL segment 00000001000000000000004F has already been removed
```

1. **The size limit works.** The slot was invalidated (`wal_status = lost`) instead of letting
   WAL fill the disk. The standby can't catch up afterwards and has to be rebuilt; a fresh
   `pg_basebackup` took 57 s here.
2. **But a *hung* standby stalled the primary's checkpoint for 10.5 minutes.** To invalidate the
   slot, the checkpointer must terminate the WAL sender that holds it. That sender was stuck
   sending to a frozen peer: the VM's TCP stack still acknowledged with a zero window, so the
   connection never looked dead. It ignored the termination until the standby was unpaused at
   15:21:12, and `CHECKPOINT` blocked all that time. That's why the scenario's 600 s timeout fired
   twice, which first looked like slow disk. A dead standby would have closed the connection.
   A hung one (a frozen VM, a stuck NIC) is worse. Mitigations: TCP keepalive / `tcp_user_timeout`
   on the replication connection, monitoring of `pg_stat_replication`, and alerts on checkpoint
   duration.

## What broke along the way

These all came out of running the lab, and each is fixed in the code above:

1. **`pg_rewind` refused to run.** It needs checksums or `wal_log_hints` *recorded in
   pg_control*. A `-c wal_log_hints=on` command-line flag isn't enough, because `pg_rewind`'s own
   crash-recovery pass starts the server without it. Fix: initdb with `--data-checksums`.
2. **The rewound node connected as the wrong user.** `pg_rewind -R` writes `primary_conninfo` with
   the *source* connection's user (`app`), which pg_hba doesn't allow to replicate. Fix: write
   `primary_conninfo` for the `replicator` role yourself.
3. **One failed rejoin poisoned every later scenario.** The pair was left with a standby that
   wasn't streaming. Fix: a health guard before each scenario rebuilds the standby when it isn't
   streaming.
4. **`ALTER SYSTEM` silently didn't apply.** Settings passed as `postgres -c ...` outrank
   `postgresql.auto.conf`, so lowering `max_slot_wal_keep_size` at runtime did nothing. Fix: keep
   tunables in `postgresql.auto.conf`, not on the command line.
5. **The iMac's VM disk is the bottleneck** (diagnosed below with bpftrace, iostat, strace and perf). Commit p99 was 75–140 ms against a 1.3 ms median,
   `pg_basebackup` ran at about 3 MB/s, and a `CHECKPOINT` after ~300 MB of inserts took over 10
   minutes. The Colima VM's disk image lives on the external USB drive.

## Debugging the slow commits: perf, strace and bpftrace

The real slowdown here: **commit p50 is 1.3–1.9 ms but p99 is 85–143 ms**, and the worst commits
take 0.2–0.6 s, on 100 small INSERTs/s. [debug/trace_io.sh](debug/trace_io.sh) ran inside the
Colima VM (Ubuntu 24.04, kernel 6.8) during a 40 s synchronous-replication run, with four tools
in parallel. Outputs are in [debug/trace-2026-11-14/](debug/trace-2026-11-14/).

The VM has no outbound internet (neither do its containers, see the Colima notes in the hub), so
`apt` couldn't install anything. The arm64 `.deb`s for bpftrace 0.20.2, perf 6.8.12, strace and
sysstat were resolved in an emulated `--platform linux/arm64 ubuntu:24.04` container on the
laptop, shipped over SSH, and installed with `dpkg`, only the packages the VM didn't already
have, so nothing got downgraded. Some of the pulled-in dependencies (`systemd-resolved`,
`networkd-dispatcher`, `libc6-dev`, perf's `linux-tools`) couldn't configure against the VM's own
library versions. They were removed again afterwards (`dpkg --audit` clean; Colima's
`/etc/resolv.conf` never changed). bpftrace, strace and sysstat stay installed in the VM; perf
was removed after this run.

| Tool | What it showed |
|---|---|
| **bpftrace**, `fdatasync`/`fsync` latency per process | 7,483 Postgres syncs in 30 s. Most took 0.25–1 ms, but **12.7% took more than 8 ms**, 76 more than 64 ms, and the worst up to 1 s. That tail *is* the commit p99. The workload's own commits wait on these flushes (`synchronous_commit=on`) |
| **bpftrace**, block-I/O latency per device (`block_rq_issue` to `block_rq_complete`) | The same shape one layer down. On the docker data disk (`vdb`), requests cluster at 16–64 µs and 256–512 µs, plus a tail of ~1,400 requests (8%) at 8 ms–1 s. **The delay is below the guest kernel**: in the virtual disk and the host storage, not in Postgres or the filesystem |
| **iostat -x** | `vdb` does ~900 small writes/s (~4 KB each) plus **~400 flushes/s**. Utilization 56–79%, average write await 1.6–3.2 ms. The averages look harmless; the tail doesn't show in them |
| **/proc/pressure/io** | **some 67%, full 62%** over 60 s: for most of the run, every runnable task in the VM was stalled on I/O |
| **strace -T** on the WAL writer | Few syncs of its own (backends flush their own commits in sync mode), and each of those took 35–49 ms |
| **perf record -a** | **92% of CPU samples in the idle loop.** Nothing is CPU-bound, so the time is all waiting on storage |

**Diagnosis:** each durable commit is an `fdatasync`, which becomes a flush request on the VM's
virtio disk. That disk is an image file on the iMac's **external USB drive** (`diskutil`:
Protocol USB, External). The host occasionally takes tens to hundreds of milliseconds to
complete a flush. With ~400 flushes/s, those stalls hit a few percent of commits.

**Measured fix options:**

| | commit p50 | commit p99 | max | acked commits that can be lost on a crash |
|---|---:|---:|---:|---|
| `synchronous_commit = on` (sync standby) | 1.59 ms | 119 ms | 583 ms | none |
| **`synchronous_commit = off`** | **0.32 ms** | **1.0 ms** | 31 ms | the last ≤ 3 × `wal_writer_delay` (≈ 0.6 s) of commits. No corruption |

`synchronous_commit` can be set per transaction. So for the job queue, the cheap, safe split is:
keep job creation and state changes durable, and mark high-volume, low-value writes (worker
heartbeats, chat logs) `SET LOCAL synchronous_commit = off`. The structural fix is to move
Colima's disk image (`homelab-runtime/colima`) off the USB drive onto the iMac's internal SSD.
That takes Colima downtime and is out of scope for this lab; it's recorded as a follow-up.

## Run it

On the iMac (Colima's docker socket, `docker-compose` v5):

```bash
python3 chaos.py setup --dump ../../backups/homelab-before-ha-lab-<ts>.dump
python3 chaos.py all                 # every scenario; out/results-<ts>.json
python3 chaos.py failover_sync       # or one
python3 chaos.py down                # removes the lab (containers + volumes + images)
python3 -m pytest test_analyze.py    # analysis unit tests (any machine)
```
