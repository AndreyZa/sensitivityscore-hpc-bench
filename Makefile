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
SCHEDULER_RELEASE_VER   ?= v20260714-5c212261
WORKLOAD_IMAGE          ?= $(REGISTRY)/geant4:11.2
SCHEDULER_IMAGE         ?= $(REGISTRY)/sensitivityscore:$(SCHEDULER_RELEASE_VER)
METRICS_AGENT_IMAGE     ?= $(REGISTRY)/metrics-agent:dev
AGGRESSOR_IMAGE         ?= $(REGISTRY)/aggressor:dev
HARNESS_IMAGE           ?= $(REGISTRY)/harness:dev

# Прокси Docker Desktop рвёт upload'ы в Docker Hub с EOF — лечим ретраями.
PUSH_RETRIES ?= 5
PUSH_BACKOFF ?= 10

# $(call docker_push,<image>) — проверка наличия (ошибка постоянная, ретрай
# бессмыслен), затем ретраи на сетевую флакозность.
define docker_push
@docker image inspect $(1) >/dev/null 2>&1 || { \
	echo ">> НЕТ локального образа $(1) — сначала собери его (make images)."; \
	exit 1; \
}
@for i in $$(seq 1 $(PUSH_RETRIES)); do \
	echo ">> push $(1) (попытка $$i/$(PUSH_RETRIES))"; \
	if docker push $(1); then exit 0; fi; \
	if [ $$i -lt $(PUSH_RETRIES) ]; then \
		echo ">> не вышло, пауза $(PUSH_BACKOFF)s"; sleep $(PUSH_BACKOFF); \
	fi; \
done; \
echo ">> ОШИБКА: $(1) не запушился за $(PUSH_RETRIES) попыток."; \
echo ">> Упереться: make PUSH_RETRIES=15 ...  Либо снять прокси в"; \
echo ">> Docker Desktop -> Settings -> Resources -> Proxies (убьёт kind)."; \
exit 1
endef

# Узлы стенда amd64: без --platform сборка с Apple Silicon даёт arm64, и он
# падает на узле "exec format error" уже после пуша. На amd64-хосте это no-op.
IMAGE_PLATFORM          ?= linux/amd64

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

# GOOS=linux: pkg/perf держится на perf_event_open/cgroupfs, на darwin этих
# символов в x/sys/unix нет и хостовый go vet падает undefined.
.PHONY: vet-go
vet-go: ## go vet по metrics-agent (под целевой linux)
	cd metrics-agent && GOOS=linux go vet ./...

.PHONY: build-go
build-go: fmt-go vet-go ## Собрать metrics-agent локально
	cd metrics-agent && GOOS=linux CGO_ENABLED=0 go build ./...

.PHONY: test-go
test-go: ## Юнит-тесты metrics-agent
	cd metrics-agent && go test -v -count=1 ./...

# ---------------------------------------------------------------------------
# Docker-образы (workload + metrics-agent; scheduler — см. секцию выше)
# ---------------------------------------------------------------------------

.PHONY: image-workload
image-workload: ## Собрать образ нагрузки Geant4 (workload/)
	docker build --platform $(IMAGE_PLATFORM) -t $(WORKLOAD_IMAGE) ./workload

.PHONY: image-workload-push
image-workload-push:
	$(call docker_push,$(WORKLOAD_IMAGE))

.PHONY: image-metrics-agent
image-metrics-agent: build-go ## Собрать образ metrics-agent
	docker build --platform $(IMAGE_PLATFORM) -t $(METRICS_AGENT_IMAGE) ./metrics-agent

.PHONY: image-aggressor
image-aggressor: ## Собрать образ LLC/membw-агрессора (stress-ng) для pressure-сценариев
	docker build --platform $(IMAGE_PLATFORM) -t $(AGGRESSOR_IMAGE) ./aggressor

.PHONY: images
images: image-workload image-metrics-agent image-harness image-aggressor ## Собрать ВСЕ образы стенда (кроме плагина — он в форке, см. scheduler-plugin-image)

.PHONY: images-push
images-push: ## Запушить ВСЕ образы стенда в registry (теги — переменные *_IMAGE выше)
	$(call docker_push,$(WORKLOAD_IMAGE))
	$(call docker_push,$(METRICS_AGENT_IMAGE))
	$(call docker_push,$(HARNESS_IMAGE))
	$(call docker_push,$(AGGRESSOR_IMAGE))

