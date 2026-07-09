# syntax=docker/dockerfile:1.7
#
# Geant4 workload image, built FROM SOURCE per the official Geant4
# installation guide:
# https://geant4.web.cern.ch/documentation/dev/ig_html/InstallationGuide/containers.html
#
# IMPORTANT: there is no official prebuilt `cern/geant4` image on Docker Hub.
# An earlier version of this Dockerfile incorrectly assumed one existed
# (unverified guess) — confirmed wrong when the build failed with
# "pull access denied, repository does not exist". This version compiles
# the Geant4 toolkit + the TestEm5 benchmark example (docs §1.1:
# "examples/extended/electromagnetic/TestEm5 ... даёт реалистичный профиль
# cross-section lookups") from source on ubuntu:22.04, matching the
# structure of the official recipe.
#
# Build takes a while (compiling the full toolkit) — this is the tradeoff
# for not depending on an unofficial/community-maintained base image whose
# internal layout we can't verify offline.

FROM ubuntu:22.04 AS build

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake wget ca-certificates \
    libxerces-c-dev libexpat1-dev \
    && rm -rf /var/lib/apt/lists/*

ENV G4INSTALL=/opt/geant4
ARG GEANT4_VERSION=11.2.2

WORKDIR /opt
RUN wget -q https://github.com/Geant4/geant4/archive/refs/tags/v${GEANT4_VERSION}.tar.gz \
    && tar -xf v${GEANT4_VERSION}.tar.gz \
    && mv geant4-${GEANT4_VERSION} geant4-source \
    && rm v${GEANT4_VERSION}.tar.gz

# Toolkit build+install. GEANT4_INSTALL_DATA=ON pulls in the physics-list
# datasets (cross-section tables etc.) needed at runtime — the basis of the
# LLC-sensitivity story in docs §1.1/§2 (large, irregularly-accessed tables).
# GEANT4_BUILD_MULTITHREADED=ON is required for /run/numberOfThreads to mean
# anything (see entrypoint.sh) — NUMA-sensitivity story in docs §1.2.
RUN cmake -S geant4-source -B geant4-build \
    -DCMAKE_INSTALL_PREFIX=${G4INSTALL} \
    -DGEANT4_BUILD_MULTITHREADED=ON \
    -DGEANT4_USE_GDML=ON \
    -DGEANT4_USE_SYSTEM_EXPAT=ON \
    -DGEANT4_INSTALL_DATA=ON \
    && cmake --build geant4-build -- -j"$(nproc)" \
    && cmake --install geant4-build

# Example sources are NOT compiled as part of the toolkit install above —
# they're a separate small cmake project built against the just-installed
# Geant4 (standard Geant4 convention: users build examples themselves).
# Geant4's CMake package config lives at <prefix>/lib/Geant4-<version>/.
#
# WORKDIR is /opt (set above, before downloading geant4-source) — so the
# relative build dir "testem5-build" ends up at /opt/testem5-build, NOT at
# filesystem root. An earlier version of this Dockerfile's COPY instruction
# referenced /testem5-build (missing the /opt prefix) and failed with
# "not found"; confirmed the real path via a temporary debug `find` step
# before fixing the COPY line below.
RUN cmake -S geant4-source/examples/extended/electromagnetic/TestEm5 -B testem5-build \
    -DGeant4_DIR=${G4INSTALL}/lib/Geant4-${GEANT4_VERSION} \
    && cmake --build testem5-build -- -j"$(nproc)"

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
# libxerces-c-dev / libexpat1-dev (not just the bare runtime libs): the
# toolkit was built with GEANT4_USE_SYSTEM_EXPAT=ON, so geant4-app links
# against the system libexpat.so.1 at runtime — missed in an earlier
# version of this stage (ran fine at build time since build/ has it, but
# crashed at container start with "libexpat.so.1: cannot open shared
# object file"). Installing -dev packages here (not just bare runtime
# packages) sidesteps guessing exact VERSIONED runtime package names, which
# have shifted between Ubuntu releases (e.g. the libxerces-c3.2 vs.
# libxerces-c3.2t64 rename after the time64 ABI transition) — -dev packages
# reliably pull in the matching runtime as a transitive dependency.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxerces-c-dev libexpat1-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV G4INSTALL=/opt/geant4 \
    G4DATA=/opt/geant4/share/Geant4/data \
    LD_LIBRARY_PATH=/opt/geant4/lib

COPY --from=build /opt/geant4 /opt/geant4
COPY --from=build /opt/testem5-build/TestEm5 /usr/local/bin/geant4-app

COPY entrypoint.sh /entrypoint.sh
COPY macros/ /macros/
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
