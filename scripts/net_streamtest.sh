#!/bin/bash
# Прямой замер фазы СТРИМА (dd -> /dev/tcp) при квоте 500m: clean vs egress-шторм.
# Меряем скорость самой отправки (из stderr dd), без geant4-compute — чтобы
# увидеть чистый throttle стрима под сетевым штормом на реальной квоте жертвы.
set -u
export KUBECONFIG=$HOME/.kube/configs/timeweb-stage
NS=sensitivityscore-bench
TX_NODE=worker-192.168.0.9        # узел жертвы + egress-шторм
SINK_NODE=worker-192.168.0.8      # приёмник стрима (cross-node)
STORMDST_NODE=worker-192.168.0.10 # приёмник шторма
GEANT=andreyza/geant4:11.2
IPERF=networkstatic/iperf3
MB=${MB:-2048}
kubectl -n $NS delete pod nst-sink nst-tx nst-sdst nst-storm --ignore-not-found --wait=true >/dev/null 2>&1

cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: nst-sink, namespace: $NS, labels: {app: ss-netprobe}}
spec:
  nodeName: $SINK_NODE
  restartPolicy: Never
  containers:
  - name: sink
    image: python:3.12-slim
    command: ["python3","-u","-c"]
    args:
    - |
      import socket, threading
      s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
      s.bind(("0.0.0.0", 9000)); s.listen(128); print("sink up", flush=True)
      def drain(c):
          import contextlib
          with contextlib.suppress(Exception):
              while c.recv(1 << 20):
                  pass
          c.close()
      while True:
          c, _ = s.accept()
          threading.Thread(target=drain, args=(c,), daemon=True).start()
YAML
kubectl -n $NS wait --for=condition=Ready pod/nst-sink --timeout=120s >/dev/null
SINK_IP=$(kubectl -n $NS get pod nst-sink -o jsonpath='{.status.podIP}')
echo "sink=$SINK_IP"

streamrate() {  # $1=name -> печатает строку статистики dd (MB/s)
  kubectl -n $NS delete pod "$1" --ignore-not-found --wait=true >/dev/null 2>&1
  cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: $1, namespace: $NS, labels: {app: ss-netprobe}}
spec:
  nodeName: $TX_NODE
  restartPolicy: Never
  containers:
  - name: tx
    image: $GEANT
    imagePullPolicy: Always
    command: ["/bin/bash","-c"]
    args: ["dd if=/dev/zero bs=1M count=$MB > /dev/tcp/$SINK_IP/9000"]
    resources: {requests: {cpu: 500m, memory: 128Mi}, limits: {cpu: 500m, memory: 512Mi}}
YAML
  kubectl -n $NS wait --for=jsonpath='{.status.phase}'=Succeeded pod/"$1" --timeout=400s >/dev/null 2>&1
  kubectl -n $NS logs "$1" 2>&1 | grep -i "copied\|bytes" | tail -1
}

echo "=== A. ЧИСТЫЙ стрим ${MB}МБ (квота 500m), $TX_NODE -> $SINK_NODE ==="
CLEAN=$(streamrate nst-tx); echo "  $CLEAN"

echo "=== B. Поднимаю egress-шторм на $TX_NODE ($TX_NODE -> $STORMDST_NODE, TCP -P8) ==="
cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: nst-sdst, namespace: $NS, labels: {app: ss-aggressor}}
spec:
  nodeName: $STORMDST_NODE
  restartPolicy: Never
  containers: [{name: iperf3, image: $IPERF, args: ["-s"], resources: {requests: {cpu: 300m, memory: 128Mi}, limits: {cpu: "2", memory: 256Mi}}}]
YAML
kubectl -n $NS wait --for=condition=Ready pod/nst-sdst --timeout=120s >/dev/null
SDST_IP=$(kubectl -n $NS get pod nst-sdst -o jsonpath='{.status.podIP}')
cat <<YAML | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: nst-storm, namespace: $NS, labels: {app: ss-aggressor}}
spec:
  nodeName: $TX_NODE
  restartPolicy: Never
  containers:
  - name: iperf3
    image: $IPERF
    command: ["/bin/sh","-c"]
    args: ["while true; do iperf3 -c $SDST_IP -P 8 -t 3600 >/dev/null 2>&1; sleep 2; done"]
    resources: {requests: {cpu: 100m, memory: 64Mi}, limits: {cpu: "2", memory: 512Mi}}
YAML
kubectl -n $NS wait --for=condition=Ready pod/nst-storm --timeout=120s >/dev/null
sleep 10
echo "=== C. Стрим ${MB}МБ ПОД ШТОРМОМ (та же квота 500m) ==="
STORM=$(streamrate nst-tx2); echo "  $STORM"

kubectl -n $NS delete pod -l app=ss-netprobe --wait=false >/dev/null 2>&1
kubectl -n $NS delete pod -l app=ss-aggressor --wait=false >/dev/null 2>&1
echo ""
echo "clean:  $CLEAN"
echo "storm:  $STORM"
python3 - "$CLEAN" "$STORM" <<'EOF'
import re,sys
def mbps(s):
    m=re.search(r'([\d.]+)\s*([KMG]?B)/s', s)
    if not m: return float('nan')
    v=float(m.group(1)); u=m.group(2)
    return v*{'B':1e-6,'KB':1e-3,'MB':1,'GB':1e3}[u]
c,s=mbps(sys.argv[1]),mbps(sys.argv[2])
print(f"\n=== стрим-фаза: чисто {c:.0f} -> шторм {s:.0f} MB/s; throttle x{c/s:.2f} ===")
print("cˢ_net реален на квоте 500m -> серию гнать (стрим сетебоунд)" if c/s>1.8
      else "throttle слаб на 500m -> стрим упирается в CPU/квоту, не в сеть (как кэш) -> net = прод")
EOF
