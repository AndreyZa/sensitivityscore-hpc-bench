# config-d-slinky — Slurm-on-K8s (slurm-bridge)

Ближайший production-аналог job-level co-location, без модели интерференции
(Программа экспериментов §3.1, H4).

## Ограничения (важно!)

- Минимальные версии: **Kubernetes ≥ v1.35**, **Slurm ≥ 25.11**.
- Аллокации **только whole-node, эксклюзивные** — co-location (overcommit > 1.0)
  здесь не тестируется, только точки плана с `overcommit = 1.0`.
- Поддерживаются только DRA-драйверы `dra-driver-cpu` и `dra-example-driver` (GPU).

## Makespan — два источника

Так как slurm-bridge — это трансляция K8s Job → Slurm job-плейсхолдер, makespan
нужно мерить с обеих сторон, чтобы увидеть оверхед самой трансляции:

```bash
kubectl get pod -l scheduling.phd/job-id=D-highs-oc1.0-rep00 \
  -o jsonpath='{.items[0].status.startTime}{"\n"}{.items[0].status.containerStatuses[0].state.terminated.finishedAt}'

sacct -j <slurm_job_id> --format=Elapsed,Start,End
```

Это не часть H1–H4 напрямую, но полезно для раздела про оверхед
(см. план §4, ветка submit_job для D).
