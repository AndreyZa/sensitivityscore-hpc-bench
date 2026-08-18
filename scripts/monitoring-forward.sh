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
# push-эндпоинт для ЛОКАЛЬНОГО ss-idrac-poller, наружу ему нельзя.
case "$SVC" in
    grafana)     PORT=3000; HEALTH=/api/health; DEF_ADDR=0.0.0.0 ;;   # отвечает без авторизации
    prometheus)  PORT=9090; HEALTH=/-/healthy;  DEF_ADDR=0.0.0.0 ;;
    pushgateway) PORT=9091; HEALTH=/-/healthy;  DEF_ADDR=127.0.0.1 ;;
    *) echo "использование: $0 <grafana|prometheus|pushgateway>"; exit 2 ;;
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