# ---------------------------------------------------------------------------
# Кластер: bootstrap (namespace + Redis), деплой планировщика, деплой агента
# ---------------------------------------------------------------------------

.PHONY: bootstrap
bootstrap: ## Разметить роли узлов (ss-system/bench) + namespace + Redis: make bootstrap SS_NODES="<ss-system-узел> [ещё]"
	./scripts/bootstrap-cluster.sh $(SS_NODES)

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

.PHONY: net-sink-deploy
net-sink-deploy: ## Развернуть sink-приёмник сетевого вывода high-s-net (OUTPUT_MODE=stream)
	$(KUBECTL) apply -f k8s/net-sink/sink.yaml
	$(KUBECTL) -n $(HARNESS_NAMESPACE) rollout status deploy/ss-sink --timeout=120s

.PHONY: net-sink-clean
net-sink-clean: ## Убрать sink-приёмник
	$(KUBECTL) delete -f k8s/net-sink/sink.yaml --ignore-not-found

# ---------------------------------------------------------------------------
# Мониторинг — Prometheus + Grafana на ss-system. Обоснование схемы сбора и
# чистоты измерений: k8s/monitoring/README.md.
# ---------------------------------------------------------------------------

MONITORING_NAMESPACE ?= sensitivityscore-monitoring
# Прод: k8s/monitoring/overlays/prod. Комментарий отдельной строкой — в make
# всё до `#` попадает в значение вместе с пробелами перед ним.
MONITORING_OVERLAY   ?= k8s/monitoring/overlays/stage
GRAFANA_PORT         ?= 3000
PROMETHEUS_PORT      ?= 9090

.PHONY: monitoring-secret
monitoring-secret: ## Создать секрет grafana-admin со случайным паролем (идемпотентно, в git не попадает)
	@$(KUBECTL) create namespace $(MONITORING_NAMESPACE) --dry-run=client -o yaml | $(KUBECTL) apply -f - >/dev/null
	@if $(KUBECTL) -n $(MONITORING_NAMESPACE) get secret grafana-admin >/dev/null 2>&1; then \
		echo "secret/grafana-admin уже есть — пароль не трогаем"; \
	else \
		pw=$$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24); \
		$(KUBECTL) -n $(MONITORING_NAMESPACE) create secret generic grafana-admin \
			--from-literal=admin-user=admin --from-literal=admin-password="$$pw"; \
		echo ""; \
		echo "  Grafana admin: admin / $$pw"; \
		echo "  (сохрани сейчас — повторно make его не покажет; сброс: make monitoring-password-reset)"; \
		echo ""; \
	fi

.PHONY: monitoring-deploy
monitoring-deploy: monitoring-secret ## Развернуть стек мониторинга на ss-system
	$(KUBECTL) apply -k $(MONITORING_OVERLAY)
	$(KUBECTL) -n $(MONITORING_NAMESPACE) rollout status deploy/prometheus --timeout=180s
	$(KUBECTL) -n $(MONITORING_NAMESPACE) rollout status deploy/grafana --timeout=180s
	$(KUBECTL) -n $(MONITORING_NAMESPACE) rollout status deploy/kube-state-metrics --timeout=180s
	@echo "готово — дальше: make monitoring-open"

.PHONY: monitoring-reload
monitoring-reload: ## Перечитать scrape-конфиг и правила без перезапуска пода
	$(KUBECTL) apply -k $(MONITORING_OVERLAY)
	@echo "ждём распространения ConfigMap на том (до ~60s)..."
	@sleep 60
	$(KUBECTL) -n $(MONITORING_NAMESPACE) exec deploy/prometheus -- \
		wget -q -O- --post-data='' http://localhost:9090/-/reload && echo "reload OK"

.PHONY: monitoring-open
monitoring-open: ## Проброс портов: Grafana :3000, Prometheus :9090 (Ctrl-C — закрыть)
	@echo "Grafana    -> http://localhost:$(GRAFANA_PORT)  (логин: make monitoring-password)"
	@echo "Prometheus -> http://localhost:$(PROMETHEUS_PORT)/targets"
	@trap 'kill 0' EXIT; \
	$(KUBECTL) -n $(MONITORING_NAMESPACE) port-forward svc/grafana $(GRAFANA_PORT):3000 & \
	$(KUBECTL) -n $(MONITORING_NAMESPACE) port-forward svc/prometheus $(PROMETHEUS_PORT):9090 & \
	wait

