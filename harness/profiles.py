"""profiles.py — low-S / high-S parameter tables, mirroring
docs/Технический_план_экспериментов.md §1.2 and the example manifests in
k8s/config-a-baremetal/. Kept in one place so the harness, the analysis pipeline,
and manual sanity-checks all agree on what "low-s"/"high-s" mean numerically.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Sensitivity:
    llc: str
    numa: str
    net: str
    io: str


@dataclass(frozen=True)
class ProfileSpec:
    env: dict[str, str]
    sensitivity: Sensitivity
    resources: dict[str, str]


PROFILES: dict[str, ProfileSpec] = {
    "low-s": ProfileSpec(
        env={
            "G4_THREADS": "1",
            "PHYSICS_LIST": "QGSP_BERT",
            # 300k, not 10k: at 10k the job finished in ~1-2s — below the
            # stand's measurement resolution (makespan dominated by container
            # startup + Geant4 physics-table init, and the metrics agent got
            # 0-1 real samples per job even at a 1s tick; the first full
            # matrix run returned 180/180 low-s rows without metrics). Low
            # SENSITIVITY must not mean short DURATION — this targets a
            # ~30-60s runtime so interference on low-s jobs is measurable.
            "N_PRIMARIES": "300000",
            "OUTPUT_MODE": "none",
            "RNG_SEED": "42",
        },
        sensitivity=Sensitivity(llc="low", numa="low", net="low", io="low"),
        resources={"cpu": "1", "memory_request": "1Gi", "memory_limit": "2Gi"},
    ),
    "high-s": ProfileSpec(
        env={
            "G4_THREADS": "8",  # = physical cores per NUMA domain on the target
            # node — adjust here per stand (there is no env override)
            "PHYSICS_LIST": "FTFP_BERT_HP",
            "N_PRIMARIES": "1000000",
            "OUTPUT_MODE": "ntuple",
            "RNG_SEED": "42",
        },
        sensitivity=Sensitivity(llc="high", numa="high", net="low", io="low"),
        resources={"cpu": "8", "memory_request": "4Gi", "memory_limit": "6Gi"},
    ),
}


def make_job_id(config: str, profile: str, overcommit: float, rep: int) -> str:
    """job_id naming scheme used consistently across §1.3 annotations, Redis keys
    (job:metrics:<job_id>:<node>), and the results schema (§5.1)."""
    return f"{config}-{profile}-oc{overcommit}-rep{rep:02d}"
