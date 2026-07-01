# scheduler-plugin — SensitivityScore

Kubernetes Scheduler Framework plugin (Score extension point), реализующий
формальную модель Z = {G, R, S} (docs §2.1) как скалярное произведение профиля
чувствительности job (`S_job`, из аннотаций `scheduling.phd/sensitivity-*`) и
текущего давления узла (`PressureVector`, из Redis, наполняется `metrics-agent/`).

```
pkg/types/      — формальные типы: SensitivityVector, PressureVector, Weights
pkg/resolver/   — NodeStateResolver: PodCgroupResolver (A) / QemuProcessResolver (B)
pkg/metrics/    — Redis-backed чтение node:metrics:* с TTL
pkg/plugin/     — Score(), dotProduct(), normalize(), hot-reload весов (fsnotify)
cmd/scheduler/  — entrypoint, регистрация плагина во втором scheduler-профиле
```

## Сборка и запуск

```bash
go build ./...
docker build -t sensitivityscore-bench/scheduler:dev .
```

Развернуть как второй scheduler-процесс (Deployment) с
`k8s/scheduler-config/scheduler-config.yaml`, смонтированным как `--config`, и
`k8s/scheduler-config/weights-configmap.yaml`, смонтированным в путь из
`weightsConfigPath`.

## Абляция (Глава 3)

Изменить веса прямо в ConfigMap (`kubectl edit configmap sensitivityscore-weights`)
— плагин подхватит изменения через fsnotify без рестарта пода планировщика. Готовые
пресеты (`no-numa`, `llc-only`, `numa-only`) — в комментариях
`k8s/scheduler-config/weights-configmap.yaml`.

## Что здесь намеренно оставлено как заглушка

- `qemu-process` resolver в `New()` (`cmd/scheduler/main.go`) требует явной wiring
  `KubeVirtPIDLookup` под конкретный стенд (KubeVirt API endpoint, формат
  virt-launcher подов) — это инфраструктурно-специфичная часть, общий код в
  `pkg/resolver/qemu_process_resolver.go` уже реализован, не хватает только
  адаптера к реальному кластеру.
- `go.sum` не закоммичен — сгенерируйте `go mod tidy` после `go mod download`
  под версии зависимостей вашего кластера (k8s.io/kubernetes должен совпадать с
  версией control-plane стенда).
