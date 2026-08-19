#!/usr/bin/env bash
# Куда бот пишет: показать доступные chat_id или переключить канал на другой.
#
# Почему через под, а не с хоста. Токен бота лежит в Secret кластера и наружу
# ему выходить незачем; кроме того, путь до Bot API из партнёрской сети рабочий
# ТОЛЬКО из пода — там подменено разрешение api.telegram.org
# (overlays/prod/ss-notifier-prod-patch.yaml: настоящий адрес имени оттуда не
# отвечает). То есть запрос из пода проверяет ровно тот путь, которым потом
# полетят сообщения.
#
#   ./notifier-chat.sh list          — chat_id из свежих сообщений боту
#   ./notifier-chat.sh set <chat_id> — переключить канал и перезапустить службу
set -euo pipefail

NS=${MONITORING_NAMESPACE:-sensitivityscore-monitoring}
KUBECTL=${KUBECTL:-kubectl}
ACTION=${1:-list}

PY_LIST=$(cat <<'PY'
import json, os, urllib.request

token = os.environ["TELEGRAM_BOT_TOKEN"]
api = os.environ.get("TELEGRAM_API", "https://api.telegram.org").rstrip("/")
with urllib.request.urlopen(f"{api}/bot{token}/getUpdates", timeout=15) as r:
    answer = json.load(r)

if not answer.get("ok"):
    raise SystemExit(f"getUpdates не удался: {answer.get('description')}")

seen = {}
for upd in answer.get("result", []):
    # Сообщения, вступления бота в чат, посты в канале — chat лежит в любом из них.
    for key in ("message", "edited_message", "channel_post", "my_chat_member"):
        chat = (upd.get(key) or {}).get("chat")
        if chat:
            seen[chat["id"]] = chat

if not seen:
    print("  свежих сообщений нет.")
    print("  Telegram хранит их 24 часа и отдаёт боту только то, что он вправе видеть:")
    print("  напишите в нужном чате /start@<имя_бота> (команда, адресованная боту,")
    print("  видна ему даже при включённом режиме приватности) и повторите.")
    raise SystemExit(0)

for cid, chat in sorted(seen.items()):
    kind = chat.get("type", "?")
    name = chat.get("title") or " ".join(
        filter(None, (chat.get("first_name"), chat.get("last_name")))) or chat.get("username", "")
    mark = "  <- групповой" if kind in ("group", "supergroup") else ""
    print(f"  {cid:<16} {kind:<11} {name}{mark}")
    if kind == "group":
        print("    ВНИМАНИЕ: обычная группа. При превращении её в супергруппу")
        print("    (добавление истории, темы, >200 участников) chat_id СМЕНИТСЯ")
        print("    на -100…, и бот замолчит молча — переключать придётся заново.")
    if chat.get("is_forum"):
        print("    Это форум с темами: сообщения без message_thread_id попадают")
        print("    в General. ss-notifier тему не выбирает.")
PY
)

case "$ACTION" in
list)
    echo "— чаты, о которых бот знает ——————————————————————————————"
    $KUBECTL -n "$NS" exec deploy/ss-notifier -- python3 -c "$PY_LIST"
    echo ""
    echo "переключить:  make notifier-chat CHAT_ID=<id>"
    ;;
set)
    CHAT_ID=${2:?укажи chat_id: $0 set <chat_id>}
    # Пустое значение или явная опечатка вида «12 34» сломали бы канал молча:
    # служба стартует, а Telegram отвечает «chat not found» на каждый алерт.
    case "$CHAT_ID" in
        -[0-9]*|[0-9]*) ;;
        *) echo "chat_id должен быть числом (у групп — отрицательным): '$CHAT_ID'" >&2; exit 2 ;;
    esac
    current=$($KUBECTL -n "$NS" get secret ss-notifier-config \
        -o jsonpath='{.data.TELEGRAM_CHAT_ID}' 2>/dev/null | base64 -d 2>/dev/null || true)
    echo "было: ${current:-<не задан>}  ->  станет: $CHAT_ID"
    $KUBECTL -n "$NS" patch secret ss-notifier-config --type=merge \
        -p "{\"stringData\":{\"TELEGRAM_CHAT_ID\":\"$CHAT_ID\"}}" >/dev/null
    # envFrom читается только при старте контейнера: без перезапуска под
    # продолжал бы писать в старый чат, и это выглядело бы как «не применилось».
    $KUBECTL -n "$NS" rollout restart deploy/ss-notifier
    $KUBECTL -n "$NS" rollout status deploy/ss-notifier --timeout=120s
    echo ""
    echo "проверить живым сообщением: make alerts-test"
    ;;
*)
    echo "использование: $0 list | set <chat_id>" >&2
    exit 2
    ;;
esac
