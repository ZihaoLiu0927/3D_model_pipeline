# syntax=docker/dockerfile:1

############################
# 基础层：Python 3.11 运行时
############################
# AWS 部署时，确保 EC2/Fargate 是 x86_64 架构。
# 如果使用 Graviton/ARM64，这个 x64 AppImage 不能运行。
ARG TARGETPLATFORM=linux/amd64
FROM --platform=${TARGETPLATFORM} python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    BLENDER_BIN=/usr/local/bin/blender \
    CURAENGINE_BIN=/usr/local/bin/CuraEngine \
    CURAENGINE_PROFILES_DIR=/app/app/profiles \
    CURAENGINE_RESOURCES_DIR=/app/app/profiles/cura_resources \
    CURAENGINE_DEFINITIONS_DIR=/app/app/profiles/cura_resources/definitions

############################
# 系统运行库 + 构建/解包工具
############################
RUN apt-get update && apt-get install -y --no-install-recommends \
    # common runtime
    ca-certificates \
    curl \
    wget \
    git \
    file \
    xz-utils \
    # AppImage / squashfs
    libarchive-tools \
    libfuse2 \
    squashfs-tools \
    # build tools used by Python deps / pymeshlab / trimesh stack
    build-essential \
    gcc \
    g++ \
    make \
    libeigen3-dev \
    # ELF patch/debug
    patchelf \
    # OpenGL / X / Qt-ish runtime libs sometimes needed by bpy/Cura extracted libs
    libgl1 \
    libglu1-mesa \
    libopengl0 \
    libxrender1 \
    libxrandr2 \
    libxi6 \
    libxkbcommon-x11-0 \
    libxcb-randr0 \
    libxcb-xinerama0 \
    libxcb-shape0 \
    libxcb-keysyms1 \
    libfontconfig1 \
    libgtk-3-0 \
    libpulse0 \
    libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

############################
# Blender 兼容入口
############################
# requirements.txt 安装的 bpy wheel 已包含 headless Blender Python 运行时。
# 这里保留 /usr/local/bin/blender，兼容：
# blender -b -P app/validate.py -- model.stl
############################
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -eu' \
    'SCRIPT=""' \
    'while [ "$#" -gt 0 ]; do' \
    '  case "$1" in' \
    '    -P)' \
    '      shift' \
    '      SCRIPT="$1"' \
    '      ;;' \
    '    --)' \
    '      shift' \
    '      break' \
    '      ;;' \
    '  esac' \
    '  shift' \
    'done' \
    'if [ -z "$SCRIPT" ]; then' \
    '  echo "Usage: blender -b -P script.py -- args..." >&2' \
    '  exit 2' \
    'fi' \
    'exec python "$SCRIPT" -- "$@"' \
    > /usr/local/bin/blender \
    && chmod +x /usr/local/bin/blender

############################
# 安装 CuraEngine 5.12.0
# 从 UltiMaker Cura AppImage 提取 CuraEngine + resources
############################
RUN set -eux; \
    df -h /tmp /opt; \
    curl --fail --location --retry 5 --retry-delay 2 --show-error \
    "https://github.com/Ultimaker/Cura/releases/download/5.12.0/UltiMaker-Cura-5.12.0-linux-X64.AppImage" \
    --output /tmp/cura.AppImage; \
    ls -lh /tmp/cura.AppImage; \
    \
    # AppImage 里可能有多个 hsqs magic，逐个尝试 unsquashfs。
    for OFFSET in $(LC_ALL=C grep -aob 'hsqs' /tmp/cura.AppImage | cut -d: -f1); do \
    rm -rf /opt/cura; \
    if unsquashfs -q -o "$OFFSET" -d /opt/cura /tmp/cura.AppImage; then \
    break; \
    fi; \
    done; \
    test -d /opt/cura; \
    rm -f /tmp/cura.AppImage; \
    \
    echo "Extracted Cura AppImage contents:"; \
    find /opt/cura -maxdepth 2 -type f -name 'CuraEngine' -o -name 'fdmprinter.def.json' | head -50

