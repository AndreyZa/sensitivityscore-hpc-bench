# metrics-agent — perf_event_open() → Redis

DaemonSet-агент (Go), один под на узел, замещает JSON-кэш метрик в MVP. Пишет два
семейства ключей в Redis: `node:metrics:*` (TTL 30s, для рантайм-скоринга
планировщиком) и `job:metrics:*` (без TTL, полная история job для анализа,
docs §3.2).

```
pkg/perf/        — perf_event_open() обёртка: LLC misses/references, cgroup-scoped
pkg/cgroup/       — io.stat (Disk I/O); net.go — rx+tx байты из /proc/<pid>/net/dev
pkg/redisclient/  — запись node:metrics:* / job:metrics:*
pkg/vpmu/         — health-check vPMU-доступности для серии B (§3.3)
cmd/agent/        — цикл сэмплирования: discover pods on node -> sample -> write
```

## Реализовано

- LLC miss rate через честные PMU-счётчики (`PERF_COUNT_HW_CACHE_MISSES` /
  `_REFERENCES`, cgroup-scoped, по счётчику на online-CPU).
- Disk I/O через cgroup v2 `io.stat` + PSI `io.pressure`.
- Network bytes (`net.go: ReadNetStats` из `/proc/<pid>/net/dev`, rx+tx) →
  `net_pressure = net_bw / NET_REFERENCE_MBPS` при калибровке стенда (иначе
  сырой `net_bw` пишется, а ось выключена). См. `make netcheck-run`.
- Redis writer с правильным разделением `node:metrics:*` (TTL) / `job:metrics:*`
  (без TTL, для последующей выгрузки харнессом в Parquet).
- vPMU health-check (in-guest + `virsh capabilities`) для конфигурации B.

## Сознательно оставлено как TODO (см. комментарии в коде)

- **NUMA bandwidth** (`pkg/perf/perf_event.go: ReadUncoreNUMABandwidth`) — требует
  определения модели PMU хоста (CPUID) и выбора нужного `uncore_imc_*` устройства;
  специфично для железа стенда, не угадывается заранее.
- **NUMA remote ratio точнее через uncore-IMC** — сейчас NUMA считается
  переносимо как `node-load-misses / node-loads` (generic node-события PMU),
  этого достаточно; истинный per-node bandwidth (`uncore_imc_*`) — см. пункт
  выше про `ReadUncoreNUMABandwidth`. На односокетных узлах ось вырождена
  (1 NUMA-домен → `node_load_misses` = ENOENT, `numa_remote_ratio` ≡ 0).
- `resolvePodCgroupPath` (`cmd/agent/pods.go`) реализует системный (`systemd`)
  cgroup driver layout — если на стенде используется `cgroupfs`, путь нужно
  поправить.

## Сборка

```bash
go build ./...                      # или: make -C .. build-go
make -C .. image-metrics-agent      # -> andreyza/metrics-agent:dev
make -C .. deploy-metrics-agent     # kubectl apply deploy/daemonset.yaml
```
