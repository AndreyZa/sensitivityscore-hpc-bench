#!/usr/bin/env bash
# scripts/bootstrap-cluster.sh — one-shot setup of the sensitivityscore-system
# namespace + in-cluster Redis, before deploying the scheduler plugin / metrics
# agent (see scheduler-plugin/README.md, metrics-agent/README.md).
set -euo pipefail

NAMESPACE="sensitivityscore-system"

echo "[bootstrap] creating namespace ${NAMESPACE}"
kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

echo "[bootstrap] deploying in-cluster Redis (metrics backend, docs §3.2)"
kubectl apply -n "${NAMESPACE}" -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: redis
spec:
  replicas: 1
  selector:
    matchLabels: {app: redis}
  template:
    metadata:
      labels: {app: redis}
    spec:
      containers:
        - name: redis
          image: redis:7-alpine
          ports:
            - containerPort: 6379
          resources:
            requests: {cpu: "100m", memory: "128Mi"}
            limits: {cpu: "500m", memory: "256Mi"}
---
apiVersion: v1
kind: Service
metadata:
  name: redis
spec:
  selector: {app: redis}
  ports:
    - port: 6379
      targetPort: 6379
EOF

echo "[bootstrap] done. Namespace + Redis ready (Redis пока не подключён к"
echo "scheduler-плагину — это отдельный агент metrics-agent, см. его README)."
echo "Next steps:"
echo "  1. Собрать образ плагина в форке scheduler-plugins: make scheduler-plugin-image"
echo "  2. Задеплоить: make scheduler-apply-config scheduler-deploy"
echo "  3. (опционально) kubectl apply -f metrics-agent/deploy/daemonset.yaml"
echo "  4. Sanity-check: make pilot (см. harness/README.md)"
