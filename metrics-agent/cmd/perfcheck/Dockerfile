FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod go.sum* ./
RUN go mod download
# Только исходники — как в основном metrics-agent/Dockerfile.
COPY cmd/ cmd/
COPY pkg/ pkg/
RUN CGO_ENABLED=0 go build -o /out/perfcheck ./cmd/perfcheck

FROM debian:bookworm-slim
COPY --from=build /out/perfcheck /perfcheck
ENTRYPOINT ["/perfcheck"]
