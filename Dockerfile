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
    libarchive-tools libfuse2 \
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
RUN wget -q \
    "https://github.com/Ultimaker/Cura/releases/download/5.12.0/UltiMaker-Cura-5.12.0-linux-X64.AppImage" \
    -O /tmp/cura.AppImage \
    && chmod +x /tmp/cura.AppImage \
    && cd /tmp && ./cura.AppImage --appimage-extract > /dev/null \
    # 【核心修改 1】：不要去猜测和挑选 .so 文件，直接保留完整的 AppImage 依赖环境
    && mv /tmp/squashfs-root /opt/cura \
    # 提取 definitions 目录
    && CURA_DEFS=$(find /opt/cura -name 'fdmprinter.def.json' -type f | head -1 | xargs dirname) \
    && echo "Found definitions at: $CURA_DEFS" \
    && mkdir -p /app/app/profiles/definitions \
    && cp -r "$CURA_DEFS"/. /app/app/profiles/definitions/ \
    # 清理无用的 AppImage 文件释放空间
    && rm -f /tmp/cura.AppImage

# 【核心修改 2】：编写更稳健的 Wrapper 脚本，注入 AppImage 的标准库路径
RUN CURA_BIN=$(find /opt/cura -name 'CuraEngine' -type f | head -1) \
    && echo '#!/bin/sh' > /usr/local/bin/CuraEngine \
    # 包含 AppImage 内置的各种 lib 路径（兼容 x86_64-linux-gnu）
    && echo 'export LD_LIBRARY_PATH="/opt/cura/usr/lib:/opt/cura/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH}"' >> /usr/local/bin/CuraEngine \
    && echo 'exec "'$CURA_BIN'" "$@"' >> /usr/local/bin/CuraEngine \
    && chmod +x /usr/local/bin/CuraEngine

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
