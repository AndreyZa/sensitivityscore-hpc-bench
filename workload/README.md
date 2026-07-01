# workload/ — образ Geant4 с управляемым профилем S

Один бинарник (`TestEm5` из `examples/extended/electromagnetic`), профиль чувствительности
(LLC / NUMA / Network / Disk I/O) переключается переменными окружения.

## Сборка

```bash
docker build -t sensitivityscore-bench/geant4:11.2 ./workload
```

## Профили

| Переменная | Low-S | High-S |
|---|---|---|
| `G4_THREADS` | `1` | число физических ядер на NUMA-домене узла |
| `PHYSICS_LIST` | `QGSP_BERT` | `FTFP_BERT_HP` |
| `N_PRIMARIES` | `10000` | `1000000`–`10000000` |
| `OUTPUT_MODE` | `none` | `ntuple` |
| `RNG_SEED` | фикс. на повтор | фикс. на повтор |

## Локальный запуск (sanity-check)

```bash
docker run --rm \
  -e G4_THREADS=1 -e PHYSICS_LIST=QGSP_BERT -e N_PRIMARIES=10000 -e OUTPUT_MODE=none \
  sensitivityscore-bench/geant4:11.2
```

Готовые k8s-манифесты с уже выставленными профилями и аннотациями `scheduling.phd/*` —
в `../k8s/config-a-baremetal/`.
