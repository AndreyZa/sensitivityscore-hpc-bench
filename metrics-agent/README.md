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
pkg/promexport/   — зеркало узловых осей в Prometheus (/metrics:9101)
cmd/agent/        — цикл сэмплирования: discover pods on node -> sample -> write
```

## Реализовано

- LLC-давление узла через честные PMU-счётчики (`PERF_COUNT_HW_CACHE_MISSES`,
  cgroup-scoped, по счётчику на online-CPU): `llc_miss_rate = misses/s /
  LLC_REFERENCE_MISSES_PER_SEC` при калибровке стенда. Без калибровки — сырой
  ratio misses/references, но он инвертируется под потоковой нагрузкой (шторм
  выглядит «чище» простоя), поэтому эталон меряется в misses/s под 2×stress-ng
  --stream; сырой `llc_misses_per_sec` пишется всегда. На поде — ratio.
- Disk I/O через cgroup v2 `io.stat` + PSI `io.pressure`.
- Network bytes (`net.go: ReadNetStats` из `/proc/<pid>/net/dev`, rx+tx) →
  `net_pressure = net_bw / NET_REFERENCE_MBPS` при калибровке стенда (иначе
  сырой `net_bw` пишется, а ось выключена). См. `make netcheck-run`.
- Redis writer с правильным разделением `node:metrics:*` (TTL) / `job:metrics:*`
  (без TTL, для последующей выгрузки харнессом в Parquet).
- vPMU health-check (in-guest + `virsh capabilities`) для конфигурации B.
- **Prometheus-эндпоинт** `:9101/metrics` (`pkg/promexport`) — те же узловые оси,
  что уходят в Redis, плюс метрики годности сбора
  (`ss_agent_pmu_hardware_available`, `ss_agent_llc_calibrated`,
  `ss_agent_net_calibrated`, `ss_agent_psi_available`,
  `ss_agent_last_sample_timestamp_seconds`). **Redis остаётся авторитетным**:
  горячий путь планировщика и выгрузка харнесса читают только его, Prometheus —
  read-only наблюдаемость, и падение HTTP-сервера не влияет на сэмплирование.
  Дашборды и scrape-конфиг — `k8s/monitoring/`.

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
