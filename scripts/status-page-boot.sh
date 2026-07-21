#!/usr/bin/env bash
# status-page-boot.sh — вернуть статус-страницу после перезагрузки хоста.
#
# Зовётся из systemd-юнита ss-status.service (шаблон рядом: scripts/ss-status.service)
# и больше ниоткуда. Руками звать незачем — для этого есть `make status-page`.
#
# Зачем юнит, если в compose уже стоит `restart: unless-stopped`. Политика
# рестарта возвращает контейнер после падения процесса, перезапуска демона и
# перезагрузки — но только пока контейнер СУЩЕСТВУЕТ и не остановлен явно.
# После `docker compose down` (или `make status-page-down`, или чистки
# `docker system prune`) возвращать нечего: там, где страницу держат постоянно
# — JumpHost .72 — она молча не поднималась бы уже никогда.
#
# Почему не просто `docker compose up -d` в ExecStart. Без переменных compose
# поднимет серию ПО УМОЛЧАНИЮ (pressure): страница ответит на привычном порту,
# будет выглядеть здоровой и показывать чужие данные. Поэтому параметры
# последнего подъёма пишет scripts/run-series.sh в harness/.status-page.env, а
# мы их отсюда читаем.
set -u

cd "$(dirname "$0")/.." || exit 1
ENVFILE=harness/.status-page.env
COMPOSE="docker compose -f statusserver/docker-compose.yaml"

# Страницу на этом хосте ни разу не поднимали — восстанавливать нечего. Это не
# ошибка: юнит может быть включён на машине, где серию ещё не запускали.
if [ ! -f "$ENVFILE" ]; then
    echo "$ENVFILE не найден — страницу здесь ещё не поднимали, восстанавливать нечего"
    exit 0
fi

set -a
# shellcheck source=/dev/null
. "$ENVFILE"
set +a
: "${SERIES=}" "${STAND:=STAGE}" "${STATUS_PORT:=8787}" "${RESULTS:=}" "${BASELINES:=}"

# Тот же капкан, что и в run-series.sh: на месте отсутствующего kubeconfig
# docker создаёт пустой КАТАЛОГ, после чего хостовый kubectl падает с «is a
# directory» на любой команде. Молча ломать kubectl хосту ради секции кластера
# на странице — плохой размен.
if [ ! -f "${KUBECONFIG:-}" ]; then
    echo "WARN: kubeconfig '${KUBECONFIG:-}' не найден — страница поднимется без секции кластера"
    KUBECONFIG=/dev/null
    export KUBECONFIG
fi

# Юнит стартует по After=docker.service, но «активен» для systemd и «принимает
# команды» для демона — не одно и то же: на холодной загрузке первый вызов
# может застать сокет ещё не готовым. Три попытки дешевле, чем failed-юнит и
# отсутствие страницы до ручного вмешательства.
for attempt in 1 2 3; do
    # Без --build: на загрузке хоста пересборка образа не нужна и только
    # растянула бы старт. Если образа statusserver:local нет вовсе, compose
    # соберёт его сам — это его штатное поведение для отсутствующего образа.
    if $COMPOSE up -d; then
        break
    fi
    echo "попытка $attempt: compose up не удался"
    [ "$attempt" = 3 ] && { echo "FAIL: страница не поднята за 3 попытки"; exit 1; }
    sleep 5
done

# `up -d` возвращает 0, как только контейнер СОЗДАН: python внутри ещё
# импортирует pandas/pyarrow, сокет не забинден. Юнит должен рапортовать
# успех тогда, когда страница действительно отвечает.
for _ in $(seq 60); do
    curl -sf -o /dev/null "http://localhost:$STATUS_PORT/healthz" 2>/dev/null && {
        echo "статус-страница поднята: http://localhost:$STATUS_PORT (серия '${SERIES:-pressure}', стенд $STAND)"
        exit 0
    }
    sleep 1
done

echo "FAIL: контейнер создан, но /healthz не ответил за 60 с"
$COMPOSE ps
$COMPOSE logs --tail=20
exit 1
