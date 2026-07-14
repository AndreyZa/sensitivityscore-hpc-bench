# workload/ — образ Geant4 с управляемым профилем S

Один бинарник (`TestEm5` из `examples/extended/electromagnetic`), профиль чувствительности
(LLC / NUMA / Network / Disk I/O) переключается переменными окружения.

**Сборка из исходников** (нет официального готового образа `cern/geant4` на
Docker Hub — по официальному рецепту
[geant4.web.cern.ch/.../containers.html](https://geant4.web.cern.ch/documentation/dev/ig_html/InstallationGuide/containers.html),
поверх `ubuntu:22.04`). Сборка компилирует весь тулкит — займёт заметное
время (десятки минут в зависимости от машины).

## Сборка

```bash
make image-workload      # -> andreyza/geant4:11.2 (см. WORKLOAD_IMAGE в Makefile)
```

## Профили

| Переменная | Low-S | High-S |
|---|---|---|
| `G4_THREADS` | `1` | число физических ядер на NUMA-домене узла |
| `PHYSICS_LIST` | `QGSP_BERT` | `FTFP_BERT_HP` |
| `N_PRIMARIES` | `10000` | `1000000`–`10000000` |
| `OUTPUT_MODE` | `none` | `burst` (реальная запись с fsync; `ntuple` — TODO, см. ниже) |
| `RNG_SEED` | фикс. на повтор | фикс. на повтор |

## Как передаются параметры

`TestEm5`, как и все стандартные примеры Geant4, принимает **только один
позиционный аргумент — файл макроса** (флагов `-t`/`-p` нет):

- **Число потоков** — через `/run/numberOfThreads N` внутри макроса,
  обязательно **до** `/run/initialize` (PreInit-state команда).
- **Physics list** — через переменную окружения `PHYSLIST`, которую
  `G4PhysListFactory::ReferencePhysList()` читает напрямую.
- **Disk I/O** — реализовано через `OUTPUT_MODE=burst`: параллельно с compute
  каждые `IO_INTERVAL_SECONDS` пишется `IO_BURST_MB` МБ с `fsync` (fsync
  принципиален — иначе запись оседает в page cache и io.pressure не растёт).
  Это и есть дисковая ось жертвы high-s-io (`profiles.py`). Настоящий per-event
  `OUTPUT_MODE=ntuple` (UI-команды `/analysis/...`, завязанные на
  `AnalysisManager` конкретного `TestEm5`) **пока не реализован** — оставлен
  как TODO (комментарии в `entrypoint.sh`), burst выбран как честная и
  проверяемая эмуляция вместо второго слоя непроверенных предположений.

## Локальный запуск (sanity-check)

```bash
docker run --rm \
  -e G4_THREADS=1 -e PHYSICS_LIST=QGSP_BERT -e N_PRIMARIES=10000 -e OUTPUT_MODE=none \
  andreyza/geant4:11.2
```

Готовые k8s-манифесты с уже выставленными профилями и аннотациями `scheduling.phd/*` —
в `../k8s/config-a-baremetal/`.
