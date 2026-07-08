# Makefile — sensitivityscore-hpc-bench
#
# Единая точка входа для всего экспериментального стенда, КРОМЕ сборки
# самого кода scheduler-плагина — та живёт в отдельном форке
# kubernetes-sigs/scheduler-plugins (см. scheduler-plugin/README.md) и
# производит ровно один артефакт: Docker-образ. Здесь мы этот образ
# собираем (шеллимся в форк), деплоим, конфигурируем, гоняем нагрузку и
# анализируем результаты.
#
# `make help` — список всех команд.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Переменные (переопределяются через `make VAR=значение ...`).
# ---------------------------------------------------------------------------

# --- Форк scheduler-plugins (соседняя папка по умолчанию) ---
SCHEDULER_PLUGINS_DIR ?= ../scheduler-plugins

# --- Docker-образы ---
REGISTRY                ?= andreyza
SCHEDULER_RELEASE_VER   ?= v20260709-55642cf2
WORKLOAD_IMAGE          ?= $(REGISTRY)/geant4:11.2
SCHEDULER_IMAGE         ?= $(REGISTRY)/sensitivityscore:$(SCHEDULER_RELEASE_VER)
METRICS_AGENT_IMAGE     ?= $(REGISTRY)/metrics-agent:dev

# --- Kubernetes ---
KUBECTL      ?= kubectl
NAMESPACE    ?= sensitivityscore-system
KIND_CLUSTER ?= sensitivityscore-dev
SCHEDULER_DEPLOYMENT ?= sensitivityscore-scheduler

# --- Python venvs ---
PYTHON        ?= python3
HARNESS_VENV  ?= harness/.venv
ANALYSIS_VENV ?= analysis/.venv

# --- Harness / analysis output paths ---
RESULTS_FILE ?= harness/results/results.parquet
REPORT_DIR   ?= analysis/report

# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Показать этот список команд
	@echo "sensitivityscore-hpc-bench — доступные команды:"
	@grep -h -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Go: metrics-agent/ (единственный оставшийся Go-модуль в этом репозитории)
# ---------------------------------------------------------------------------

.PHONY: fmt-go
fmt-go: ## gofmt -w по metrics-agent
	gofmt -l -w metrics-agent

.PHONY: vet-go
vet-go: ## go vet по metrics-agent
	cd metrics-agent && go vet ./...

.PHONY: build-go
build-go: fmt-go vet-go ## Собрать metrics-agent локально
	cd metrics-agent && go build ./...

.PHONY: test-go
test-go: ## Юнит-тесты metrics-agent
	cd metrics-agent && go test -v -count=1 ./...

# ---------------------------------------------------------------------------
# Docker-образы (workload + metrics-agent; scheduler — см. секцию выше)
# ---------------------------------------------------------------------------

.PHONY: image-workload
image-workload: ## Собрать образ нагрузки Geant4 (workload/)
	docker build -t $(WORKLOAD_IMAGE) ./workload

.PHONY: image-metrics-agent
image-metrics-agent: build-go ## Собрать образ metrics-agent
	docker build -t $(METRICS_AGENT_IMAGE) ./metrics-agent

# ---------------------------------------------------------------------------
# Кластер: bootstrap (namespace + Redis), деплой планировщика, деплой агента
# ---------------------------------------------------------------------------

.PHONY: bootstrap
bootstrap: ## namespace + Redis (для будущего metrics-agent, см. scripts/bootstrap-cluster.sh)
	./scripts/bootstrap-cluster.sh

.PHONY: scheduler-apply-config
scheduler-apply-config: ## Применить ConfigMap-ы плагина (sensitivity-config + scheduler-config)
	$(KUBECTL) create namespace $(NAMESPACE) --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) create configmap scheduler-config \
		--from-file=k8s/scheduler-config/scheduler-config.yaml \
		-n $(NAMESPACE) --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) apply -f k8s/scheduler-config/sensitivity-configmap.yaml

.PHONY: scheduler-deploy
scheduler-deploy: scheduler-apply-config ## Развернуть second scheduler (Deployment) с текущим значением SCHEDULER_IMAGE
	$(KUBECTL) apply -f k8s/scheduler-config/deployment.yaml
	$(KUBECTL) set image deployment/$(SCHEDULER_DEPLOYMENT) \
		$(SCHEDULER_DEPLOYMENT)=$(SCHEDULER_IMAGE) -n $(NAMESPACE)

