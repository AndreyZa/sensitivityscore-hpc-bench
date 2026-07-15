#!/bin/bash
# Тормозит ли СЕТЕВОЙ шторм ПРОПУСКНУЮ СПОСОБНОСТЬ cross-node стрима соседа?
# Аналог disk_probe для сети. Проверяем ФИЗИКУ до серии cˢ_net:
#   - жертва стримит cross-node  SRC -> SINK  (iperf3 TCP, меряем Mbps);
#   - шторм = iperf3-клиент НА ТОМ ЖЕ узле SRC, но egress на ТРЕТИЙ узел
#     STORMDST -> насыщает uplink узла SRC (в отличие от боевого mode:net,
#     где пара сидит на одном узле и трафик мостится через veth, uplink не
#     трогая — поэтому cross-node стрим он не тормозит).
# Если clean/storm > ~1.5 — сетевая контенция на uplink реальна, cˢ_net
# измерим, и серию надо гонять со ШТОРМОМ-egress (не veth-парой).
set -u
export KUBECONFIG=$HOME/.kube/configs/timeweb-stage
NS=sensitivityscore-bench
SRC=worker-192.168.0.8       # узел жертвы И шторма (общий TX uplink)
SINK=worker-192.168.0.10     # сервер-приёмник измерения (cross-node)
STORMDST=worker-192.168.0.9  # сервер-приёмник шторма (третий узел)
IMG=networkstatic/iperf3
kubectl create ns $NS --dry-run=client -o yaml | kubectl apply -f - >/dev/null 2>&1

server() {  # $1=name $2=node
  kubectl -n $NS delete pod "$1" --ignore-not-found --wait=false >/dev/null 2>&1
  cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: $1, namespace: $NS, labels: {app: ss-netprobe}}
spec:
  nodeName: $2
  restartPolicy: Never
  containers:
  - {name: iperf3, image: $IMG, args: ["-s"], resources: {requests: {cpu: 300m, memory: 128Mi}, limits: {cpu: "2", memory: 256Mi}}}
YAML
}

# Измерение: iperf3-клиент SRC -> сервер $1, TCP, -t $2 c, вернуть Mbps (receiver).
measure() {  # $1=server_svc_ip $2=secs $3=name
  kubectl -n $NS delete pod "$3" --ignore-not-found >/dev/null 2>&1
  cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: $3, namespace: $NS, labels: {app: ss-netprobe}}
spec:
  nodeName: $SRC
  restartPolicy: Never
  containers:
  - name: iperf3
    image: $IMG
    command: ["/bin/sh","-c"]
    args: ["i=0; while [ \$i -lt 30 ]; do iperf3 -c $1 -t 1 >/dev/null 2>&1 && break; i=\$((i+1)); sleep 2; done; exec iperf3 -c $1 -t $2 --json"]
    resources: {requests: {cpu: 300m, memory: 128Mi}, limits: {cpu: "2", memory: 256Mi}}
YAML
  kubectl -n $NS wait --for=jsonpath='{.status.phase}'=Succeeded pod/"$3" --timeout=200s >/dev/null 2>&1
  kubectl -n $NS logs "$3" 2>/dev/null | python3 -c "
import sys,json
s=sys.stdin.read(); i=s.find('{')
try: print(f\"{json.loads(s[i:])['end']['sum_received']['bits_per_second']/1e6:.0f}\")
except Exception as e: print('ERR')"
}

echo '=== net_probe: тормозит ли сетевой шторм cross-node стрим? ==='
echo "поднимаю серверы: измерение на $SINK, шторм-приёмник на $STORMDST"
server np-sink  $SINK
server np-sdst  $STORMDST
kubectl -n $NS wait --for=condition=Ready pod/np-sink pod/np-sdst --timeout=120s >/dev/null
SINK_IP=$(kubectl -n $NS get pod np-sink -o jsonpath='{.status.podIP}')
SDST_IP=$(kubectl -n $NS get pod np-sdst -o jsonpath='{.status.podIP}')
echo "  sink=$SINK_IP  stormdst=$SDST_IP"

echo "1) ЧИСТЫЙ стрим $SRC -> $SINK (10с):"
CLEAN=$(measure "$SINK_IP" 10 np-meas-clean); echo "   ${CLEAN} Mbps"
kubectl -n $NS delete pod np-meas-clean --ignore-not-found --wait=false >/dev/null 2>&1

echo "2) поднимаю СЕТЕВОЙ ШТОРМ на $SRC (egress $SRC -> $STORMDST, 8 параллельных TCP)..."
kubectl -n $NS delete pod np-storm --ignore-not-found >/dev/null 2>&1
cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: np-storm, namespace: $NS, labels: {app: ss-netprobe}}
spec:
  nodeName: $SRC
  restartPolicy: Never
  containers:
  - name: iperf3
    image: $IMG
    command: ["/bin/sh","-c"]
    args: ["exec iperf3 -c $SDST_IP -P 8 -t 3600 >/dev/null 2>&1"]
    resources: {requests: {cpu: 100m, memory: 64Mi}, limits: {cpu: "2", memory: 512Mi}}
YAML
kubectl -n $NS wait --for=condition=Ready pod/np-storm --timeout=120s >/dev/null
sleep 8
echo "3) стрим $SRC -> $SINK ПОД ШТОРМОМ (10с):"
STORM=$(measure "$SINK_IP" 10 np-meas-storm); echo "   ${STORM} Mbps"

kubectl -n $NS delete pod -l app=ss-netprobe --wait=false >/dev/null 2>&1
python3 -c "
c,s=float('${CLEAN}' if '${CLEAN}'!='ERR' else 'nan'), float('${STORM}' if '${STORM}'!='ERR' else 'nan')
print(f'\n=== стрим: чисто {c:.0f} -> шторм {s:.0f} Mbps; throughput-замедление x{c/s:.2f} ===')
print('сетевой шторм РЕЖЕТ cross-node стрим соседа -> cˢ_net измерим (гнать серию с egress-штормом)' if c/s>1.5
      else 'uplink не делится/шторм слаб -> cross-node egress-контенция тут не проявляется; cˢ_net на этом стенде не показать')"
