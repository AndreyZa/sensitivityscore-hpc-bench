# Статус-страница прогона в контейнере: один образ на два сценария.
#
#   локально — примонтировать репозиторий read-only и kubeconfig, страница
#              смотрит на те же файлы, что и хостовый запуск;
#   в кластере — смонтировать PVC harness-results (туда пишет in-cluster
#              харнесс), доступ к API — через ServiceAccount, а не kubeconfig.
#
# Сервер ТОЛЬКО читает: ни один его путь не открывается на запись, поэтому и
# монтирования, и RBAC — read-only. Это не перестраховка: страницу удобно
# держать открытой во время серии, и она не должна иметь возможности её
# испортить.
FROM python:3.12-slim

# kubectl нужен statusserver/cluster.py (снимок узлов). Версия совпадает с
# k0s стенда (1.35); если kubectl не окажется — страница живёт дальше с
# пустой секцией кластера, это заложено в cluster.py как best-effort.
ARG KUBECTL_VERSION=v1.35.0
ARG TARGETARCH=amd64
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSLo /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Без этого stdout блочно буферизуется (в контейнере он не TTY), и `docker logs`
# молчит даже когда сервер что-то печатает — отлаживать приходится вслепую.
ENV PYTHONUNBUFFERED=1

# Зависимости отдельным слоем — правка кода страницы не тянет пересборку pip.
COPY statusserver/requirements.txt statusserver/requirements.txt
RUN pip install --no-cache-dir -r statusserver/requirements.txt

# Явный список путей, без `COPY . .` — по правилу репозитория: так «входы
# образа» остаются точным множеством и видно, когда нужна пересборка.
# config_loader из харнесса обязателен: страница читает конфиг серии, а он с
# 19.07 — слой поверх родителя (extends), и плоский разбор показал бы конфиг
# без унаследованных значений, то есть тихо соврал бы.
COPY statusserver/*.py statusserver/
COPY harness/config_loader.py harness/config_loader.py

# Внутри контейнера слушаем все интерфейсы: наружу порт всё равно отдаёт
# docker -p (локально) или Service (в кластере), и там граница уже есть.
# На хосте по умолчанию остаётся 127.0.0.1 — см. server.py.
EXPOSE 8787
ENTRYPOINT ["python", "-m", "statusserver", "--bind", "0.0.0.0"]
