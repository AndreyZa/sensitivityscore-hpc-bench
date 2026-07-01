# metrics-agent — perf_event_open() → Redis

DaemonSet-агент (Go), один под на узел, замещает JSON-кэш метрик в MVP. Пишет два
семейства ключей в Redis: `node:metrics:*` (TTL 30s, для рантайм-скоринга
планировщиком) и `job:metrics:*` (без TTL, полная история job для анализа,
docs §3.2).

```
pkg/perf/        — perf_event_open() обёртка: LLC misses/references, cgroup-scoped
pkg/cgroup/       — io.stat (Disk I/O); net.go — заглушка под eBPF socket hook
pkg/redisclient/  — запись node:metrics:* / job:metrics:*
pkg/vpmu/         — health-check vPMU-доступности для серии B (§3.3)
cmd/agent/        — цикл сэмплирования: discover pods on node -> sample -> write
```

## Реализовано

- LLC miss rate через честные PMU-счётчики (`PERF_COUNT_HW_CACHE_MISSES` /
  `_REFERENCES`, cgroup-scoped).
- Disk I/O через cgroup v2 `io.stat`.
- Redis writer с правильным разделением `node:metrics:*` (TTL) / `job:metrics:*`
  (без TTL, для последующей выгрузки харнессом в Parquet).
- vPMU health-check (in-guest + `virsh capabilities`) для конфигурации B.

## Сознательно оставлено как TODO (см. комментарии в коде)

- **NUMA bandwidth** (`pkg/perf/perf_event.go: ReadUncoreNUMABandwidth`) — требует
  определения модели PMU хоста (CPUID) и выбора нужного `uncore_imc_*` устройства;
  специфично для железа стенда, не угадывается заранее.
- **Network bytes** (`pkg/cgroup/net.go: ReadNetStats`) — cgroup v2 не имеет
  встроенного network-байт-каунтера; нужен отдельный eBPF `cgroup_skb` hook
  (через `cilium/ebpf`), это отдельная единица работы, требующая подтверждения
  версии ядра/BTF на стенде партнёров (см. открытый вопрос в Программе
  экспериментов §8).
- `resolvePodCgroupPath` (`cmd/agent/pods.go`) реализует системный (`systemd`)
  cgroup driver layout — если на стенде используется `cgroupfs`, путь нужно
  поправить.

## Сборка

```bash
go build ./...
docker build -t sensitivityscore-bench/metrics-agent:dev .
kubectl apply -f deploy/daemonset.yaml
```
