FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod go.sum* ./
RUN go mod download
# Только исходники сборки — правки deploy/-манифестов и README не должны
# инвалидировать слой и требовать пересборки бинаря.
COPY cmd/ cmd/
COPY pkg/ pkg/
RUN CGO_ENABLED=0 go build -o /out/metrics-agent ./cmd/agent

# Not distroless: virsh capabilities (vpmu healthcheck, config B) shells out to a
# host binary, and we want a minimal-but-debuggable base for a privileged-ish
# DaemonSet during the experiment phase.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/metrics-agent /metrics-agent
ENTRYPOINT ["/metrics-agent"]
