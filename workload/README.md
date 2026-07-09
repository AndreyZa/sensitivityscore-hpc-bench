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
docker build -t sensitivityscore-bench/geant4:11.2 ./workload
```

## Профили

| Переменная | Low-S | High-S |
|---|---|---|
| `G4_THREADS` | `1` | число физических ядер на NUMA-домене узла |
| `PHYSICS_LIST` | `QGSP_BERT` | `FTFP_BERT_HP` |
| `N_PRIMARIES` | `10000` | `1000000`–`10000000` |
| `OUTPUT_MODE` | `none` | `ntuple` (⚠ пока не реализовано, см. ниже) |
| `RNG_SEED` | фикс. на повтор | фикс. на повтор |

## Как реально передаются параметры (важно)

`TestEm5`, как и все стандартные примеры Geant4, принимает **только один
позиционный аргумент — файл макроса**. Никаких `-t`/`-p` флагов не
существует (это было неверным предположением в первой версии образа):

- **Число потоков** — через `/run/numberOfThreads N` внутри макроса,
  обязательно **до** `/run/initialize` (PreInit-state команда).
- **Physics list** — через переменную окружения `PHYSLIST`, которую
  `G4PhysListFactory::ReferencePhysList()` читает напрямую (это
  подтверждённый механизм в исходниках Geant4, не выдумка).
- **Disk I/O (OUTPUT_MODE=ntuple)** — **пока не реализовано**. Нужны
  UI-команды `/analysis/...` для настройки per-event ntuple-вывода, которые
  зависят от конкретной настройки `AnalysisManager` в `TestEm5` этой версии
  — оставлено как явный TODO (см. комментарии в `entrypoint.sh`/`macros/run.mac`),
  чтобы не городить второй слой непроверенных предположений поверх первого.

## Локальный запуск (sanity-check)

```bash
docker run --rm \
  -e G4_THREADS=1 -e PHYSICS_LIST=QGSP_BERT -e N_PRIMARIES=10000 -e OUTPUT_MODE=none \
  sensitivityscore-bench/geant4:11.2
```

Готовые k8s-манифесты с уже выставленными профилями и аннотациями `scheduling.phd/*` —
в `../k8s/config-a-baremetal/`.