.PHONY: monitoring-password
monitoring-password: ## Показать текущий пароль Grafana из секрета
	@$(KUBECTL) -n $(MONITORING_NAMESPACE) get secret grafana-admin \
		-o jsonpath='{.data.admin-password}' | base64 -d; echo

.PHONY: monitoring-password-reset
monitoring-password-reset: ## Перевыпустить пароль Grafana (пересоздаёт секрет и перезапускает под)
	$(KUBECTL) -n $(MONITORING_NAMESPACE) delete secret grafana-admin --ignore-not-found
	@$(MAKE) --no-print-directory monitoring-secret
	$(KUBECTL) -n $(MONITORING_NAMESPACE) rollout restart deploy/grafana

.PHONY: monitoring-targets
monitoring-targets: ## Показать состояние scrape-целей (up/down) без открытия UI
	@$(KUBECTL) -n $(MONITORING_NAMESPACE) exec deploy/prometheus -- \
		wget -q -O- 'http://localhost:9090/api/v1/query?query=up' \
		| python3 -c 'import json,sys;rs=json.load(sys.stdin)["data"]["result"];rs.sort(key=lambda r:r["metric"].get("job",""));[print("UP  " if r["value"][1]=="1" else "DOWN", r["metric"].get("job","?").ljust(28), r["metric"].get("instance","?")) for r in rs]'

.PHONY: monitoring-clean
monitoring-clean: ## Снести стек мониторинга (TSDB в hostPath на узле НЕ удаляется)
	$(KUBECTL) delete -k $(MONITORING_OVERLAY) --ignore-not-found
	@echo "namespace удалён; данные Prometheus/Grafana остались в /var/lib/sensitivityscore на ss-system"

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
# PMU smoke-test (Этап 0, шаг 1а) — проверить, что cgroup-scoped
# perf_event_open() на КОНКРЕТНОМ узле не только открывается, но и ЧЕСТНО
# считает, ДО разворачивания metrics-agent DaemonSet. Прежний EINVAL «везде»
# был нашим багом (cpu=-1, исправлен) — теперь perfcheck ловит не код, а
# честность железа/гипервизора: open OK + read>0 = честно; open OK + read=0 =
# гипервизор подделывает счётчик (наблюдалось на VMware Workstation; Timeweb-
# KVM и bare-metal считают честно); FAILED to open = права/paranoid/политика.
# Узлы бывают неоднородны — гоняй per-node: `make perfcheck-run NODE=<имя>`
# (без NODE под встаёт куда придётся). См. metrics-agent/cmd/perfcheck/main.go.
# ---------------------------------------------------------------------------

.PHONY: perfcheck-image
perfcheck-image: ## Собрать образ perfcheck (изолированная PMU-проверка)
	docker build --platform $(IMAGE_PLATFORM) -t andreyza/perfcheck:dev -f metrics-agent/cmd/perfcheck/Dockerfile ./metrics-agent

.PHONY: perfcheck-push
perfcheck-push: ## Запушить образ perfcheck в registry (узлы тянут его с imagePullPolicy: Always)
	$(call docker_push,andreyza/perfcheck:dev)

.PHONY: perfcheck-run
perfcheck-run: ## Засабмитить разовый под perfcheck (NODE=<имя> — прибить к конкретному узлу)
	$(KUBECTL) delete pod perfcheck --ignore-not-found
	@if [ -n "$(NODE)" ]; then \
		echo "perfcheck: pinning to node $(NODE)"; \
		sed "s|__NODE__|$(NODE)|" metrics-agent/cmd/perfcheck/pod.yaml | $(KUBECTL) apply -f -; \
	else \
		grep -v '__NODE__' metrics-agent/cmd/perfcheck/pod.yaml | $(KUBECTL) apply -f -; \
	fi

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
# Код net_pressure в агенте реализован end-to-end (main.go:netPressure,
# writer.go); netcheck — операционная половина: измерить референс и выставить
# NET_REFERENCE_MBPS на DaemonSet.
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

# Калибровки живут в ConfigMap metrics-agent-calibration, а НЕ в env
# DaemonSet'а: пока они задавались `kubectl set env`, любой `kubectl apply -f
# daemonset.yaml` их молча обнулял (случилось 2026-07-18). ConfigMap апплаем
# DaemonSet'а не трогается. В git не хранится — значения стендо-специфичны.
CALIBRATION_CM ?= metrics-agent-calibration