.PHONY: scheduler-redeploy
scheduler-redeploy: scheduler-deploy ## Полный цикл: пересобрать образ плагина -> передеплоить -> дождаться rollout (образ берётся локально, без kind load — см. примечание у SCHEDULER_IMAGE)
	$(KUBECTL) rollout restart deployment/$(SCHEDULER_DEPLOYMENT) -n $(NAMESPACE)
	$(KUBECTL) rollout status deployment/$(SCHEDULER_DEPLOYMENT) -n $(NAMESPACE) --timeout=120s

.PHONY: scheduler-undeploy
scheduler-undeploy: ## Убрать Deployment планировщика и его ConfigMap-ы
	$(KUBECTL) delete -f k8s/scheduler-config/deployment.yaml --ignore-not-found
	$(KUBECTL) delete configmap scheduler-config -n $(NAMESPACE) --ignore-not-found
	$(KUBECTL) delete -f k8s/scheduler-config/sensitivity-configmap.yaml --ignore-not-found

.PHONY: deploy-metrics-agent
deploy-metrics-agent: ## Развернуть DaemonSet metrics-agent
	$(KUBECTL) apply -f metrics-agent/deploy/daemonset.yaml

.PHONY: setup-cluster
setup-cluster: bootstrap images kind-load scheduler-deploy deploy-metrics-agent ## Полная подготовка кластера с нуля (kind должен быть уже поднят)

# ---------------------------------------------------------------------------
# Отладка планировщика: логи, статус, правка метрик/весов "на лету"
# ---------------------------------------------------------------------------

.PHONY: scheduler-logs
scheduler-logs: ## Логи scheduler-пода, отфильтрованные по SensitivityScore (Ctrl+C для выхода)
	$(KUBECTL) logs -n $(NAMESPACE) deploy/$(SCHEDULER_DEPLOYMENT) -f | grep --line-buffered SensitivityScore

.PHONY: scheduler-logs-raw
scheduler-logs-raw: ## Логи scheduler-пода целиком, без фильтра
	$(KUBECTL) logs -n $(NAMESPACE) deploy/$(SCHEDULER_DEPLOYMENT) -f

.PHONY: scheduler-status
scheduler-status: ## Быстрый обзор: под планировщика, ConfigMap-ы, последние scheduling events
	@echo "--- pods ---"
	@$(KUBECTL) get pods -n $(NAMESPACE) -l component=scheduler
	@echo "--- configmaps ---"
	@$(KUBECTL) get configmap -n $(NAMESPACE)
	@echo "--- last 10 scheduling events ---"
	@$(KUBECTL) get events --field-selector reason=Scheduled --sort-by=.lastTimestamp | tail -10

.PHONY: weights-edit
weights-edit: ## Отредактировать веса измерений S "на лету" (плагин перечитает файл сам, без рестарта)
	$(KUBECTL) edit configmap sensitivity-config -n $(NAMESPACE)

.PHONY: metrics-edit
metrics-edit: ## Отредактировать node-metrics.json "на лету" (ручная имитация метрик, до подключения metrics-agent)
	$(KUBECTL) edit configmap sensitivity-config -n $(NAMESPACE)

# ---------------------------------------------------------------------------
# Smoke-test планировщика — простой под без Geant4 (Фаза 2 плана)
# ---------------------------------------------------------------------------

.PHONY: test-pod-highs
test-pod-highs: ## Засабмитить простой high-S под (busybox+sleep) — быстрая проверка, что SensitivityScore вообще работает
	$(KUBECTL) apply -f k8s/smoke-test/test-pod-highs.yaml
	@$(KUBECTL) get pod test-highs -o jsonpath='{.spec.nodeName}{"\n"}'   

.PHONY: test-pod-lows
test-pod-lows: ## Засабмитить симметричный low-S под — для сравнения score с test-pod-highs
	$(KUBECTL) apply -f k8s/smoke-test/test-pod-lows.yaml
	@$(KUBECTL) get pod test-lows -o jsonpath='{.spec.nodeName}{"\n"}'    

.PHONY: test-pod-clean
test-pod-clean: ## Убрать оба smoke-test пода
	$(KUBECTL) delete -f k8s/smoke-test/test-pod-highs.yaml --ignore-not-found
	$(KUBECTL) delete -f k8s/smoke-test/test-pod-lows.yaml --ignore-not-found

# ---------------------------------------------------------------------------
# Конфигурация A (K8s bare-metal) — ручной smoke-test без харнесса
# ---------------------------------------------------------------------------

.PHONY: submit-job-low-s
submit-job-low-s: ## Засабмитить low-S Job (config A) напрямую, в обход харнесса
	$(KUBECTL) apply -f k8s/config-a-baremetal/job-low-s.yaml

.PHONY: submit-job-high-s
submit-job-high-s: ## Засабмитить high-S Job (config A) напрямую, в обход харнесса
	$(KUBECTL) apply -f k8s/config-a-baremetal/job-high-s.yaml

