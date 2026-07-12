# Makefile — sensitivityscore-hpc-bench
#
# Единая точка входа для всего экспериментального стенда, КРОМЕ сборки
# самого кода scheduler-плагина — та живёт в отдельном форке
# kubernetes-sigs/scheduler-plugins (см. scheduler-plugin/README.md) и
# производит ровно один артефакт: Docker-образ.
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
SCHEDULER_RELEASE_VER   ?= v20260711-b2797acc
WORKLOAD_IMAGE          ?= $(REGISTRY)/geant4:11.2
SCHEDULER_IMAGE         ?= $(REGISTRY)/sensitivityscore:$(SCHEDULER_RELEASE_VER)
METRICS_AGENT_IMAGE     ?= $(REGISTRY)/metrics-agent:dev
AGGRESSOR_IMAGE         ?= $(REGISTRY)/aggressor:dev
HARNESS_IMAGE           ?= $(REGISTRY)/harness:dev

# --- Kubernetes ---
KUBECTL      ?= kubectl
NAMESPACE    ?= sensitivityscore-system
KIND_CLUSTER ?= sensitivityscore-dev
SCHEDULER_DEPLOYMENT ?= sensitivityscore-scheduler
HARNESS_NAMESPACE ?= sensitivityscore-bench

# --- Net calibration (netcheck-*) ---
# Stock upstream iperf3 image — not one of ours, override if the stand can't
# reach Docker Hub / mirrors it elsewhere.
IPERF_IMAGE  ?= networkstatic/iperf3

# --- Python venvs ---
PYTHON        ?= python3
HARNESS_VENV  ?= harness/.venv
ANALYSIS_VENV ?= analysis/.venv

# --- Harness / analysis output paths ---
RESULTS_FILE   ?= harness/results/results.parquet
BASELINES_FILE ?= harness/results/baselines.parquet
REPORT_DIR     ?= analysis/report

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
# Scheduler plugin (образ собирается во ВНЕШНЕМ форке scheduler-plugins)
# ---------------------------------------------------------------------------
# См. scheduler-plugin/README.md — код и сборка живут в
# $(SCHEDULER_PLUGINS_DIR), здесь мы только вызываем его Makefile.
 
.PHONY: scheduler-plugin-build
scheduler-plugin-build: ## Собрать пакет плагина в форке (без Docker-образа) — быстрая проверка компиляции
	$(MAKE) -C $(SCHEDULER_PLUGINS_DIR) -f sensitivityscore.mk ss-build
 
.PHONY: scheduler-plugin-test
scheduler-plugin-test: ## Юнит-тесты плагина в форке
	$(MAKE) -C $(SCHEDULER_PLUGINS_DIR) -f sensitivityscore.mk ss-test
 
.PHONY: scheduler-plugin-image
scheduler-plugin-image: ## Собрать Docker-образ плагина в форке -> $(SCHEDULER_IMAGE)
	$(MAKE) -C $(SCHEDULER_PLUGINS_DIR) -f sensitivityscore.mk ss-image \
		REGISTRY=$(REGISTRY) RELEASE_VERSION=$(SCHEDULER_RELEASE_VER)

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

.PHONY: image-workload-push
image-workload-push:
	docker push $(WORKLOAD_IMAGE)

.PHONY: image-metrics-agent
image-metrics-agent: build-go ## Собрать образ metrics-agent
	docker build -t $(METRICS_AGENT_IMAGE) ./metrics-agent

.PHONY: image-aggressor
image-aggressor: ## Собрать образ LLC/membw-агрессора (stress-ng) для pressure-сценариев
	docker build -t $(AGGRESSOR_IMAGE) ./aggressor

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

.PHONY: trimaran-deps
trimaran-deps: ## Установить metrics-server (нужен профилю trimaran; см. scheduler_variants в harness/config.yaml)
	# LoadVariationRiskBalancing (плечо A-trimaran) читает утилизацию нод через
	# metrics-server. На большинстве стендов он уже есть — тогда этот таргет не
	# нужен. На kind/dev его нет: ставим и патчим --kubelet-insecure-tls (kind
	# отдаёт kubelet-метрики по самоподписанному TLS, иначе metrics-server не
	# стартует Ready). На настоящем стенде патч, как правило, не требуется.
	$(KUBECTL) apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
	$(KUBECTL) patch deployment metrics-server -n kube-system --type=json \
		-p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' || true
	$(KUBECTL) rollout status deployment/metrics-server -n kube-system --timeout=120s

# ---------------------------------------------------------------------------
# PMU smoke-test (Фаза 4, шаг 0) — проверить доступность perf_event_open()
# ДО того, как разворачивать весь metrics-agent DaemonSet. Особенно важно
# для Docker Desktop: Kubernetes там работает внутри VM, доступ к PMU может
# быть заблокирован гипервизором — см. metrics-agent/cmd/perfcheck/main.go.
# ---------------------------------------------------------------------------
 
