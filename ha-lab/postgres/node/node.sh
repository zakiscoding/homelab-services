#!/bin/sh
# One lab node. First start with an empty data dir:
#   PRIMARY_HOST empty -> normal initdb via the image entrypoint + replication role
#   PRIMARY_HOST set   -> pg_basebackup from it (writes standby.signal + primary_conninfo, slot)
# Later starts just run postgres with the lab settings; role is whatever the data dir says.
set -eu
# replication password for pg_basebackup / pg_rewind / the walreceiver, kept next to PGDATA
# so it survives container re-creation (the volume is the node's identity)
PASSFILE=/var/lib/postgresql/data/.pgpass
mkdir -p /var/lib/postgresql/data
echo "*:*:*:replicator:${REPL_PASSWORD}" > "$PASSFILE"
chmod 600 "$PASSFILE"; chown postgres:postgres "$PASSFILE" /var/lib/postgresql/data
export PGPASSFILE="$PASSFILE"
SETTINGS="-c wal_level=replica -c max_wal_senders=10 -c max_replication_slots=10 -c hot_standby=on
 -c wal_log_hints=on
 -c listen_addresses=* -c cluster_name=${NODE_NAME}
 -c log_min_duration_statement=-1 -c log_replication_commands=on"

if [ ! -s "$PGDATA/PG_VERSION" ] && [ -n "${PRIMARY_HOST:-}" ]; then
  mkdir -p "$PGDATA" && chown -R postgres:postgres "$(dirname "$PGDATA")" && chmod 700 "$PGDATA"
  until pg_isready -h "$PRIMARY_HOST" -U app -d homelab -q; do sleep 1; done
  su-exec postgres pg_basebackup -h "$PRIMARY_HOST" -U replicator \
      -D "$PGDATA" -X stream -R -C -S "slot_${NODE_NAME}" --checkpoint=fast
  # application_name lets the primary list this node in synchronous_standby_names
  sed -i "s/^primary_conninfo = '/primary_conninfo = 'application_name=${NODE_NAME} /" "$PGDATA/postgresql.auto.conf"
fi

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  # primary bootstrap: the stock entrypoint runs initdb and /docker-entrypoint-initdb.d/*
  mkdir -p /docker-entrypoint-initdb.d
  cat > /docker-entrypoint-initdb.d/10-lab.sh <<INIT
#!/bin/sh
set -e
psql -v ON_ERROR_STOP=1 -U app -d homelab -c "CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD '\${REPL_PASSWORD}'"
echo "host replication replicator all scram-sha-256" >> "\$PGDATA/pg_hba.conf"
psql -v ON_ERROR_STOP=1 -U app -d homelab -c "ALTER SYSTEM SET max_slot_wal_keep_size = '1GB'"
psql -v ON_ERROR_STOP=1 -U app -d homelab -c "ALTER SYSTEM SET wal_keep_size = '128MB'"
INIT
fi
# WAL retention lives in postgresql.auto.conf, NOT on the command line: a -c flag outranks
# ALTER SYSTEM, so chaos.py could not lower it at runtime (found in the slot_wal scenario).
# shellcheck disable=SC2086
exec docker-entrypoint.sh postgres $SETTINGS
