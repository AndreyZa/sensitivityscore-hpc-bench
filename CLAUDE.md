# Working agreements

- `git commit` no longer needs my approval: commit on your own once a
  logical unit of work is done and verified (build/tests/dry-run pass).
  Keep commits scoped and messages explanatory, as before.
- After any commit (yours or mine), `git push` the current branch to `origin`
  (`git@github.com:AndreyZa/sensitivityscore-hpc-bench.git`) automatically,
  without asking first. Unconditional — applies to every commit, regardless
  of what it touches.
- Rebuilding + `docker push`-ing an image is **conditional**: only do it when
  the commit actually touches that image's inputs, not on every commit.
  - `metrics-agent/**` changed → `make image-metrics-agent` then
    `docker push andreyza/metrics-agent:dev`.
  - `workload/**` changed → `make image-workload` then
    `docker push andreyza/geant4:11.2` (check `WORKLOAD_IMAGE` in `Makefile`
    for the current tag).
  - `harness/**` changed → `make image-harness` then
    `docker push andreyza/harness:dev` — the in-cluster harness Job
    (harness/deploy/job-*.yaml) pulls this image; without the rebuild it
    silently runs stale code. (A host-side `python run_experiment.py` run
    doesn't need the image.)
  - `aggressor/**` changed → `make image-aggressor` then
    `docker push andreyza/aggressor:dev` (pressure-scenario stress pods).
  - The scheduler plugin image is built from the **separate**
    `scheduler-plugins` repo (`pkg/sensitivityscore/**`, not anything under
    `k8s/` here) — see that repo's own `CLAUDE.md`. A commit here touching
    only `k8s/scheduler-config/*.yaml` is a manifest change (`kubectl apply`
    territory), not an image rebuild.
  - A commit touching only `docs/`, `analysis/`, or other non-image paths →
    git push only, no image rebuild, no docker push.
- This repo's cluster uses `imagePullPolicy: Always`, so a local rebuild is enough for the local dev cluster to pick it up — the Docker Hub push is for durability/sharing (e.g. a partner stand pulling the same tag), not a local-dev requirement.
