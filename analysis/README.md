# analysis — пайплайн анализа данных (§5, проверка H1–H4)

```
load.py     — чтение results.parquet (схема §5.1), базовая валидация
stats.py    — Mann-Whitney U, Cliff's delta, coefficient of variation (§5.2)
plots.py    — boxplot makespan, scatter LLC vs makespan (§5.3)
analyze.py  — главный скрипт: гоняет всё выше по данным harness, пишет report/
```

## Установка

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Запуск

```bash
python analyze.py --results ../harness/results/results.parquet --outdir report/
```

Результат в `report/`:

- `summary.md` — человекочитаемая сводка по H1–H4 (для брифинга научруку),
  Mann-Whitney p-value + Cliff's delta + CV на каждую точку плана.
- `comparisons.csv` — все сравнения одной таблицей (для дальнейшей обработки).
- `makespan_boxplot.png` — boxplot по `config × profile` при `overcommit=2.0`.
- `llc_vs_makespan.png` — scatter LLC miss rate vs makespan.

## Пары конфигураций по гипотезам

| Гипотеза | Сравнение | Что проверяет |
|---|---|---|
| H1 | `A-sensitivityscore` vs `A-default` | SensitivityScore vs default на bare-metal |
| H2 | `B-sensitivityscore` vs `B-default` | то же самое, но под KubeVirt (аддитивность оверхеда) |
| H3 | `A-sensitivityscore` vs `C` | SensitivityScore vs верхняя граница (чистый Slurm) |
| H4 | `A-sensitivityscore` vs `D` | SensitivityScore vs Slinky/slurm-bridge |

## Проверено на синтетических данных

Пайплайн прогнан end-to-end на сгенерированных данных (не входят в репозиторий) —
`analyze.py` корректно строит все сравнения, пишет `summary.md`/`comparisons.csv`
и оба графика без ошибок. Реальные данные появятся здесь после первого прогона
харнесса (`../harness/`).

## notebooks/

Пустой каталог-заготовка для последующих ad-hoc Jupyter-разборов (например,
детальный разбор отдельной аномальной точки плана) — не часть основного
пайплайна.
