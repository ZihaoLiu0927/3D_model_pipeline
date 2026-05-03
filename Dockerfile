# syntax=docker/dockerfile:1

############################
#  基础层：Python 3.11 运行时
############################
# 【注意】AWS 部署时，确保你的计算实例（EC2/Fargate）是 x86_64 架构。
# 如果使用 Graviton (ARM64) 实例，这个 amd64 的 AppImage 将无法运行。
FROM --platform=linux/amd64 python:3.11-slim-bookworm

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
#  安装 Blender (headless)
############################
ARG BL_VER=4.1.1
ARG BL_DIR=${BL_VER%.*}
RUN curl -fSL \
    https://download.blender.org/release/Blender${BL_DIR}/blender-${BL_VER}-linux-x64.tar.xz \
    -o blender.tar.xz \
    && tar -xJf blender.tar.xz -C /opt \
    && ln -s /opt/blender-${BL_VER}-linux-x64/blender /usr/local/bin/blender \
    && rm blender.tar.xz

############################
#  安装 CuraEngine 5.12.0（从官方 AppImage 提取）
############################
RUN set -eux; \
    wget -q \
        "https://github.com/Ultimaker/Cura/releases/download/5.12.0/UltiMaker-Cura-5.12.0-linux-X64.AppImage" \
        -O /tmp/cura.AppImage; \
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

# 【核心修改 2】：编写更稳健的 Wrapper 脚本，注入 AppImage 的标准库路径
RUN set -eux; \
    CURA_BIN="$(find /opt/cura -name 'CuraEngine' -type f -perm /111 | sort | head -1)"; \
    test -n "$CURA_BIN"; \
    if [ "$CURA_BIN" != "/opt/cura/CuraEngine" ]; then \
        ln -sf "$CURA_BIN" /opt/cura/CuraEngine; \
    fi; \
    printf '%s\n' \
        '#!/bin/sh' \
        'set -eu' \
        'CURA_LIB_PATHS="$(find /opt/cura -type f -name "*.so*" -printf "%h\n" | sort -u | tr "\n" ":")"' \
        'export LD_LIBRARY_PATH="${CURA_LIB_PATHS}${LD_LIBRARY_PATH:-}"' \
        'if [ -x /opt/cura/CuraEngine ]; then' \
        '  exec /opt/cura/CuraEngine "$@"' \
        'fi' \
        'CURA_BIN="$(find /opt/cura -name CuraEngine -type f -perm /111 | sort | head -1)"' \
        'if [ -z "$CURA_BIN" ]; then' \
        '  echo "CuraEngine binary not found under /opt/cura" >&2' \
        '  exit 127' \
        'fi' \
        'exec "$CURA_BIN" "$@"' \
        > /usr/local/bin/CuraEngine; \
    chmod +x /usr/local/bin/CuraEngine; \
    test -x /usr/local/bin/CuraEngine; \
    test -n "$(find /opt/cura -name 'libArcus.so*' -type f | head -1)"

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
