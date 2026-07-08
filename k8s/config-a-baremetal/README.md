# config-a-baremetal — K8s bare-metal (default kube-scheduler vs SensitivityScore)

Базовая площадка для прямого A/B-сравнения (Программа экспериментов §3, конфигурация A).

## Использование

```bash
# default kube-scheduler (baseline):
sed 's/schedulerName: sensitivityscore/schedulerName: default-scheduler/' \
  job-low-s.yaml | kubectl apply -f -

# SensitivityScore (custom plugin), как есть:
kubectl apply -f job-low-s.yaml
kubectl apply -f job-high-s.yaml
```

На практике манифесты генерируются из шаблона харнессом (`harness/templates/job-template.yaml.j2`)
с подстановкой `profile` / `overcommit` / `job_id` — эти два файла здесь — готовые примеры
"один в один" по плану (§1.3), чтобы можно было проверить пайплайн руками без харнесса.

Для co-location (overcommit > 1.0) просто сабмитьте несколько Job с разными `job-id`
на один и тот же набор узлов — переполнение специально полагается на решение планировщика,
а не на ручное nodeSelector/affinity.
