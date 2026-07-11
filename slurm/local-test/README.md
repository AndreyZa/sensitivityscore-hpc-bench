# slurm/local-test — smoke-test Slurm cluster for `slurm_submit.py`

`harness/submit/slurm_submit.py` (config C's submission backend) had never
run against a real Slurm before — only reviewed by reading the code. This is
a single-container Slurm cluster (munge + mariadb + slurmdbd + slurmctld +
slurmd, all one node, real accounting) purpose-built to close that gap
locally, before a partner stand is available. Local dev/test tooling only —
not part of the `andreyza/*` image set pushed for the actual experiment
stand, no CLAUDE.md image-rebuild rule applies here.

## What this does and doesn't validate

Validates the harness's own code: `sbatch` submission + `Submitted batch job
N` regex parsing, the `squeue`/`sacct` polling loop, `sacct` State/NodeList/
Elapsed/Start/End field parsing, the FAILED/TIMEOUT/OOM → `RuntimeError`
path, `scancel` cleanup, and `fetch_job_metrics`'s Redis-unreachable
fallback — against a genuine `slurmctld`/`slurmd`/`slurmdbd`, not just
plausible-looking code.

Does **not** validate: Pyxis/enroot (`srun --container-image=...`) — not
installed here, that's the container-runtime question already open to
partners (docs §8). See "Known gaps found" below for what this surfaced.

## Usage

```bash
docker build -t slurm-local-test:dev .
docker run -d --name slurm-local-test --privileged \
  -v "$(cd ../../harness && pwd)":/workspace/harness \
  -v "$(pwd)":/workspace/slurm/local-test:ro \
  slurm-local-test:dev
docker logs -f slurm-local-test    # wait for "cluster up"

docker exec -u testuser -w /workspace/harness slurm-local-test \
  /opt/harness-venv/bin/python3 /workspace/slurm/local-test/smoke_test.py

docker rm -f slurm-local-test
```

`--privileged` is for convenience running Slurm's own process-tracking
(`proctrack/linuxproc`) and mariadb cleanly inside Docker — this is throwaway
local test tooling, not something to run unattended or exposed.

## Known gaps found

(filled in after the first real run — see smoke_test.py output)
