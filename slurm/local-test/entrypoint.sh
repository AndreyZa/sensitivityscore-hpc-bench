#!/usr/bin/env bash
# Brings up mariadb + munge + slurmdbd + slurmctld + slurmd, all in this one
# container, then execs the container CMD (default: sleep infinity, so you
# can `docker exec` in; smoke_test.py overrides CMD to run the actual test).
set -euo pipefail

STORAGE_PASS="localtest-pass"
HOSTNAME_NOW="$(hostname)"
CPUS="$(nproc)"

log() { echo "[entrypoint] $*" >&2; }

mkdir -p /var/spool/slurmd /var/spool/slurmctld /var/log/slurm /run/munge /run/mysqld
chown slurm:slurm /var/spool/slurmd /var/spool/slurmctld /var/log/slurm
chown munge:munge /run/munge
chown mysql:mysql /run/mysqld

# Real Boards/Sockets/CoresPerSocket/ThreadsPerCore from slurmd's own
# hardware detection, not a guess — leaving Sockets/Cores/Threads
# unspecified defaults to "1 core/thread per socket", which mismatches the
# actual hardware and slurmd logs it (harmlessly), but better to just match
# reality.
NODELINE="$(slurmd -C | head -1) NodeAddr=127.0.0.1 State=UNKNOWN"
log "rendering slurm.conf / slurmdbd.conf for hostname=${HOSTNAME_NOW} cpus=${CPUS}"
log "node line: ${NODELINE}"
sed -e "s/__HOSTNAME__/${HOSTNAME_NOW}/g" -e "s/__CPUS__/${CPUS}/g" \
  -e "s|__NODELINE__|${NODELINE}|" \
  /etc/slurm/slurm.conf.tmpl > /etc/slurm/slurm.conf
sed -e "s/__STORAGEPASS__/${STORAGE_PASS}/g" \
  /etc/slurm/slurmdbd.conf.tmpl > /etc/slurm/slurmdbd.conf
chown slurm:slurm /etc/slurm/slurmdbd.conf
chmod 600 /etc/slurm/slurmdbd.conf

log "starting munged"
gosu munge /usr/sbin/munged
for i in $(seq 1 20); do
  [ -S /run/munge/munge.socket.2 ] && break
  sleep 0.5
done
munge -n | unmunge >/dev/null || { log "munge self-test FAILED"; exit 1; }
log "munge OK"

if [ ! -d /var/lib/mysql/mysql ]; then
  log "initializing mariadb data dir"
  mariadb-install-db --user=mysql --datadir=/var/lib/mysql >/var/log/slurm/mariadb-install.log 2>&1
fi
log "starting mariadbd"
mariadbd --user=mysql --skip-networking=0 --bind-address=127.0.0.1 >/var/log/slurm/mariadbd.log 2>&1 &
for i in $(seq 1 40); do
  (echo > /dev/tcp/127.0.0.1/3306) >/dev/null 2>&1 && break
  sleep 0.5
done
(echo > /dev/tcp/127.0.0.1/3306) >/dev/null 2>&1 || { log "mariadbd did not come up"; cat /var/log/slurm/mariadbd.log >&2; exit 1; }
log "mariadb OK"

# Socket auth as the OS root user — MariaDB's default root@localhost account
# uses unix_socket/auth_socket, not a TCP-reachable password; connecting via
# --host=127.0.0.1 gets access-denied instead.
mysql -u root <<SQL
CREATE DATABASE IF NOT EXISTS slurm_acct_db;
CREATE USER IF NOT EXISTS 'slurm'@'localhost' IDENTIFIED BY '${STORAGE_PASS}';
GRANT ALL PRIVILEGES ON slurm_acct_db.* TO 'slurm'@'localhost';
FLUSH PRIVILEGES;
SQL
log "slurm_acct_db ready"

log "starting slurmdbd"
gosu slurm /usr/sbin/slurmdbd -D >/var/log/slurm/slurmdbd-stdout.log 2>&1 &
for i in $(seq 1 40); do
  (echo > /dev/tcp/127.0.0.1/6819) >/dev/null 2>&1 && break
  sleep 0.5
done
(echo > /dev/tcp/127.0.0.1/6819) >/dev/null 2>&1 || { log "slurmdbd did not come up"; cat /var/log/slurm/slurmdbd.log >&2 2>/dev/null; exit 1; }
log "slurmdbd OK"

log "starting slurmctld"
/usr/sbin/slurmctld -D >/var/log/slurm/slurmctld-stdout.log 2>&1 &
sleep 2

log "registering cluster with slurmdbd"
sacctmgr -i add cluster localtest || log "(cluster already registered, continuing)"

# cgroup/v2 + IgnoreSystemd=yes still expects a systemd-shaped
# /sys/fs/cgroup/system.slice/ to put its slurmstepd scope directory under —
# there's no systemd in this container to create it, so mkdir it ourselves
# (a single mkdir, no -p needed beyond this, is enough for slurmd's own
# nested <host>_slurmstepd.scope create to then succeed).
mkdir -p /sys/fs/cgroup/system.slice

log "starting slurmd"
/usr/sbin/slurmd -D >/var/log/slurm/slurmd-stdout.log 2>&1 &

log "waiting for node to become idle in sinfo"
for i in $(seq 1 40); do
  state="$(sinfo -h -o '%t' 2>/dev/null || true)"
  if [ "${state}" = "idle" ]; then
    break
  fi
  sleep 1
done
sinfo
scontrol show node || true

log "cluster up. exec: $*"
exec "$@"
