#!/usr/bin/env bash
# Одна картинка про оповещения: правила, активные алерты и СОСТОЯНИЕ ДОСТАВКИ.
#
# Отдельным скриптом, а не строчкой в Makefile: три источника (Prometheus,
# Alertmanager, счётчики доставки) в одну команду ужимаются только ценой
# нечитаемых однострочников на python — а именно этой командой будут проверять
# канал перед запуском длинной серии.
#
# Программы на python лежат в переменных (heredoc в кавычках), а не в
# `python3 -c '...'`: внутри одинарных кавычек bash не даёт использовать
# одинарные кавычки самого python, а экранирование двойных ломает f-строки
# (поймано первым же запуском: SyntaxError на \" внутри f-string).
set -euo pipefail

NS=${MONITORING_NAMESPACE:-sensitivityscore-monitoring}
KUBECTL=${KUBECTL:-kubectl}

prom() { $KUBECTL -n "$NS" exec deploy/prometheus -c prometheus -- wget -q -O- "$1" 2>/dev/null; }
am()   { $KUBECTL -n "$NS" exec deploy/alertmanager -- wget -q -O- "$1" 2>/dev/null; }

PY_RULES=$(cat <<'PY'
import json, sys
g = json.load(sys.stdin)["data"]["groups"]
print(f'  групп {len(g)}, правил {sum(len(x["rules"]) for x in g)}')
active = [(r.get("state", "?"), r["name"]) for x in g for r in x["rules"]
          if r.get("state") not in (None, "inactive")]
if not active:
    print("  все inactive")
for st, name in active:
    print(f'  {st:<9} {name}')
PY
)

PY_ALERTS=$(cat <<'PY'
import json, sys
a = json.load(sys.stdin)
if not a:
    print("  (пусто)")
for x in a:
    l = x.get("labels", {})
    st = x.get("status", {}).get("state", "?")
    print(f'  {st:<10} {l.get("alertname", "?"):<26} {l.get("severity", "")}')
PY
)

PY_COUNTERS=$(cat <<'PY'
import json, sys
label = sys.argv[1]
r = json.load(sys.stdin)["data"]["result"]
vals = {x["metric"].get("integration", "?"): x["value"][1] for x in r}
body = ", ".join(f'{k}={v}' for k, v in sorted(vals.items())) if vals else "нет данных"
print(f'  {label}: {body}')
PY
)

PY_WATCHDOG=$(cat <<'PY'
import json, sys
r = json.load(sys.stdin)["data"]["result"]
if not r:
    print("  SSWatchdog НЕ активен — сторож не работает, молчание канала снова")
    print("  неотличимо от здоровья. Проверить, загрузилось ли правило.")
else:
    print(f'  SSWatchdog {r[0]["metric"].get("alertstate")} — в канал уходит раз в 12 ч')
PY
)

echo "— правила ————————————————————————————————————————————————"
prom http://localhost:9090/api/v1/rules | python3 -c "$PY_RULES"

echo "— в Alertmanager —————————————————————————————————————————"
am http://localhost:9093/api/v2/alerts | python3 -c "$PY_ALERTS"

echo "— доставка ———————————————————————————————————————————————"
# Целиком путь Alertmanager -> ss-notifier -> Telegram: неудачей считается и
# недоступный ss-notifier, и его 502 «канал не принял».
prom 'http://localhost:9090/api/v1/query?query=sum%20by%20(integration)%20(alertmanager_notifications_total)' \
    | python3 -c "$PY_COUNTERS" "отправлено"
prom 'http://localhost:9090/api/v1/query?query=sum%20by%20(integration)%20(alertmanager_notifications_failed_total)' \
    | python3 -c "$PY_COUNTERS" "не удалось"

echo "— сторож —————————————————————————————————————————————————"
# SSWatchdog обязан быть firing ВСЕГДА: он и есть признак жизни канала.
prom 'http://localhost:9090/api/v1/query?query=ALERTS%7Balertname%3D%22SSWatchdog%22%7D' \
    | python3 -c "$PY_WATCHDOG"
