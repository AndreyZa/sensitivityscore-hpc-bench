# Single-container Slurm cluster (slurmdbd+mariadb+slurmctld+slurmd, all one
# node) — purpose-built to smoke-test harness/submit/slurm_submit.py against a
# REAL sbatch/squeue/sacct, not just code review. See README.md in this dir.
#
# This is local dev/test tooling only — not part of the andreyza/* image set
# pushed for the actual experiment stand (no CLAUDE.md rebuild/push rule
# applies to this image).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        munge \
        slurmctld \
        slurmd \
        slurmdbd \
        slurm-client \
        mariadb-server \
        python3 \
        python3-pip \
        python3-venv \
        gosu \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/harness-venv \
    && /opt/harness-venv/bin/pip install --no-cache-dir "jinja2>=3.1.0" "redis>=5.0.0" "PyYAML>=6.0"

# Non-root user to submit jobs as — Slurm compute daemons run as root (need
# setuid to launch jobs as the submitting user) but submitting itself as root
# is atypical of real HPC usage and exercises the same uid-handling path a
# partner stand would.
RUN useradd -m -s /bin/bash testuser

COPY slurm.conf.tmpl /etc/slurm/slurm.conf.tmpl
COPY slurmdbd.conf.tmpl /etc/slurm/slurmdbd.conf.tmpl
COPY cgroup.conf /etc/slurm/cgroup.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["sleep", "infinity"]
