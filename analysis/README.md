# analysis — пайплайн анализа данных (§5, проверка H1–H4)

```
load.py      — чтение results.parquet (схема §5.1) + slowdown из baselines
stats.py     — Mann-Whitney U, Cliff's delta, coefficient of variation (§5.2)
fingerprint.py — таблица «заявленный vs измеренный S» + проверка монотонности
plots.py     — boxplot makespan, scatter метрик vs makespan, regret по плечам (§5.3)
analyze.py   — главный скрипт: гоняет всё выше по данным harness, пишет report/
```

## Установка

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Запуск

```bash
python analyze.py --results ../harness/results/results.parquet --outdir report/
# с бейзлайнами (добавляет slowdown-сравнения и fingerprint-таблицу):
python analyze.py --results ... --baselines ../harness/results/baselines.parquet
```

`--baselines` опционален: если файл есть (харнесс `--baseline`), добавляются
slowdown и fingerprint; если нет — пайплайн работает как раньше (makespan +
regret), напечатав предупреждение.

### Метрики сравнения

Каждая точка плана сравнивается по трём метрикам (по всем «меньше = лучше»),
каждая — своя Holm-семья в пределах сценария:

- `makespan_s` — сырое время исполнения (всегда);
- `slowdown = makespan_s / медиана изолированного` (при `--baselines`) —
  безразмерная, профили разной длительности становятся объединяемыми;
- `placement_regret` — качество решения планировщика по снапшоту давления на
  момент сабмита (прямое свидетельство механизма, почти без шума исхода).

Результат в `report/`:

- `summary.md` — сводка по H1–H4 (для брифинга научруку): на каждую метрику ×
  точку плана Mann-Whitney p (+ Holm-adjusted) + Cliff's delta + CV; плюс
  fingerprint-таблица (при `--baselines`).
- `comparisons.csv` — все сравнения одной таблицей (колонки `scenario`,
  `metric`).
- `fingerprint.csv` — заявленный vs измеренный S по соло-прогонам.
- `makespan_boxplot.png` — boxplot по `config × profile` при `overcommit=2.0`.
- `{llc,io_pressure,numa}_vs_makespan.png` — scatter метрики vs makespan.
- `placement_regret-<scenario>.png` — regret по плечам (график механизма).
- `cv_comparison-<scenario>.png` — стабильность (CV) для H1.

## Пары конфигураций по гипотезам

| Гипотеза | Сравнение | Что проверяет |
|---|---|---|
| H1 | `A-sensitivityscore` vs `A-default` | SensitivityScore vs default на bare-metal |
| H1-trimaran | `A-sensitivityscore` vs `A-trimaran` | interference-aware vs load-aware (не любой учёт загрузки) |
| H2 | `B-sensitivityscore` vs `B-default` | то же самое, но под KubeVirt (аддитивность оверхеда) |
| H3 | `A-sensitivityscore` vs `C` | SensitivityScore vs верхняя граница (чистый Slurm) |
| H4 | `A-sensitivityscore` vs `D` | SensitivityScore vs Slinky/slurm-bridge |

H1-trimaran появляется в отчёте, только если плечо `A-trimaran` включено в
`scheduler_variants` харнесса; иначе секция пишет «no data yet».

## Проверено на синтетических данных

Пайплайн прогнан end-to-end на сгенерированных данных (не входят в репозиторий) —
`analyze.py` корректно строит все сравнения, пишет `summary.md`/`comparisons.csv`
и оба графика без ошибок. Реальные данные появятся здесь после первого прогона
харнесса (`../harness/`).

## notebooks/

Пустой каталог-заготовка для последующих ad-hoc Jupyter-разборов (например,
детальный разбор отдельной аномальной точки плана) — не часть основного
пайплайна.
