#!/usr/bin/env bash
# monitoring-forward.sh <grafana|prometheus> — постоянный проброс UI мониторинга
# с хоста, который держат включённым (JumpHost .72, позже PROD).
#
# Зовётся из ss-forward@.service и больше ниоткуда. Разовый проброс на время
# работы — это `make monitoring-open`, он никуда не делся.
#
# Почему не `nohup kubectl port-forward &`. Так и было: два форварда прожили
# сутки и умерли бы молча вместе с первой перезагрузкой. Хуже, что
# port-forward умеет ломаться НЕ УМИРАЯ — процесс жив, сокет слушает, соединения
# принимаются, а туннель уже мёртв. На этот случай мало Restart=always: сам
# факт «процесс жив» ничего не значит, нужна проба насквозь.
#
# Поэтому здесь: kubectl в фоне + проба через сам форвард (curl на localhost
# идёт по туннелю до сервиса). Три провала подряд — выходим ненулевым кодом,
# systemd поднимает заново. Один провал не считаем: API стенда моргает, и
# перезапускать туннель на каждое моргание значило бы рвать открытую вкладку.
set -u

SVC=${1:-}
# DEF_ADDR: grafana/prometheus пробрасываются в домашнюю сеть (смысл юнита —
# видимость с других машин; Grafana требует логин, Prometheus выставлять
# осознанно). Pushgateway — строго localhost: это неаутентифицированный
# push-эндпоинт, наружу ему нельзя.
#
# ВНИМАНИЕ, ss-forward@pushgateway НЕ выведен вместе с лабным поллером iDRAC.
# Его клиент сменился: поллер 19.08.2026 уехал в кластер и толкает по
# ClusterIP, но в тот же день появился МАРКЕР СЕРИИ — series_marker() из
# scripts/run-series.sh пишет ss_series_running/ss_series_heartbeat_seconds на
# http://127.0.0.1:9091 с того хоста, откуда гонят серию. Без этого форварда
# маркер не доедет, а run-series продолжит серию (маркер по построению не
# валит прогон): пропадут аннотации начала/конца серии на дашбордах и
# сработает SSSeriesMarkerStale — то есть тихо испортится разбор прогона.
#
# ss-notifier — тоже строго localhost, и по более острому доводу: за этим
# портом стоит бот, пишущий в ГРУППОВОЙ чат. Токен приёма — граница
# безопасности, но выставлять такой эндпоинт в домашнюю сеть незачем: его
# единственный клиент здесь — notify() из scripts/run-series.sh на этом же
# хосте. Проброс появился 19.08.2026, когда служба уехала в кластер: до того
# она крутилась на .72 контейнером, и SS_NOTIFY_URL=http://127.0.0.1:8790 в
# harness/.notify.env указывал прямо на неё. Без этого юнита тот же адрес
# перестал отвечать, а notify() по построению молчит при недоступной службе —
# то есть уведомления о зависшей серии пропали бы БЕЗ ЕДИНОГО следа. Ровно
# та беда, ради которой ss-notifier и написан.
case "$SVC" in
    grafana)     PORT=3000; HEALTH=/api/health; DEF_ADDR=0.0.0.0 ;;   # отвечает без авторизации
    prometheus)  PORT=9090; HEALTH=/-/healthy;  DEF_ADDR=0.0.0.0 ;;
    pushgateway) PORT=9091; HEALTH=/-/healthy;  DEF_ADDR=127.0.0.1 ;;
    ss-notifier) PORT=8790; HEALTH=/healthz;    DEF_ADDR=127.0.0.1 ;;
    *) echo "использование: $0 <grafana|prometheus|pushgateway|ss-notifier>"; exit 2 ;;
esac

NS=${MONITORING_NAMESPACE:-sensitivityscore-monitoring}
ADDRESS=${FORWARD_ADDRESS:-$DEF_ADDR}
PROBE_INTERVAL=${PROBE_INTERVAL:-30}
PROBE_FAILURES=${PROBE_FAILURES:-3}

child=
cleanup() { [ -n "$child" ] && kill "$child" 2>/dev/null; }
trap 'cleanup; exit 0' TERM INT

kubectl -n "$NS" port-forward --address "$ADDRESS" "svc/$SVC" "$PORT:$PORT" &
child=$!
sleep 3   # дать сокету забиндиться, иначе первая же проба провалится зря

fails=0
while :; do
    if ! kill -0 "$child" 2>/dev/null; then
        echo "port-forward $SVC завершился сам — выходим, systemd поднимет заново"
        exit 1
    fi
    if curl -sf -o /dev/null --max-time 5 "http://127.0.0.1:$PORT$HEALTH"; then
        fails=0
    else
        fails=$((fails + 1))
        echo "проба $SVC не прошла ($fails из $PROBE_FAILURES): http://127.0.0.1:$PORT$HEALTH"
        if [ "$fails" -ge "$PROBE_FAILURES" ]; then
            echo "туннель $SVC мёртв — перезапуск"
            cleanup
            exit 1
        fi
    fi
    sleep "$PROBE_INTERVAL"
done
