#!/usr/bin/env bash
# Учётные данные реестра для вытягивания образов стенда.
#
# Зачем. Все поды задач и агрессоров идут с imagePullPolicy: Always (dev-теги
# в этом репозитории мутабельны — политика Always защищает от прогона на
# устаревшем образе, см. CLAUDE.md). Без учётки Docker Hub считает узлы
# анонимными и режет по ВНЕШНЕМУ адресу: 100 запросов в час на весь стенд за
# NAT. Серии из десятков задач этого хватает на 15-20 минут, после чего поды
# начинают падать в ErrImagePull ПОСРЕДИ прогона — то есть серия портится не
# сразу и не очевидно. С учёткой лимит считается на пользователя и выше.
#
# Креды в репозиторий не попадают: передаются переменными окружения и
# оседают только в Secret'ах кластера.
#
#   make registry-secret DOCKERHUB_USER=<логин> DOCKERHUB_TOKEN=<токен>
#
# Токен — Personal Access Token с правом Public Repo Read
# (hub.docker.com -> Account Settings -> Personal access tokens), НЕ пароль.
# Повторный запуск просто перезаписывает Secret — безопасно.
set -euo pipefail

KUBECTL=${KUBECTL:-kubectl}
SECRET=${REGISTRY_SECRET_NAME:-registry-creds}
SERVER=${REGISTRY_SERVER:-https://index.docker.io/v1/}
NAMESPACES=${REGISTRY_SECRET_NAMESPACES:-sensitivityscore-system sensitivityscore-bench}

: "${DOCKERHUB_USER:?укажи DOCKERHUB_USER=<логин>}"
: "${DOCKERHUB_TOKEN:?укажи DOCKERHUB_TOKEN=<personal access token>}"

for ns in $NAMESPACES; do
    $KUBECTL create namespace "$ns" --dry-run=client -o yaml | $KUBECTL apply -f - >/dev/null

    $KUBECTL -n "$ns" create secret docker-registry "$SECRET" \
        --docker-server="$SERVER" \
        --docker-username="$DOCKERHUB_USER" \
        --docker-password="$DOCKERHUB_TOKEN" \
        --dry-run=client -o yaml | $KUBECTL apply -f - >/dev/null
    echo "[registry] $ns: Secret $SECRET записан"

    # Патчим ВСЕ ServiceAccount'ы неймспейса, включая default: поды задач и
    # агрессоров своего SA не имеют и берут именно default, а у служебных
    # компонентов SA свои. Патч идемпотентен — kubectl перезапишет поле целиком.
    for sa in $($KUBECTL -n "$ns" get serviceaccounts -o name); do
        $KUBECTL -n "$ns" patch "$sa" \
            -p "{\"imagePullSecrets\":[{\"name\":\"$SECRET\"}]}" >/dev/null
        echo "[registry]   $sa -> imagePullSecrets: $SECRET"
    done
done

cat <<MSG

Готово. ВАЖНО: ServiceAccount, созданный ПОЗЖЕ (новый компонент стенда),
учётки не получит — прогнать таргет ещё раз после деплоя новых компонентов.
Проверить, что под реально тянет с учёткой:
  kubectl -n sensitivityscore-bench get sa default -o jsonpath='{.imagePullSecrets}'
MSG
