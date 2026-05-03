# syntax=docker/dockerfile:1

############################
#  基础层：Python 3.11 运行时
############################
# 【注意】AWS 部署时，确保你的计算实例（EC2/Fargate）是 x86_64 架构。
# 如果使用 Graviton (ARM64) 实例，这个 amd64 的 AppImage 将无法运行。
ARG TARGETPLATFORM=linux/amd64
FROM --platform=${TARGETPLATFORM} python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    BLENDER_BIN=/usr/local/bin/blender \
    CURAENGINE_BIN=/usr/local/bin/CuraEngine \
    CURAENGINE_PROFILES_DIR=/app/app/profiles \
    CURAENGINE_DEFINITIONS_DIR=/app/app/profiles/definitions

############################
#  系统运行库 + 构建工具
############################
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglu1-mesa libqt5widgets5 qtbase5-dev \
    libxrender1 libxrandr2 libxi6 libopengl0 \
    libarchive-tools libfuse2 squashfs-tools \
    build-essential gcc g++ make libeigen3-dev \
    patchelf \
    curl wget git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libfontconfig1 \
    libgtk-3-0 \
    libxkbcommon-x11-0 \
    libxcb-randr0 \
    libxcb-xinerama0 \
    libxcb-shape0 \
    libxcb-keysyms1 \
    libpulse0 \
    libsqlite3-0 \
    libwebkit2gtk-4.1-0 \
    && rm -rf /var/lib/apt/lists/*

############################
#  Blender 兼容入口
############################
# requirements.txt 安装的 bpy wheel 已包含 headless Blender Python 运行时。
# 这里保留 /usr/local/bin/blender 这个入口名，兼容现有代码里的
# `blender -b -P app/validate.py -- model.stl` 调用，但不再解压完整 Blender 包。
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
#  安装 CuraEngine 5.12.0（从官方 AppImage 提取）
############################
RUN set -eux; \
    df -h /tmp /opt; \
    curl --fail --location --retry 5 --retry-delay 2 --show-error \
    "https://github.com/Ultimaker/Cura/releases/download/5.12.0/UltiMaker-Cura-5.12.0-linux-X64.AppImage" \
    --output /tmp/cura.AppImage; \
    ls -lh /tmp/cura.AppImage; \
    for OFFSET in $(LC_ALL=C grep -aob 'hsqs' /tmp/cura.AppImage | cut -d: -f1); do \
    rm -rf /opt/cura; \
    if unsquashfs -q -o "$OFFSET" -d /opt/cura /tmp/cura.AppImage; then \
    break; \
    fi; \
    done; \
    test -d /opt/cura; \
    CURA_DEFS="$(find /opt/cura -name 'fdmprinter.def.json' -type f | head -1 | xargs dirname)"; \
    echo "Found definitions at: $CURA_DEFS"; \
    mkdir -p /app/app/profiles/definitions; \
    cp -r "$CURA_DEFS"/. /app/app/profiles/definitions/; \
    rm -f /tmp/cura.AppImage

# Patch all ELF binaries extracted from the AppImage to use the system interpreter.
# AppImage binaries embed a custom interpreter path (e.g. /opt/cura/lib/ld-linux*.so)
# that doesn't exist outside the AppImage runtime, causing "exec: not found" failures.
RUN INTERP="/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"; \
    find /opt/cura -type f -perm /111 | while read -r f; do \
    if file "$f" 2>/dev/null | grep -q 'ELF.*dynamically linked'; then \
    patchelf --set-interpreter "$INTERP" "$f" 2>/dev/null || true; \
    fi; \
    done

RUN set -eux; \
    CURA_BIN="$(find /opt/cura -name 'CuraEngine' -type f -perm /111 | sort | head -1)"; \
    test -n "$CURA_BIN"; \
    echo "CuraEngine real bin: $CURA_BIN"; \
    file "$CURA_BIN"; \
    patchelf --print-interpreter "$CURA_BIN" || true; \
    patchelf --set-interpreter /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 "$CURA_BIN"; \
    patchelf --print-interpreter "$CURA_BIN"; \
    ldd "$CURA_BIN" || true

############################
#  Python 依赖
############################
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

############################
#  复制业务代码 & 默认入口
############################
COPY app /app/app

ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