.PHONY: perfcheck-image
perfcheck-image: ## Собрать образ perfcheck (изолированная PMU-проверка)
	docker build -t andreyza/perfcheck:dev -f metrics-agent/cmd/perfcheck/Dockerfile ./metrics-agent
 
.PHONY: perfcheck-run
perfcheck-run: ## Засабмитить разовый под perfcheck
	$(KUBECTL) delete pod perfcheck --ignore-not-found
	$(KUBECTL) apply -f metrics-agent/cmd/perfcheck/pod.yaml
 
.PHONY: perfcheck-logs
perfcheck-logs: ## Посмотреть результат (после того, как под завершится — STATUS Completed/Error)
	$(KUBECTL) logs pod/perfcheck
 
.PHONY: perfcheck-clean
perfcheck-clean: ## Убрать под perfcheck
	$(KUBECTL) delete pod perfcheck --ignore-not-found

# ---------------------------------------------------------------------------
# Net-калибровка (Этап 0, рядом с perfcheck) — измерить реальную пропускную
# способность uplink NIC + CNI между ДВУМЯ РАЗНЫМИ worker-нодами (cross-node,
# НЕ на одной ноде: same-node трафик мостится локально через veth и не
# касается физического NIC). Даёт NET_REFERENCE_MBPS для нормировки net_bw в
# net_pressure — см. docs/Технический план экспериментов.md §3.4.
# Только измерительная половина: код net_pressure в агенте — пока TODO.
# ---------------------------------------------------------------------------

.PHONY: netcheck-run
netcheck-run: ## iperf3 server+client на двух разных worker-нодах (NODE_CLIENT=/NODE_SERVER= для явного выбора)
	@set -e; \
	workers=$$($(KUBECTL) get nodes --selector='!node-role.kubernetes.io/control-plane' -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | sort); \
	nclient="$${NODE_CLIENT:-$$(echo "$$workers" | sed -n 1p)}"; \
	nserver="$${NODE_SERVER:-$$(echo "$$workers" | sed -n 2p)}"; \
	if [ -z "$$nserver" ] || [ "$$nclient" = "$$nserver" ]; then \
		echo "netcheck: нужно >=2 разных worker-нод (есть: $$(echo $$workers | tr '\n' ' ')); задай NODE_CLIENT=/NODE_SERVER= вручную"; exit 1; \
	fi; \
	echo "netcheck: client=$$nclient server=$$nserver image=$(IPERF_IMAGE)"; \
	sed -e "s|__NODE_CLIENT__|$$nclient|" -e "s|__NODE_SERVER__|$$nserver|" -e "s|__IPERF_IMAGE__|$(IPERF_IMAGE)|g" \
		scripts/netcheck/netcheck.yaml | $(KUBECTL) apply -f -

.PHONY: netcheck-logs
netcheck-logs: ## Дождаться завершения клиента и распечатать NET_REFERENCE_MBPS
	@$(KUBECTL) wait --for=jsonpath='{.status.phase}'=Succeeded pod/netcheck-client --timeout=120s || \
		echo "netcheck: клиент ещё не Succeeded — показываю, что есть"; \
	$(KUBECTL) logs pod/netcheck-client | $(PYTHON) scripts/netcheck/parse.py

.PHONY: netcheck-clean
netcheck-clean: ## Убрать поды и Service netcheck
	$(KUBECTL) delete pod netcheck-server netcheck-client --ignore-not-found
	$(KUBECTL) delete svc netcheck-server --ignore-not-found

# Образы НЕ собираются здесь: рабочий цикл — build + push в Docker Hub
# ($(REGISTRY)), кластер пуллит их сам (imagePullPolicy: Always). См.
# image-workload-push / scheduler-plugin-image / image-metrics-agent.
.PHONY: setup-cluster
setup-cluster: bootstrap scheduler-deploy deploy-metrics-agent ## Подготовка кластера: namespace+Redis, планировщик, агент (образы уже в Docker Hub)

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

.PHONY: harness-clean-jobs
harness-clean-jobs: ## Удалить все Job харнесса (namespace HARNESS_NAMESPACE, после make pilot/run-all/run-config-a)
	$(KUBECTL) delete jobs -l app=geant4-bench -n $(HARNESS_NAMESPACE) --ignore-not-found

.PHONY: harness-clean-full
harness-clean-full: harness-clean-jobs ## Снести весь namespace харнесса целиком — ВКЛЮЧАЯ запущенный in-cluster harness Job и PVC с results.parquet! (пересоздастся при следующем прогоне, см. _ensure_namespace/harness-rbac)
	$(KUBECTL) delete namespace $(HARNESS_NAMESPACE) --ignore-not-found

