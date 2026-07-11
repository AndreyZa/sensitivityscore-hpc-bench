# config-c — классический Slurm (без K8s)

Верхняя граница HPC-эффективности «как есть» (Программа экспериментов §3, H3).

## Запуск

```bash
mkdir -p results
sbatch --export=G4_THREADS=1,PHYSICS_LIST=QGSP_BERT,N_PRIMARIES=10000,OUTPUT_MODE=none,RNG_SEED=42 \
  geant4-low-s.sbatch
```

`--output=results/...` is a path relative to wherever `sbatch` was invoked
(the harness's cwd, per `config.yaml`'s note that `sbatch`/`squeue`/`sacct`
must be run from a login node or a node with Slurm client tools) — it must
resolve to the same writable directory regardless of which compute node the
job actually lands on. Standard on real HPC clusters (shared home/project
storage), but worth confirming explicitly rather than discovering it
mid-series as a `Permission denied`/`No such file or directory` on the
`--output` file (see `slurm/local-test/README.md`'s "Known gaps found" for
exactly that failure mode, hit locally as a uid mismatch).

## Overcommit (`--oversubscribe`)

В отличие от A/B, где overcommit — это просто несколько Job на узел, для чистого
Slurm это управляется явно через partition config / `--oversubscribe`:

```bash
sbatch --oversubscribe --export=... geant4-high-s.sbatch
```

Харнесс (`harness/submit/slurm_submit.py`) генерирует нужный набор флагов в
зависимости от точки плана (`overcommit ∈ {1.0, 1.5, 2.0}`).

## Makespan

```bash
sacct -j <job_id> --format=JobID,Elapsed,Start,End
```