############################
# 复制完整 Cura resources
############################
# 不只复制 definitions，否则 CuraEngine 后续可能找不到 extruders/materials/quality/variants 等资源。
############################
RUN set -eux; \
    CURA_DEFS="$(find /opt/cura -name 'fdmprinter.def.json' -type f | head -1 | xargs dirname)"; \
    test -n "$CURA_DEFS"; \
    CURA_RESOURCES="$(dirname "$CURA_DEFS")"; \
    echo "Found Cura definitions at: $CURA_DEFS"; \
    echo "Found Cura resources at: $CURA_RESOURCES"; \
    mkdir -p /app/app/profiles/cura_resources; \
    cp -a "$CURA_RESOURCES"/. /app/app/profiles/cura_resources/; \
    test -f /app/app/profiles/cura_resources/definitions/fdmprinter.def.json; \
    find /app/app/profiles/cura_resources -maxdepth 2 -type d | sort | head -50

############################
# 修正 CuraEngine ELF interpreter
############################
# 只 patch CuraEngine 本体，不再批量 patch 全部 ELF。
############################
RUN set -eux; \
    INTERP="/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"; \
    test -e "$INTERP"; \
    CURA_BIN="$(find /opt/cura -name 'CuraEngine' -type f -perm /111 | sort | head -1)"; \
    test -n "$CURA_BIN"; \
    CURA_BIN_REAL="$(readlink -f "$CURA_BIN")"; \
    echo "CuraEngine found at: $CURA_BIN_REAL"; \
    file "$CURA_BIN_REAL"; \
    patchelf --print-interpreter "$CURA_BIN_REAL" || true; \
    patchelf --set-interpreter "$INTERP" "$CURA_BIN_REAL"; \
    patchelf --print-interpreter "$CURA_BIN_REAL"; \
    echo "$CURA_BIN_REAL" > /opt/cura/CuraEngine.path

############################
# CuraEngine wrapper
############################
# 关键：优先使用 AppImage 自带库，避免系统 libssl/libstdc++ 版本不够。
############################
RUN set -eux; \
    printf '%s\n' \
    '#!/bin/sh' \
    'set -eu' \
    'CURA_BIN="$(cat /opt/cura/CuraEngine.path)"' \
    'export LD_LIBRARY_PATH="/opt/cura:/opt/cura/runtime/compat:/opt/cura/runtime/compat/usr/lib/x86_64-linux-gnu:/opt/cura/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"' \
    'exec "$CURA_BIN" "$@"' \
    > /usr/local/bin/CuraEngine; \
    chmod +x /usr/local/bin/CuraEngine; \
    \
    echo "Checking AppImage bundled runtime libs:"; \
    find /opt/cura \
    \( -name 'libArcus.so*' \
    -o -name 'libpolyclipping.so*' \
    -o -name 'libtbb*.so*' \
    -o -name 'libssl.so.3' \
    -o -name 'libcrypto.so.3' \
    -o -name 'libstdc++.so.6' \) \
    -print; \
    \
    echo "Checking CuraEngine dynamic dependencies under wrapper LD_LIBRARY_PATH:"; \
    LD_LIBRARY_PATH="/opt/cura:/opt/cura/runtime/compat:/opt/cura/runtime/compat/usr/lib/x86_64-linux-gnu:/opt/cura/usr/lib/x86_64-linux-gnu" \
    ldd "$(cat /opt/cura/CuraEngine.path)" | tee /tmp/cura_ldd.txt; \
    if grep -q "not found" /tmp/cura_ldd.txt; then \
    echo "CuraEngine still has missing shared libraries:"; \
    grep "not found" /tmp/cura_ldd.txt; \
    exit 1; \
    fi; \
    \
    /usr/local/bin/CuraEngine help >/tmp/cura_help.txt 2>&1 || { \
    echo "CuraEngine failed to start"; \
    cat /tmp/cura_help.txt; \
    exit 1; \
    }; \
    echo "CuraEngine startup check passed"

############################
# Python 依赖
############################
WORKDIR /app

COPY requirements.txt .

RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

############################
# 复制业务代码 & 默认入口
############################
COPY app /app/app

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]