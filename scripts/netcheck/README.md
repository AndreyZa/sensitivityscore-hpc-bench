# netcheck — калибровка Net-измерения (Этап 0)

Меряет реально достижимую пропускную способность pod-to-pod **между двумя
разными worker-нодами** (через физический uplink NIC + CNI-оверлей) и печатает
`NET_REFERENCE_MBPS` — референс, которым `metrics-agent` будет нормировать
сырой `net_bw` в `net_pressure` (в score). Полное обоснование методики —
`docs/Технический план экспериментов.md` §3.4.

Симметрично `perfcheck` — часть Этапа 0, до/во время разворачивания
`metrics-agent`.

## Запуск

```bash
make netcheck-run     # iperf3 server+client на двух разных worker-нодах
make netcheck-logs    # дождаться завершения клиента → печатает NET_REFERENCE_MBPS=<N>
make netcheck-clean   # убрать поды + Service
```

Ноды выбираются автоматически (первые две worker-ноды по сортировке имён,
control-plane исключается). Явный выбор:

```bash
make netcheck-run NODE_CLIENT=worker-a NODE_SERVER=worker-b
```

Образ iperf3 — стоковый `networkstatic/iperf3` (не наш). Если стенд не тянет
из Docker Hub: `make netcheck-run IPERF_IMAGE=<свой-mirror>/iperf3`.

## Почему cross-node, а не на одной ноде

Same-node pod-to-pod трафик CNI мостит локально через veth — он не касается
физического NIC, намеряется скорость памяти/моста (десятки Гбит/с). Дефицитный
ресурс, за который поды реально конкурируют, — это физический uplink ноды,
общий когда поды шлют трафик за её пределы. Его и меряет cross-node тест между
двумя нодами. Померив на одной ноде, получили бы завышенный потолок, при
котором `net_pressure` всегда ≈ 0.

## Что здесь есть и чего нет

- `netcheck.yaml` — Service + iperf3 server-под + client-под (nodeName-пин
  обоих на выбранные ноды, `--bidir`, retry до готовности сервера).
- `parse.py` — `iperf3 --json` (stdin) → `NET_REFERENCE_MBPS` (stdout).
  Stdlib-only, покрывает и top-level `sum_sent/sum_received`, и per-stream
  fallback; распечатывает понятную ошибку на пустой ввод / iperf3-error.

Это только измерительная половина. Код, который потребляет
`NET_REFERENCE_MBPS` (нормировка `net_pressure` в агенте + чтение в
`redis_source.go`), — пока TODO (см. корневой README, «Статус реализации»).
