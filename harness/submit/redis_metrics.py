"""redis_metrics.py — shared helper to pull job:metrics:<job_id>:<node> back out of
Redis after a run completes (docs §3.2, §5.1). Used by every submit/* backend so
the resulting results.parquet row schema is identical regardless of which
config produced it.
"""

from __future__ import annotations

import os

import redis


def _connect(redis_addr: str) -> redis.Redis:
    """REDIS_ADDR env overrides config (see fetch_job_metrics docstring)."""
    redis_addr = os.environ.get("REDIS_ADDR", redis_addr)
    host, _, port = redis_addr.partition(":")
    return redis.Redis(
        host=host,
        port=int(port or 6379),
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )


def purge_job_metrics(redis_addr: str, job_id: str) -> None:
    """Deletes any leftover job:metrics:<job_id>:* keys before (re)submission.

    Belt to fetch_job_metrics' export-then-delete braces: if a previous run
    crashed between the agent writing and the harness reading, the stale key
    would otherwise survive (no TTL) and pollute this run's accumulating sums
    — job_ids are deterministic across runs. Never raises, same rationale as
    fetch_job_metrics: a missing metrics pipeline must not fail the run.
    """
    try:
        r = _connect(redis_addr)
        keys = list(r.scan_iter(match=f"job:metrics:{job_id}:*"))
        if keys:
            r.delete(*keys)
    except redis.exceptions.RedisError:
        pass


def fetch_job_metrics(redis_addr: str, job_id: str, node: str) -> dict:
    """Returns the job:metrics:<job_id>:<node> hash, or an all-NaN-ish dict with
    approximation="missing" if the agent never wrote anything for this job/node
    pair (e.g. agent down, or node name mismatch between K8s and Slurm naming),
    OR approximation="no-agent" if Redis itself is unreachable (e.g.
    metrics-agent/Redis not deployed yet — docs Фаза 4, deferred/not a blocker
    for earlier phases). Either way this function never raises: a missing
    metrics pipeline should not fail the whole job attempt in run_experiment.py
    (makespan_s is still valid and worth recording even without PMU data).

    redis_addr (from config.yaml) is the in-cluster DNS name — correct once the
    harness itself runs inside the cluster/stand network, but unreachable from
    a laptop running the harness against a local dev cluster (confirmed: the
    first real pilot run got "no-agent" on every single row even though
    metrics-agent had written every job:metrics:* key correctly — the harness
    process just couldn't resolve *.svc.cluster.local from the host). REDIS_ADDR
    env var overrides it, matching the same var metrics-agent/the scheduler
    plugin use — e.g. `export REDIS_ADDR=localhost:16379` after `kubectl
    port-forward svc/redis -n sensitivityscore-system 16379:6379`.
    """
    empty = {
        "llc_miss_rate": float("nan"),
        "numa_remote_ratio": float("nan"),
        "net_bw": float("nan"),
        "io_iops": float("nan"),
        "io_pressure": float("nan"),
    }

    try:
        r = _connect(redis_addr)
        key = f"job:metrics:{job_id}:{node}"
        fields = r.hgetall(key)
        # Export-then-delete (the contract in metrics-agent's WriteJobMetrics
        # docstring): job_ids are deterministic and the hash now ACCUMULATES
        # sums, so a leftover key would silently mix a previous run's samples
        # into a rerun — or be returned wholesale if the agent is down.
        if fields:
            r.delete(key)
    except redis.exceptions.RedisError:
        # Redis unreachable (connection refused, DNS not found, timeout, ...) —
        # distinct from "reachable but no data yet" (below), useful when
        # debugging why a whole series has no metrics at all vs. just one
        # node/job pair.
        return {**empty, "approximation": "no-agent"}

    if not fields:
        return {**empty, "approximation": "missing"}

    # The agent accumulates running sums + a sample counter (one HINCRBYFLOAT
    # per ~5s tick, see metrics-agent/pkg/redisclient.WriteJobMetrics); the
    # job-lifetime mean is <dim>_sum / samples. Previously the hash held only
    # the LAST sample before completion — usually the teardown phase, not
    # representative of the job.
    try:
        samples = int(fields.get("samples", "0"))
    except ValueError:
        samples = 0
    if samples <= 0:
        return {**empty, "approximation": "missing"}

    def lifetime_mean(field: str) -> float:
        return float(fields.get(field, "nan")) / samples

    return {
        "llc_miss_rate": lifetime_mean("llc_miss_rate_sum"),
        "numa_remote_ratio": lifetime_mean("numa_remote_ratio_sum"),
        "net_bw": lifetime_mean("net_bw_sum"),
        # io_iops — сырая активность (ops/s, агрессор); io_pressure — PSI-доля
        # времени в ожидании IO, [0,1] (её же читает планировщик из node:metrics).
        "io_iops": lifetime_mean("io_iops_sum"),
        "io_pressure": lifetime_mean("io_pressure_sum"),
        "approximation": fields.get("approximation", ""),
    }
