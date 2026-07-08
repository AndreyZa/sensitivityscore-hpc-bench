# scheduler-plugin — перенесено в отдельный форк

Разработка самого плагина SensitivityScore больше не ведётся в этом
репозитории — исходный standalone Go-модуль здесь тянул `k8s.io/kubernetes`
напрямую, что упирается в известную проблему с `replace`-директивами
(`k8s.io/api@v0.0.0: unknown revision` и т.п. — см. обсуждение в истории
разработки). Вместо повторения этой проблемы разработка плагина перенесена в
форк [`kubernetes-sigs/scheduler-plugins`](https://github.com/kubernetes-sigs/scheduler-plugins),
у которого уже есть рабочий `go.mod` с нужным `replace`-блоком.

## Где теперь что

- **Код плагина**: `pkg/sensitivityscore/` в форке `scheduler-plugins`
  (не в этом репозитории).
- **Сборка плагина**: только в форке, через `make -f sensitivityscore.mk ss-image`
  — единственный результат, который форк отдаёт наружу, это Docker-образ.
- **Всё остальное** (деплой этого образа, ConfigMap с метриками/весами,
  харнесс экспериментов, анализ результатов) — здесь, в этом репозитории.
  См. корневой `Makefile`, секцию "Scheduler plugin (образ из форка)".

## Как это стыкуется

```bash
# В форке (один раз или после правки sensitivityscore.go):
cd ../scheduler-plugins
make -f sensitivityscore.mk ss-image

# Здесь (в этом репозитории):
cd sensitivityscore-hpc-bench
make scheduler-plugin-image   # шеллится в форк, дёргает ss-image там
make scheduler-deploy         # деплоит уже готовый образ + ConfigMap-ы
```

Переменная `SCHEDULER_PLUGINS_DIR` в корневом `Makefile` (по умолчанию
`../scheduler-plugins`, т.е. соседняя папка) задаёт путь до форка.
