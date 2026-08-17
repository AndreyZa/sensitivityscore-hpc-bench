# load-watcher (paypal/load-watcher) — сервис фактической утилизации узлов
# для Trimaran-плечей энерговетки (профили peaks и packing в
# k8s/scheduler-config/scheduler-config.yaml).
#
# Зачем свой образ: у апстрима НЕТ опубликованного образа (Docker Hub
# paypal/load-watcher не существует, проверено 17.08.2026), а плагин Peaks
# без сервиса load-watcher не стартует вовсе: его PeaksArgs, в отличие от
# LoadVariationRiskBalancing, не имеет metricProvider — пустой WatcherAddress
# валит checkSpecs ("invalid MetricProvider.Type"). Форк не патчим (решение
# 17.08) — поэтому сервис.
#
# Сборка из исходников с пином коммита: апстрим-мастер от 05.07.2026.
# Слушает :2020 (GET /watcher); провайдер метрик задаётся окружением
# METRICS_PROVIDER_NAME (KubernetesMetricsServer — дефолт, in-cluster).
FROM golang:1.23-alpine AS build
RUN apk add --no-cache git
ARG LW_COMMIT=f6b6fc61da09b932c0223b315ffbfb164e26c4a3
RUN git clone https://github.com/paypal/load-watcher /src \
    && cd /src && git checkout --quiet "$LW_COMMIT"
WORKDIR /src
# CGO выключен: статический бинарь, рантайм-слою не нужен libc тулчейна.
RUN CGO_ENABLED=0 go build -o /load-watcher main.go

FROM alpine:3.20
COPY --from=build /load-watcher /bin/load-watcher
USER 65534:65534
EXPOSE 2020
ENTRYPOINT ["/bin/load-watcher"]