.PHONY: calibration-apply
calibration-apply: ## Выставить калибровки: make calibration-apply NET_REFERENCE_MBPS=<N> LLC_REFERENCE_MISSES_PER_SEC=<M> (перезапускает агент — МЕЖДУ сериями)
	@test -n "$(NET_REFERENCE_MBPS)$(LLC_REFERENCE_MISSES_PER_SEC)" || { \
		echo "укажи хотя бы одну: NET_REFERENCE_MBPS=<N> (из make netcheck-logs)"; \
		echo "                    LLC_REFERENCE_MISSES_PER_SEC=<M> (llc_misses_per_sec под 2x stress-ng --stream)"; \
		exit 1; }
	@args=""; \
	if [ -n "$(NET_REFERENCE_MBPS)" ]; then args="$$args --from-literal=NET_REFERENCE_MBPS=$(NET_REFERENCE_MBPS)"; fi; \
	if [ -n "$(LLC_REFERENCE_MISSES_PER_SEC)" ]; then args="$$args --from-literal=LLC_REFERENCE_MISSES_PER_SEC=$(LLC_REFERENCE_MISSES_PER_SEC)"; fi; \
	existing=$$($(KUBECTL) -n $(NAMESPACE) get cm $(CALIBRATION_CM) -o json 2>/dev/null \
		| python3 -c 'import json,sys; d=json.load(sys.stdin).get("data",{}); print(" ".join(f"--from-literal={k}={v}" for k,v in d.items()))' 2>/dev/null); \
	for kv in $$existing; do \
		key=$${kv#--from-literal=}; key=$${key%%=*}; \
		case "$$args" in *"--from-literal=$$key="*) ;; *) args="$$args $$kv";; esac; \
	done; \
	$(KUBECTL) -n $(NAMESPACE) create configmap $(CALIBRATION_CM) $$args \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n $(NAMESPACE) rollout restart ds/sensitivityscore-metrics-agent
	$(KUBECTL) -n $(NAMESPACE) rollout status ds/sensitivityscore-metrics-agent --timeout=240s
	@$(MAKE) --no-print-directory calibration-show

.PHONY: calibration-show
calibration-show: ## Показать текущие калибровки стенда
	@$(KUBECTL) -n $(NAMESPACE) get cm $(CALIBRATION_CM) -o json 2>/dev/null \
		| python3 -c 'import json,sys; d=json.load(sys.stdin).get("data") or {}; \
		  print("\n".join(f"  {k} = {v}" for k,v in sorted(d.items())) or "  (пусто — обе оси выключены)")' \
		|| echo "  ConfigMap $(CALIBRATION_CM) отсутствует — обе оси не откалиброваны"

.PHONY: netcheck-apply
netcheck-apply: ## Выставить измеренный референс: make netcheck-apply NET_REFERENCE_MBPS=<N> (перезапускает агент — делать МЕЖДУ сериями, не в прогоне)
	@test -n "$(NET_REFERENCE_MBPS)" || { echo "укажи число из 'make netcheck-logs': make netcheck-apply NET_REFERENCE_MBPS=<N> (пусто = ось Net выключить -> netcheck-disable)"; exit 1; }
	@$(MAKE) --no-print-directory calibration-apply NET_REFERENCE_MBPS=$(NET_REFERENCE_MBPS)

.PHONY: netcheck-disable
netcheck-disable: ## Выключить Net-ось (сырой net_bw пишется, net_pressure=0)
	$(KUBECTL) -n $(NAMESPACE) patch cm $(CALIBRATION_CM) --type=json \
		-p='[{"op":"remove","path":"/data/NET_REFERENCE_MBPS"}]' || true
	$(KUBECTL) -n $(NAMESPACE) rollout restart ds/sensitivityscore-metrics-agent

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

# ---------------------------------------------------------------------------
# Запуск серии одной командой — preflight + запуск фоном + статус-страница +
# вотчдог (scripts/run-series.sh). Конвенция имён:
# SERIES=placebo -> harness/config-stage-placebo.yaml + run-stage-placebo.sh.
# FORCE=1 превращает проваленные проверки preflight в предупреждения.
# ---------------------------------------------------------------------------

