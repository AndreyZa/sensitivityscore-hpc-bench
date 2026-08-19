#!/usr/bin/env bash
# Одна картинка про оповещения: правила, активные алерты и СОСТОЯНИЕ ДОСТАВКИ.
#
# Отдельным скриптом, а не строчкой в Makefile: три источника (Prometheus,
# Alertmanager, счётчики доставки) в одну команду ужимаются только ценой
# нечитаемых однострочников на python — а именно этой командой будут проверять
# канал перед запуском длинной серии.
set -euo pipefail

NS=${MONITORING_NAMESPACE:-sensitivityscore-monitoring}
KUBECTL=${KUBECTL:-kubectl}

prom() { $KUBECTL -n "$NS" exec deploy/prometheus -c prometheus -- wget -q -O- "$1" 2>/dev/null; }
am()   { $KUBECTL -n "$NS" exec deploy/alertmanager -- wget -q -O- "$1" 2>/dev/null; }

echo "— правила ————————————————————————————————————————————————"
prom http://localhost:9090/api/v1/rules | python3 -c '
import json, sys
g = json.load(sys.stdin)["data"]["groups"]
print(f"  групп {len(g)}, правил {sum(len(x[\"rules\"]) for x in g)}")
active = [(r["state"], r["name"]) for x in g for r in x["rules"] if r.get("state") != "inactive"]
if not active:
    print("  все inactive")
for st, name in active:
    print(f"  {st:<9} {name}")
'

echo "— в Alertmanager —————————————————————————————————————————"
am http://localhost:9093/api/v2/alerts | python3 -c '
import json, sys
a = json.load(sys.stdin)
if not a:
    print("  (пусто)")
for x in a:
    l = x.get("labels", {})
    print(f"  {x[\"status\"][\"state\"]:<10} {l.get(\"alertname\",\"?\"):<26} {l.get(\"severity\",\"\")}")
'

echo "— доставка ———————————————————————————————————————————————"
# Целиком путь Alertmanager -> ss-notifier -> Telegram: неудачей считается и
# недоступный ss-notifier, и его 502 «канал не принял».
for metric in alertmanager_notifications_total alertmanager_notifications_failed_total; do
    label=$([ "$metric" = alertmanager_notifications_total ] && echo "отправлено" || echo "не удалось")
    prom "http://localhost:9090/api/v1/query?query=sum%20by%20(integration)%20($metric)" \
        | python3 -c '
import json, sys
label = sys.argv[1]
r = json.load(sys.stdin)["data"]["result"]
vals = {x["metric"].get("integration", "?"): x["value"][1] for x in r}
print(f"  {label}: " + (", ".join(f"{k}={v}" for k, v in sorted(vals.items())) if vals else "нет данных"))
' "$label"
done

echo "— сторож —————————————————————————————————————————————————"
# SSWatchdog обязан быть firing ВСЕГДА: он и есть признак жизни канала.
prom 'http://localhost:9090/api/v1/query?query=ALERTS%7Balertname%3D%22SSWatchdog%22%7D' | python3 -c '
import json, sys
r = json.load(sys.stdin)["data"]["result"]
if not r:
    print("  SSWatchdog НЕ активен — правило не загружено; сторож не работает")
else:
    print(f"  SSWatchdog {r[0][\"metric\"].get(\"alertstate\")} (в канал уходит раз в 12 ч)")
'
