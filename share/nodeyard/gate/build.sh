#!/bin/sh
# Builds the model gate (gate.c) as small static programs for the CPU types nodeyard runs on, with Docker:
#   sh share/nodeyard/gate/build.sh   ->   share/nodeyard/gate/bin/nodeyard-gate-{amd64,arm64,arm}
# amd64 and arm64 link musl statically (an Alpine image that already has gcc); arm links glibc statically for ARMv5,
# so it runs on every 32-bit Arm board (its name lookups use glibc's built-in files and DNS resolvers: no NSS modules).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$here/bin"
for arch in amd64 arm64; do
    docker run --rm --platform "linux/$arch" -v "$here:/src" rust:1-alpine3.20 \
        gcc -Os -static -s -pthread -Wall -Wextra -o "/src/bin/nodeyard-gate-$arch" /src/gate.c
    echo "built bin/nodeyard-gate-$arch ($(wc -c < "$here/bin/nodeyard-gate-$arch") bytes)"
done
docker run --rm --platform linux/arm/v5 -v "$here:/src" gcc:14 \
    gcc -Os -static -s -pthread -Wall -Wextra -o /src/bin/nodeyard-gate-arm /src/gate.c
echo "built bin/nodeyard-gate-arm ($(wc -c < "$here/bin/nodeyard-gate-arm") bytes)"
