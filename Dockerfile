# syntax=docker/dockerfile:1

############################
#  基础层：Python 3.11 运行时
############################
FROM --platform=linux/amd64 python:3.11-slim-bookworm
# 官方镜像自带 debian 12 (bookworm) + CPython 3.11，体积小且长期维护 :contentReference[oaicite:0]{index=0}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    # CLI 路径写成环境变量，代码里的 config.py 会读取
    BLENDER_BIN=/usr/local/bin/blender \
    BAMBUSTUDIO_BIN=/usr/local/bin/bambustudio

############################
#  系统运行库 + 构建工具
############################
RUN apt-get update && apt-get install -y --no-install-recommends \
    # —— 运行期必需 —— (PyMeshLab/OpenGL 等) ↓
    libgl1 libglu1-mesa libqt5widgets5 qtbase5-dev \
    libxrender1 libxrandr2 libxi6 libopengl0 \
    libarchive-tools libfuse2 \
    # —— 构建 BambuStudio 源码所需 —— 
    # —— 编译 mathutils 等 C 扩展（可删，但第一次建议保留）↓
    build-essential gcc g++ make                       \
    # —— 常用工具 ↓
    curl wget git ca-certificates                      \
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
#  安装 Bambu Studio CLI 
############################
# ARG BS_VERSION=V02.00.03.54
# RUN wget -O source.tar.gz \
#     https://github.com/bambulab/BambuStudio/archive/refs/tags/${BS_VERSION}.tar.gz \
#     && tar -xzf source.tar.gz --strip-components=1 \
#     && rm source.tar.gz

# RUN mkdir -p build && cd build \
#     && cmake -DCMAKE_BUILD_TYPE=Release .. \
#     && make -j$(nproc)

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
