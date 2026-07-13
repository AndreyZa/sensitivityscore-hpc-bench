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

Цикл замкнут в коде: агент (`metrics-agent/cmd/agent/main.go:netReferenceFromEnv`,
`pkg/redisclient/writer.go`) читает `NET_REFERENCE_MBPS` из env и пишет
`net_pressure` в Redis, `redis_source.go` подаёт его в score. Так что после
`netcheck` остаётся один шаг — выставить измеренное число на DaemonSet:

```bash
make netcheck-apply NET_REFERENCE_MBPS=<N>   # перезапускает агент — делать МЕЖДУ сериями
```

`make netcheck-disable` (или не выставлять вовсе) = ось честно выключена
(`net_pressure=0`), сырой `net_bw` всё равно пишется для анализа.

## Наблюдение со STAGE-стенда (пример вывода)

Первый живой прогон на 3-нодовом стенде (worker 192.168.0.5/6/7, dedicated-
серверы) показал, зачем эта калибровка вообще нужна — пропускная способность
**неоднородна** по парам нод (все линки чистые, ретрансмиты 14–16):

| Пара (client ↔ server) | Агрегат rx+tx |
|---|---|
| 0.5 ↔ 0.6 | 1335 Мбит/с |
| 0.5 ↔ 0.7 | 1200 Мбит/с |
| 0.6 ↔ 0.7 | 900 Мбит/с |

Разброс ~1.5× — прямой аргумент к вопросу «один референс или per-nodegroup»
(docs §8): на неоднородном стенде честнее брать консервативный минимум
(здесь ~900) как единый референс, либо калибровать попарно. Номинал NIC
(«1 Гбит/с») тут не сказал бы ничего — реальные числа и ниже (потолок),
и разные между парами.
