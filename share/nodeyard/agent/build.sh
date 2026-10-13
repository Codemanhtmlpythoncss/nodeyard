#!/bin/sh
# Builds the node agent (agent.c) as small static programs for the CPU types nodeyard runs on, with Docker:
#   sh share/nodeyard/agent/build.sh   ->   share/nodeyard/agent/bin/nodeyard-agent-{amd64,arm64,arm}
# (arm is ARMv6 hard-float, so it runs on every 32-bit Raspberry Pi OS.) No libraries: musl, linked statically.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$here/bin"
for arch in amd64 arm64 arm; do
    plat="linux/$arch"
    [ "$arch" = arm ] && plat="linux/arm/v6"
    docker run --rm --platform "$plat" -v "$here:/src" alpine:3.20 sh -c \
        "apk add --no-cache gcc musl-dev >/dev/null && gcc -Os -static -s -Wall -Wextra -o /src/bin/nodeyard-agent-$arch /src/agent.c"
    echo "built bin/nodeyard-agent-$arch ($(wc -c < "$here/bin/nodeyard-agent-$arch") bytes)"
done
