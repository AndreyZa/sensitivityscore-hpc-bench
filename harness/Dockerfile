# harness/Dockerfile — lets run_experiment.py run FROM INSIDE the cluster, so
# it reaches Redis via the normal in-cluster DNS name
# (redis.sensitivityscore-system.svc.cluster.local) instead of needing
# kubectl port-forward + REDIS_ADDR from a host machine outside the cluster
# (see harness/README.md's former "Redis при локальном запуске" section —
# port-forward kept dying mid-run and needed a restart-loop babysitting it).
#
# k8s_submit.py shells out to the kubectl binary directly (not just the
# Python kubernetes client), so it has to be on PATH here too.
FROM python:3.12-slim

ARG KUBECTL_VERSION=v1.35.0
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && apt-get purge -y curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["python", "run_experiment.py"]
CMD ["--pilot"]
