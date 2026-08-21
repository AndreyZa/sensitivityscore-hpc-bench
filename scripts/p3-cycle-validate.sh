#!/bin/bash
# Один надзорный цикл гашения одного узла: off -> подтверждение Off ->
# подъём по появлению работы -> Ready -> возврат в планирование.
#
# Зачем отдельным скриптом. Это последнее, что в цепочке P3 не проверено
# на живом железе: логика политики прогнана против прод-кластера сухим
# режимом (узлы видны, решение принимается), но путь ЗАПИСИ — реальный
# Redfish-вызов из контроллера — обкатан только на эмуляторе sushy.
# Прогон даёт сразу и валидацию, и материал Э3.3: окна cycle-off и
# cycle-boot пишутся в ClickHouse под меткой p3-cycle-validate, откуда их
# читает analysis/energy_metrics.py --cycle.
#
# Гасится ТОЛЬКО указанный узел: остальные перечислены в --suspend-exc.
# Подъём вызывается штатным путём политики — появлением работы, которую
# некуда поставить, — а не ручной командой: проверять надо тот механизм,
# который будет работать в Ш8.
#
# Если что-то пошло не так и узел остался выключенным, возврат руками:
#   curl -sk -K - -X POST https://<bmc>/redfish/v1/Systems/System.Embedded.1/\
#        Actions/ComputerSystem.Reset -H 'Content-Type: application/json' \
#        -d '{"ResetType":"On"}'   <<< 'user = "root:<пароль из файла>"'
#   kubectl uncordon <узел>
#
# Запуск С ЛАБЫ (там kubeconfig прода, файл пароля и маршрут до BMC):
#   scripts/p3-cycle-validate.sh wrk-b8
set -u
NODE=${1:?укажи узел, например wrk-b8}
KUBECONFIG=${KUBECONFIG:-$HOME/.kube/configs/prod}; export KUBECONFIG
K="kubectl --kubeconfig $KUBECONFIG"
MAP="wrk-b6=10.21.200.106,wrk-b7=10.21.200.107,wrk-b8=10.21.200.108"
EXC=$(echo "$MAP" | tr ',' '\n' | cut -d= -f1 | grep -v "^$NODE$" | paste -sd,)
LOG=/tmp/p3-cycle-$NODE.log
cd "$(dirname "$0")/.." || exit 1

nohup $K -n sensitivityscore-system port-forward svc/clickhouse 8124:8123 \
    >/tmp/pf-ch.log 2>&1 &
sleep 5

nohup python3 -u scripts/power-save.py \
    --executor redfish --idrac-map "$MAP" --suspend-exc "$EXC" \
    --suspend-time 45 --interval 15 --resume-timeout 600 \
    --record-windows --run-label p3-cycle-validate --ch-port 8124 \
    > "$LOG" 2>&1 &
CTL=$!
echo "контроллер pid $CTL, лог $LOG; гасим только $NODE (исключены: $EXC)"

for _ in $(seq 1 24); do grep -q "гашение $NODE" "$LOG" && break; sleep 10; done
if ! grep -q "гашение $NODE" "$LOG"; then
    echo "гашение не началось за 4 минуты — смотри $LOG"; kill $CTL; exit 1
fi
echo "--- гашение пошло, жду подтверждения Off ---"
sleep 45
$K get node "$NODE" --no-headers

echo "--- создаю работу, которую некуда поставить: штатный триггер подъёма ---"
$K apply -f - <<POD >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: wake-trigger, namespace: sensitivityscore-bench}
spec:
  containers:
    - name: pause
      image: registry.k8s.io/pause:3.9
      resources: {requests: {cpu: "200"}}
POD

# Ждём НЕ сообщения о начале подъёма, а фактической готовности узла.
# Первый прогон (21.08.2026) ждал строку «подъём» и через 30 с убивал
# контроллер — а загрузка занимает около трёх минут, так что kill обрывал
# wait_ready и последующий uncordon: узел остался Ready, но закордоненным,
# и возвращать его пришлось руками. Контроллер обязан довести цикл сам.
echo "--- жду готовности $NODE (загрузка около 3 минут) ---"
for _ in $(seq 1 60); do
    st=$($K get node "$NODE" --no-headers 2>/dev/null | awk '{print $2}')
    echo "  $(date +%H:%M:%S) $NODE: ${st:-нет ответа}"
    [ "$st" = "Ready" ] && break
    sleep 15
done
$K delete pod wake-trigger -n sensitivityscore-bench --ignore-not-found >/dev/null
st=$($K get node "$NODE" --no-headers 2>/dev/null | awk '{print $2}')
if [ "$st" != "Ready" ]; then
    echo "ВНИМАНИЕ: $NODE так и не вернулся в строй ($st) — контроллер НЕ убиваю,"
    echo "          он ещё в бюджете подъёма; смотри $LOG и состояние узла."
    exit 1
fi
kill $CTL 2>/dev/null
pkill -f "port-forward svc/clickhouse"

echo "=== лог контроллера ==="; cat "$LOG"
echo "=== состояние узла ==="; $K get node "$NODE" --no-headers
echo
echo "окна цикла: analysis/energy_metrics.py --run-label p3-cycle-validate --cycle"
