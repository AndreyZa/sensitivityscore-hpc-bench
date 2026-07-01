"""redis_metrics.py — shared helper to pull job:metrics:<job_id>:<node> back out of
Redis after a run completes (docs §3.2, §5.1). Used by every submit/* backend so
the resulting results.parquet row schema is identical regardless of which
config produced it.
"""

from __future__ import annotations

import redis


def fetch_job_metrics(redis_addr: str, job_id: str, node: str) -> dict:
    """Returns the job:metrics:<job_id>:<node> hash, or an all-NaN-ish dict with
    approximation="missing" if the agent never wrote anything for this job/node
    pair (e.g. agent down, or node name mismatch between K8s and Slurm naming)."""
    host, _, port = redis_addr.partition(":")
    r = redis.Redis(host=host, port=int(port or 6379), decode_responses=True)

    key = f"job:metrics:{job_id}:{node}"
    fields = r.hgetall(key)
    if not fields:
        return {
            "llc_miss_rate": float("nan"),
            "numa_remote_ratio": float("nan"),
            "net_bw": float("nan"),
            "io_iops": float("nan"),
            "approximation": "missing",
        }

    return {
        "llc_miss_rate": float(fields.get("llc_miss_rate", "nan")),
        "numa_remote_ratio": float(fields.get("numa_remote_ratio", "nan")),
        "net_bw": float(fields.get("net_bw", "nan")),
        "io_iops": float(fields.get("io_iops", "nan")),
        "approximation": fields.get("approximation", ""),
    }
