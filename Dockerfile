# syntax=docker/dockerfile:1

############################
# Stage 1: 从 AppImage 提取 CuraEngine 5.x
# 将下载/解压与运行时完全隔离，最终镜像不含 AppImage 残余
############################
FROM --platform=linux/amd64 debian:bookworm-slim AS cura-extractor

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG CURA_VER=5.12.0
RUN set -e \
    # --tries=5 --waitretry=15 应对 AWS 出口 IP 偶发被 GitHub CDN 限速
    && wget -q --tries=5 --timeout=600 --waitretry=15 \
       "https://github.com/Ultimaker/Cura/releases/download/${CURA_VER}/UltiMaker-Cura-${CURA_VER}-linux-X64.AppImage" \
       -O /tmp/cura.AppImage \
    && chmod +x /tmp/cura.AppImage \
    # --appimage-extract 不依赖 FUSE，在容器内安全运行
    && cd /tmp && ./cura.AppImage --appimage-extract > /dev/null \
    # 定位 CuraEngine 二进制
    && CURA_BIN=$(find /tmp/squashfs-root -name 'CuraEngine' -type f | head -1) \
    && test -n "$CURA_BIN" && echo "Found CuraEngine: $CURA_BIN" \
    && install -Dm755 "$CURA_BIN" /opt/CuraEngine.bin \
    # 提取运行时所需的全部 Ultimaker/protobuf 相关动态库
    && mkdir -p /opt/cura-libs \
    && find /tmp/squashfs-root \( \
         -name 'libarcus*'       \
         -o -name 'libprotobuf*' \
         -o -name 'libpolyclipping*' \
         -o -name 'libnest2d*'   \
         -o -name 'libSavitar*'  \
         -o -name 'libpython3*'  \
       \) | xargs -I{} cp -P {} /opt/cura-libs/ 2>/dev/null || true \
    # 定位打印机定义目录
    && CURA_DEFS=$(find /tmp/squashfs-root -name 'fdmprinter.def.json' -type f | head -1 | xargs dirname) \
    && test -n "$CURA_DEFS" && echo "Found definitions: $CURA_DEFS" \
    && mkdir -p /opt/cura-defs \
    && cp -r "$CURA_DEFS"/. /opt/cura-defs/

############################
#  Stage 2: Python 3.11 运行时
############################
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
    libgl1 libglu1-mesa libqt5widgets5 \
    libxrender1 libxrandr2 libxi6 libopengl0 \
    libarchive-tools libfuse2 \
    build-essential gcc g++ make libeigen3-dev \
    curl wget git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends \
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
#  从 Stage 1 复制 CuraEngine 5.x 及其依赖
############################
COPY --from=cura-extractor /opt/CuraEngine.bin /opt/CuraEngine.bin
COPY --from=cura-extractor /opt/cura-libs      /opt/cura-libs
COPY --from=cura-extractor /opt/cura-defs      /app/app/profiles/definitions

# wrapper：注入 AppImage 运行时库路径
RUN printf '#!/bin/sh\nexec env LD_LIBRARY_PATH=/opt/cura-libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH} /opt/CuraEngine.bin "$@"\n' \
    > /usr/local/bin/CuraEngine && chmod +x /usr/local/bin/CuraEngine

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
