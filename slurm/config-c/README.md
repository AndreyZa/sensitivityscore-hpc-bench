# config-c — классический Slurm (без K8s)

Верхняя граница HPC-эффективности «как есть» (Программа экспериментов §3, H3).

## Запуск

```bash
mkdir -p results
sbatch --export=G4_THREADS=1,PHYSICS_LIST=QGSP_BERT,N_PRIMARIES=10000,OUTPUT_MODE=none,RNG_SEED=42 \
  geant4-low-s.sbatch
```

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
