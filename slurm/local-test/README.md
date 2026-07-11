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

First real run against genuine `sbatch`/`squeue`/`sacct` immediately found a
bug that pure code review had missed:

- **`--mem` rejected outright (fixed).** `profiles.py`'s `resources` dict is
  shared between the K8s manifest template (Kubernetes quantities: `1Gi`,
  `4Gi`) and `sbatch-template.sh.j2`'s `#SBATCH --mem={{
  resources.memory_request }}`. Slurm's `--mem` does not understand the
  Kubernetes `Ki/Mi/Gi/Ti` suffix — `sbatch` rejected every submission with
  `error: Invalid --mem specification`, confirmed here. **Every config-C
  submission would have failed before ever reaching a compute node.** Fixed
  in `slurm_submit.py` via `_slurm_mem()`, which strips the trailing `i`
  before rendering the sbatch script (same magnitude either way — both are
  binary multiples — just Slurm's own suffix spelling). `profiles.py` itself
  is untouched since the K8s template needs the original `Gi` form.

- **`results/` must be writable by whatever OS user runs `sbatch` (not a
  bug, but undocumented).** `--output=results/{{ job_id }}-%j.out` is a
  relative path resolved against wherever `sbatch` was invoked from (the
  harness's cwd) — first hit as a red herring here (a uid mismatch between
  this container's test user and the bind-mounted host directory produced
  `Could not open stdout file ...: Permission denied`, before the real
  Pyxis-related failure below could even be observed). On a real HPC stand
  this is normally satisfied automatically by shared home/project storage
  across nodes, but it's an implicit assumption worth a one-line mention in
  `harness/README.md` / `slurm/config-c/README.md` rather than something to
  discover mid-series.

With both of the above out of the way, the full `submit_job` →
`wait_for_completion` → `record_result` → `cleanup` cycle now runs cleanly
against real `sbatch`/`squeue`/`sacct` for a job that reaches `COMPLETED`
(confirmed: job ID regex parse, node resolution, `Elapsed`/`Start`/`End`
parsing, `fetch_job_metrics`'s "no-agent" fallback with no Redis present).
Separately, submitting the **unmodified real production template**
confirmed the FAILED-state → `RuntimeError` path fires correctly, and for
the expected reason — the job's own stdout shows `srun: unrecognized option
'--container-image=...'`, i.e. Pyxis/enroot genuinely absent, not some other
masked failure. That part (getting a real container payload to run under
Slurm) is out of scope for this local test — it's the partner stand's
container-runtime question already tracked in docs §8.