.PHONY: clean-jobs
clean-jobs: ## Удалить все Job с меткой app=geant4-bench (после ручных прогонов)
	$(KUBECTL) delete jobs -l app=geant4-bench --ignore-not-found

# ---------------------------------------------------------------------------
# Конфигурация C (Slurm) — ручной smoke-test
# ---------------------------------------------------------------------------

.PHONY: submit-slurm-low-s
submit-slurm-low-s: ## sbatch low-S скрипт (config C) напрямую
	cd slurm/config-c && sbatch geant4-low-s.sbatch

.PHONY: submit-slurm-high-s
submit-slurm-high-s: ## sbatch high-S скрипт (config C) напрямую
	cd slurm/config-c && sbatch geant4-high-s.sbatch

# ---------------------------------------------------------------------------
# Python venv (harness + analysis — раздельные окружения, разные requirements)
# ---------------------------------------------------------------------------

$(HARNESS_VENV)/bin/activate: harness/requirements.txt
	$(PYTHON) -m venv $(HARNESS_VENV)
	$(HARNESS_VENV)/bin/pip install --quiet --upgrade pip
	$(HARNESS_VENV)/bin/pip install --quiet -r harness/requirements.txt
	touch $(HARNESS_VENV)/bin/activate

.PHONY: venv-harness
venv-harness: $(HARNESS_VENV)/bin/activate ## Создать/обновить venv для harness/

$(ANALYSIS_VENV)/bin/activate: analysis/requirements.txt
	$(PYTHON) -m venv $(ANALYSIS_VENV)
	$(ANALYSIS_VENV)/bin/pip install --quiet --upgrade pip
	$(ANALYSIS_VENV)/bin/pip install --quiet -r analysis/requirements.txt
	touch $(ANALYSIS_VENV)/bin/activate

.PHONY: venv-analysis
venv-analysis: $(ANALYSIS_VENV)/bin/activate ## Создать/обновить venv для analysis/

# ---------------------------------------------------------------------------
# Harness — оркестрация серий экспериментов (§4 плана)
# ---------------------------------------------------------------------------

.PHONY: plan-dry-run
plan-dry-run: venv-harness ## Построить полный план и распечатать его, ничего не запуская
	cd harness && ../$(HARNESS_VENV)/bin/python run_experiment.py --dry-run

.PHONY: pilot
pilot: venv-harness ## Пилотная серия: 1 точка плана (high-s, oc=2.0), 3 повтора, только config A
	cd harness && ../$(HARNESS_VENV)/bin/python run_experiment.py --pilot

.PHONY: run-config-a
run-config-a: venv-harness ## Полная матрица только для конфигурации A
	cd harness && ../$(HARNESS_VENV)/bin/python run_experiment.py --configs A

.PHONY: run-all
run-all: venv-harness ## Полная матрица по всем конфигурациям из harness/config.yaml
	cd harness && ../$(HARNESS_VENV)/bin/python run_experiment.py

# ---------------------------------------------------------------------------
# Analysis — статистика и графики (§5 плана)
# ---------------------------------------------------------------------------

.PHONY: analyze
analyze: venv-analysis ## Прогнать H1-H4 анализ по текущим результатам харнесса
	cd analysis && ../$(ANALYSIS_VENV)/bin/python analyze.py \
		--results ../$(RESULTS_FILE) --outdir report

.PHONY: report
report: analyze ## analyze + сразу открыть summary.md (macOS/Linux `open`/`xdg-open`)
	@command -v xdg-open >/dev/null && xdg-open $(REPORT_DIR)/summary.md \
		|| command -v open >/dev/null && open $(REPORT_DIR)/summary.md \
		|| cat $(REPORT_DIR)/summary.md

# ---------------------------------------------------------------------------
# Сквозной прогон: от пилота до отчёта одной командой
# ---------------------------------------------------------------------------

.PHONY: smoke
smoke: setup-cluster pilot analyze ## Полный sanity-check пайплайна: кластер -> пилот -> анализ

# ---------------------------------------------------------------------------
# Уборка
# ---------------------------------------------------------------------------

.PHONY: clean
clean: ## Убрать venv-ы, __pycache__, Go build-кэш, отчёты анализа (образ плагина чистится в форке отдельно)
	rm -rf $(HARNESS_VENV) $(ANALYSIS_VENV) $(REPORT_DIR)
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	cd metrics-agent && go clean ./... 2>/dev/null || true

.PHONY: nuke
nuke: clean-jobs scheduler-undeploy clean ## clean + убрать Job и Deployment планировщика из кластера
	$(KUBECTL) delete namespace $(NAMESPACE) --ignore-not-found