# Имена полей Redis захардкожены в трёх местах (агент пишет, планировщик и
# харнесс читают), в двух репозиториях и на двух языках. Расхождение не падает,
# а тихо обнуляет давление — см. contract/redis-fields.yaml. Та же проверка
# входит в preflight серии, вместе со сверкой живых данных.
.PHONY: check-contract
check-contract: ## Сверить имена полей Redis во всех трёх копиях (агент/планировщик/харнесс)
	@$(PYTHON) scripts/check-redis-contract.py

.PHONY: series
series: venv-harness ## Прогнать серию под ключ: make series SERIES=<имя> (preflight+запуск+вотчдог)
	@test -n "$(SERIES)" || { echo "укажи серию: make series SERIES=<имя> (config-stage-<имя>.yaml)"; exit 1; }
	./scripts/run-series.sh start $(SERIES)

.PHONY: series-preflight
series-preflight: venv-harness ## Проверить стенд перед серией, ничего не запуская: make series-preflight SERIES=<имя>
	@test -n "$(SERIES)" || { echo "укажи серию: make series-preflight SERIES=<имя>"; exit 1; }
	@./scripts/run-series.sh preflight $(SERIES)

.PHONY: series-status
series-status: ## Состояние серии: фазы, ошибки, строки результатов: make series-status SERIES=<имя>
	@test -n "$(SERIES)" || { echo "укажи серию: make series-status SERIES=<имя>"; exit 1; }
	@./scripts/run-series.sh status $(SERIES)

.PHONY: series-stop
series-stop: ## Остановить серию и прибрать кластер (агрессоры, job'ы): make series-stop SERIES=<имя>
	@test -n "$(SERIES)" || { echo "укажи серию: make series-stop SERIES=<имя>"; exit 1; }
	./scripts/run-series.sh stop $(SERIES)

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
	docker build --platform $(IMAGE_PLATFORM) -t $(HARNESS_IMAGE) ./harness

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
# ClickHouse — центральная агрегация результатов (batch-load parquet).
# CH_HOST/CH_PORT/... — адрес ПК-агрегатора; STAND/RUN_LABEL — провенанс.
# См. db/clickhouse/README.md.
# ---------------------------------------------------------------------------

CH_VENV     ?= db/clickhouse/.venv
CH_HOST     ?= localhost
CH_PORT     ?= 8123
CH_USER     ?= default
CH_PASSWORD ?=
CH_DATABASE ?= sensitivityscore
# ПК-агрегатор слушает только localhost; доступ из WSL2 — через SSH-туннель.
# Управляем им через ControlMaster-сокет (-O check/exit), а не pgrep/pkill:
# рецепт сам содержит ssh-строку, из-за чего pkill -f матчил бы свой же shell.
CH_SSH         ?= andrey@192.168.1.72
CH_TUNNEL_PORT ?= 8123
CH_SOCK        ?= /tmp/ch-tunnel-$(CH_TUNNEL_PORT).sock

# Результаты льются в ДВА приёмника (ch-load-all):
#   prod — in-cluster ClickHouse прод-стенда (make ch-forward),
#   home — домашний ПК-агрегатор, кросс-стендовая агрегация (make ch-tunnel).
# Порты локальные и РАЗНЫЕ намеренно: туннель к дому уже занимает 8123, а оба
# приёмника должны быть доступны одновременно, иначе «залить в оба» не выйдет.
CH_SINKS     ?= prod home
CH_PROD_HOST ?= localhost
CH_PROD_PORT ?= 8124
CH_HOME_HOST ?= localhost
CH_HOME_PORT ?= $(CH_TUNNEL_PORT)

.PHONY: ch-tunnel
ch-tunnel: ## Поднять SSH-туннель к ПК-агрегатору (CH_SSH=user@host); дальше ch-load CH_HOST=localhost
	@if ssh -S $(CH_SOCK) -O check $(CH_SSH) 2>/dev/null; then \
		echo "туннель уже поднят"; \
	else \
		ssh -M -S $(CH_SOCK) -f -N -L $(CH_TUNNEL_PORT):localhost:8123 $(CH_SSH) && \
		echo "туннель localhost:$(CH_TUNNEL_PORT) -> $(CH_SSH):8123 поднят"; \
	fi

