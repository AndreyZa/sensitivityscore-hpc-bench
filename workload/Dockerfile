# syntax=docker/dockerfile:1.7
#
# Geant4 workload image with a controllable sensitivity profile (S = {LLC, NUMA, Net, IO}).
# See docs/Технический_план_экспериментов.md §1.1–1.2 for the rationale.
#
# Multistage build: heavy toolchain (CMake, compilers, Geant4 source build) stays in the
# build stage; the final image only ships the installed Geant4 runtime + data libraries.

FROM cern/geant4:11.2 AS build
# The CERN base image already ships physics-list datasets. If a pinned/custom Geant4
# version is required instead, replace this stage with a from-source build:
#
#   RUN cmake -S /src/geant4 -B /build \
#         -DGEANT4_INSTALL_DATA=ON \
#         -DGEANT4_BUILD_MULTITHREADED=ON \
#         -DGEANT4_USE_GDML=ON \
#       && cmake --build /build -j"$(nproc)" \
#       && cmake --install /build --prefix /usr/local/geant4

FROM debian:bookworm-slim

ENV G4DATA=/usr/local/geant4/share/Geant4/data \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        libstdc++6 \
        libexpat1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/geant4 /usr/local/geant4

# Reference application: examples/extended/electromagnetic/TestEm5 (or hadronic/Hadr03)
# already gives a realistic cross-section-table access profile without writing a
# bespoke Geant4 application from scratch (see plan §1.1).
COPY --from=build /usr/local/geant4/bin/TestEm5 /usr/local/bin/geant4-app

COPY entrypoint.sh /entrypoint.sh
COPY macros/ /macros/
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
