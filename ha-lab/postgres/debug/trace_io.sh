#!/bin/bash
# Run as root inside the Colima VM while chaos.py drives the workload (~40 s).
# Captures, in parallel, for 30 s:
#   iostat -x       per-device await/util for the docker data disk (vdb)
#   bpftrace        fdatasync/fsync latency histograms per process, block-I/O latency per device
#   strace -T       the WAL writer's and one backend's sync calls, with time spent in each
#   perf record -a  where CPU time goes system-wide (is anything CPU-bound?)
set -u
OUT=${1:-/tmp/pgdebug}; mkdir -p "$OUT"; cd "$OUT"
DUR=30
PRIMARY=${PRIMARY:-pg1}
# cluster_name puts the node name in the process title: "postgres: pg1: walwriter"
WALW=$(pgrep -f "postgres: ${PRIMARY}: walwriter" | head -1)
echo "lab primary $PRIMARY walwriter pid: $WALW; all walwriters: $(pgrep -f 'postgres: .*walwriter' | tr '
' ' ')" > pids.txt
iostat -x -d vdb 1 $DUR > iostat.txt &
bpftrace -e '
tracepoint:syscalls:sys_enter_fdatasync, tracepoint:syscalls:sys_enter_fsync { @start[tid] = nsecs; }
tracepoint:syscalls:sys_exit_fdatasync, tracepoint:syscalls:sys_exit_fsync /@start[tid] && pid == '$WALW'/ {
  @walwriter_sync_us = hist((nsecs - @start[tid]) / 1000); }
tracepoint:syscalls:sys_exit_fdatasync, tracepoint:syscalls:sys_exit_fsync /@start[tid]/ {
  @sync_us[comm] = hist((nsecs - @start[tid]) / 1000); @sync_count[comm] = count(); delete(@start[tid]); }
tracepoint:block:block_rq_issue { @rq[args.dev, args.sector] = nsecs; }
tracepoint:block:block_rq_complete /@rq[args.dev, args.sector]/ {
  @blk_us[args.dev] = hist((nsecs - @rq[args.dev, args.sector]) / 1000); delete(@rq[args.dev, args.sector]); }
interval:s:'$DUR' { exit(); }' > bpftrace.txt 2>&1 &
WALW=$(pgrep -f "postgres: .*walwriter" | head -1)
echo "walwriter pid(s): $(pgrep -f 'postgres: .*walwriter' | tr '\n' ' ')" > pids.txt
timeout $DUR strace -f -T -tt -e trace=fdatasync,fsync,pwrite64,write -p "$WALW" -o strace_walwriter.txt &
timeout $DUR strace -c -f -e trace=fdatasync,fsync,pwrite64 -p "$WALW" -o strace_walwriter_summary.txt &
perf record -a -g -F 99 -o perf.data -- sleep $DUR >/dev/null 2>&1
wait
perf report -i perf.data --stdio --no-children --sort comm,dso 2>/dev/null | grep -v "^#" | grep -v "^$" | head -40 > perf_top.txt
cat /proc/pressure/io > psi_io.txt
echo done