# ---------------------------------------------------------------------------
# Harness — запуск ВНУТРИ кластера (без kubectl port-forward): Redis
# резолвится по обычному in-cluster DNS-имени из harness/config.yaml.
# ---------------------------------------------------------------------------

.PHONY: image-harness
image-harness: ## Собрать образ харнесса (python + kubectl) -> $(HARNESS_IMAGE)
	docker build -t $(HARNESS_IMAGE) ./harness

.PHONY: harness-rbac
harness-rbac: ## Применить namespace/ServiceAccount/RBAC/PVC для in-cluster харнесса (один раз)
	$(KUBECTL) apply -f harness/deploy/rbac.yaml
	$(KUBECTL) apply -f harness/deploy/pvc.yaml

.PHONY: harness-run-pilot-incluster
harness-run-pilot-incluster: harness-rbac ## Пилот (§9 чек-листа) как Job внутри кластера
	$(KUBECTL) delete job harness-pilot -n $(HARNESS_NAMESPACE) --ignore-not-found
	$(KUBECTL) apply -f harness/deploy/job-pilot.yaml

.PHONY: harness-run-config-a-incluster
harness-run-config-a-incluster: harness-rbac ## Полная матрица (config A) как Job внутри кластера
	$(KUBECTL) delete job harness-config-a -n $(HARNESS_NAMESPACE) --ignore-not-found
	$(KUBECTL) apply -f harness/deploy/job-config-a.yaml

.PHONY: harness-run-pressure-incluster
harness-run-pressure-incluster: harness-rbac ## Pressure-сценарии (агрессоры + поток жертв) как Job внутри кластера
	$(KUBECTL) delete job harness-pressure -n $(HARNESS_NAMESPACE) --ignore-not-found
	$(KUBECTL) apply -f harness/deploy/job-pressure.yaml

.PHONY: harness-run-baseline-incluster
harness-run-baseline-incluster: harness-rbac ## Соло-бейзлайны (--baseline) как Job — кластер ДОЛЖЕН быть пустым (slowdown/fingerprint)
	$(KUBECTL) delete job harness-baseline -n $(HARNESS_NAMESPACE) --ignore-not-found
	$(KUBECTL) apply -f harness/deploy/job-baseline.yaml

.PHONY: harness-logs-incluster
harness-logs-incluster: ## Логи текущего in-cluster harness Job (укажи JOB=harness-pilot|harness-config-a)
	$(KUBECTL) logs -n $(HARNESS_NAMESPACE) job/$(JOB) -f

.PHONY: harness-fetch-results
harness-fetch-results: ## Скопировать results.parquet с PVC на хост (после завершения Job'а) — поднимает/переиспользует read-only под
	@$(KUBECTL) get pod harness-results-reader -n $(HARNESS_NAMESPACE) >/dev/null 2>&1 || $(KUBECTL) apply -f harness/deploy/results-reader.yaml
	@$(KUBECTL) wait --for=condition=ready pod/harness-results-reader -n $(HARNESS_NAMESPACE) --timeout=60s
	$(KUBECTL) cp -n $(HARNESS_NAMESPACE) harness-results-reader:/results/results.parquet "$(RESULTS_FILE)"

.PHONY: harness-fetch-baselines
harness-fetch-baselines: ## Скопировать baselines.parquet с PVC на хост (после harness-run-baseline-incluster)
	@$(KUBECTL) get pod harness-results-reader -n $(HARNESS_NAMESPACE) >/dev/null 2>&1 || $(KUBECTL) apply -f harness/deploy/results-reader.yaml
	@$(KUBECTL) wait --for=condition=ready pod/harness-results-reader -n $(HARNESS_NAMESPACE) --timeout=60s
	$(KUBECTL) cp -n $(HARNESS_NAMESPACE) harness-results-reader:/results/baselines.parquet "$(BASELINES_FILE)"

.PHONY: harness-clean-reader
harness-clean-reader: ## Убрать read-only под для выгрузки результатов (после harness-fetch-results)
	$(KUBECTL) delete -f harness/deploy/results-reader.yaml --ignore-not-found

# ---------------------------------------------------------------------------
# Analysis — статистика и графики (§5 плана)
# ---------------------------------------------------------------------------

.PHONY: analyze
analyze: venv-analysis ## Прогнать H1-H4 анализ по текущим результатам харнесса (+ baselines.parquet, если есть: slowdown/fingerprint)
	cd analysis && ../$(ANALYSIS_VENV)/bin/python analyze.py \
		--results ../$(RESULTS_FILE) --baselines ../$(BASELINES_FILE) --outdir report

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
nuke: clean-jobs scheduler-undeploy clean harness-clean-full ## clean + убрать Job и Deployment планировщика из кластера (внимание: сносит и in-cluster harness Job/PVC — см. harness-clean-full)
	$(KUBECTL) delete namespace $(NAMESPACE) --ignore-not-found
