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

echo "[bootstrap] applying scheduler weights ConfigMap"
kubectl apply -f k8s/scheduler-config/weights-configmap.yaml

echo "[bootstrap] done. Next steps:"
echo "  1. Build & push scheduler-plugin and metrics-agent images"
echo "  2. Deploy the second scheduler profile using k8s/scheduler-config/scheduler-config.yaml"
echo "  3. kubectl apply -f metrics-agent/deploy/daemonset.yaml"
echo "  4. Sanity-check with harness --pilot (see harness/README.md)"