.PHONY: ch-tunnel-close
ch-tunnel-close: ## Закрыть SSH-туннель к ПК-агрегатору
	@ssh -S $(CH_SOCK) -O exit $(CH_SSH) 2>/dev/null && echo "туннель закрыт" || echo "туннель не найден"

$(CH_VENV)/bin/activate: db/clickhouse/requirements.txt
	$(PYTHON) -m venv $(CH_VENV)
	$(CH_VENV)/bin/pip install --quiet --upgrade pip
	$(CH_VENV)/bin/pip install --quiet -r db/clickhouse/requirements.txt
	touch $(CH_VENV)/bin/activate

.PHONY: venv-clickhouse
venv-clickhouse: $(CH_VENV)/bin/activate ## Создать/обновить venv для загрузчика ClickHouse

.PHONY: ch-schema
ch-schema: ## Применить schema.sql на ПК-агрегаторе (нужен clickhouse-client; CH_HOST=<PC>)
	clickhouse-client --host $(CH_HOST) --multiquery < db/clickhouse/schema.sql

# schema.sql создаёт таблицы через IF NOT EXISTS и потому не меняет уже
# существующие — новые колонки накатываются миграциями.
.PHONY: ch-migrate
ch-migrate: ## Накатить миграции схемы на приёмник: make ch-migrate CH_HOST=<хост>
	@for m in db/clickhouse/migrations/*.sql; do \
		echo "-> $$m"; \
		clickhouse-client --host $(CH_HOST) --port 9000 --multiquery < "$$m" || exit 1; \
	done
	@echo "миграции применены"

.PHONY: ch-load
ch-load: venv-clickhouse ## Залить results+baselines в ClickHouse: make ch-load CH_HOST=<PC> STAND=<s> RUN_LABEL=<l>
	@test -n "$(STAND)" && test -n "$(RUN_LABEL)" || { echo "укажи STAND=<стенд> RUN_LABEL=<метка серии>"; exit 1; }
	$(CH_VENV)/bin/python db/clickhouse/load_parquet.py \
		--host $(CH_HOST) --port $(CH_PORT) --user $(CH_USER) --password "$(CH_PASSWORD)" \
		--database $(CH_DATABASE) --stand $(STAND) --run-label $(RUN_LABEL) \
		--results $(RESULTS_FILE) --baselines $(BASELINES_FILE)

# Заливка в оба приёмника. Устойчивость к недоступности одного из них здесь
# принципиальна: источник истины — parquet на диске, поэтому падение домашнего
# ПК (или отсутствие прод-кластера) не должно мешать залить во второй. Поэтому
# цикл не прерывается на первой ошибке, а в конце печатает команду долива
# именно того приёмника, который не взлетел. Повторная заливка безопасна:
# таблицы — ReplacingMergeTree(ingested_at), версии схлопываются при мерже,
# а читатели селектят с FINAL (analysis/clickhouse_source.py).
.PHONY: ch-load-all
ch-load-all: venv-clickhouse ## Залить results+baselines во ВСЕ приёмники: make ch-load-all STAND=<s> RUN_LABEL=<l> [CH_SINKS="prod home"]
	@test -n "$(STAND)" && test -n "$(RUN_LABEL)" || { echo "укажи STAND=<стенд> RUN_LABEL=<метка серии>"; exit 1; }
	@# Загрузчик на отсутствующий файл лишь предупреждает и выходит с 0 (чтобы
	@# можно было залить только --results или только --baselines). Здесь это
	@# опасно: цель отрапортовала бы «залито во все приёмники», не залив ничего.
	@test -f $(RESULTS_FILE) || test -f $(BASELINES_FILE) || { \
		echo "нет ни $(RESULTS_FILE), ни $(BASELINES_FILE) — сначала make harness-fetch-results"; exit 1; }
	@failed=""; \
	for sink in $(CH_SINKS); do \
		case $$sink in \
			prod) host="$(CH_PROD_HOST)"; port="$(CH_PROD_PORT)"; hint="make ch-forward";; \
			home) host="$(CH_HOME_HOST)"; port="$(CH_HOME_PORT)"; hint="make ch-tunnel";; \
			*) echo "неизвестный приёмник: $$sink (ожидается prod и/или home)"; exit 1;; \
		esac; \
		echo ""; echo "=== $$sink -> $$host:$$port ==="; \
		if $(CH_VENV)/bin/python db/clickhouse/load_parquet.py \
			--host "$$host" --port "$$port" --user $(CH_USER) --password "$(CH_PASSWORD)" \
			--database $(CH_DATABASE) --stand $(STAND) --run-label $(RUN_LABEL) \
			--results $(RESULTS_FILE) --baselines $(BASELINES_FILE); then \
			echo "  OK: $$sink"; \
		else \
			echo "  ОШИБКА: приёмник $$sink недоступен (поднят ли $$hint?)"; \
			echo "  parquet на месте — долить позже одной командой:"; \
			echo "    make ch-load-all CH_SINKS=$$sink STAND=$(STAND) RUN_LABEL=$(RUN_LABEL)"; \
			failed="$$failed $$sink"; \
		fi; \
	done; \
	echo ""; \
	if [ -n "$$failed" ]; then echo "НЕ залито:$$failed — см. команды долива выше"; exit 1; fi; \
	echo "залито во все приёмники: $(CH_SINKS)"

.PHONY: ch-forward
ch-forward: ## Проброс in-cluster ClickHouse на localhost:$(CH_PROD_PORT) (Ctrl-C — закрыть)
	@echo "ClickHouse (in-cluster) -> localhost:$(CH_PROD_PORT); дальше в другом окне: make ch-load-all"
	$(KUBECTL) -n $(CH_INCLUSTER_NS) port-forward svc/clickhouse $(CH_PROD_PORT):8123

.PHONY: ch-analyze
ch-analyze: venv-analysis ## Построить H1-H4 отчёт ИЗ ClickHouse: make ch-analyze STAND=<s> RUN_LABEL=<l> (нужен ch-tunnel)
	@test -n "$(STAND)" && test -n "$(RUN_LABEL)" || { echo "укажи STAND=<стенд> RUN_LABEL=<метка серии> (поверх make ch-tunnel)"; exit 1; }
	cd analysis && ../$(ANALYSIS_VENV)/bin/python analyze.py --clickhouse \
		--ch-host $(CH_HOST) --ch-port $(CH_PORT) --ch-database $(CH_DATABASE) \
		--ch-user $(CH_USER) --ch-password "$(CH_PASSWORD)" \
		--stand $(STAND) --run-label $(RUN_LABEL) --outdir report

# --- In-cluster ClickHouse (StatefulSet на системной ноде; см. k8s/clickhouse) ---
CH_INCLUSTER_NS ?= sensitivityscore-system
# Прод: k8s/clickhouse/overlays/prod (комментарий отдельной строкой — см. выше).
CH_KUSTOMIZE    ?= k8s/clickhouse/base

.PHONY: ch-incluster-deploy
ch-incluster-deploy: ## Развернуть in-cluster ClickHouse (CH_KUSTOMIZE=base|k8s/clickhouse/overlays/prod)
	$(KUBECTL) create namespace $(CH_INCLUSTER_NS) --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n $(CH_INCLUSTER_NS) create configmap clickhouse-schema \
		--from-file=schema.sql=db/clickhouse/schema.sql --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) apply -k $(CH_KUSTOMIZE)

.PHONY: ch-incluster-status
ch-incluster-status: ## Статус in-cluster ClickHouse (под, PVC, schema-Job)
	$(KUBECTL) -n $(CH_INCLUSTER_NS) get statefulset,pod,pvc,svc,job -l app=clickhouse

.PHONY: ch-incluster-clean
ch-incluster-clean: ## Снести in-cluster ClickHouse — ВНИМАНИЕ: удаляет PVC с данными
	$(KUBECTL) delete -k $(CH_KUSTOMIZE) --ignore-not-found
	$(KUBECTL) -n $(CH_INCLUSTER_NS) delete configmap clickhouse-schema --ignore-not-found
	$(KUBECTL) -n $(CH_INCLUSTER_NS) delete pvc -l app=clickhouse --ignore-not-found

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
	rm -rf $(HARNESS_VENV) $(ANALYSIS_VENV) $(CH_VENV) $(REPORT_DIR)
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	cd metrics-agent && go clean ./... 2>/dev/null || true

.PHONY: nuke
nuke: clean-jobs scheduler-undeploy clean harness-clean-full ## clean + убрать Job и Deployment планировщика из кластера (внимание: сносит и in-cluster harness Job/PVC — см. harness-clean-full)
	$(KUBECTL) delete namespace $(NAMESPACE) --ignore-not-found
