# syntax=docker/dockerfile:1
# ============================================================
# WxReadAssistant v2.4.0 镜像（多阶段构建）
# 目标平台：linux/amd64（NAS x86_64 / Debian 12 / Docker 28.5.2）
# 国内构建加速示例：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
#     -f docker/Dockerfile -t wxreadassistant:2.4.0 .
# ============================================================

# -------------------- 阶段 1：builder（装依赖 + 下载浏览器，产物随后整体拷走）--------------------
# 固定 linux/amd64 + bookworm，避免在其他架构机构建出错误平台镜像
FROM --platform=linux/amd64 python:3.14-slim-bookworm AS builder

# pip 源覆盖参数：仅在构建时显式传入才生效（国内加速），故意不设默认值，缺省走 pip 官方源
ARG PIP_INDEX_URL
# Playwright 浏览器二进制下载源（国内可传 https://cdn.npmmirror.com/binaries/playwright）
ARG PLAYWRIGHT_DOWNLOAD_HOST

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# 先单独拷贝依赖清单：只要 requirements-docker.txt 不变，
# 后续 pip / 浏览器下载层均可命中 BuildKit 缓存，不受源码变更影响
COPY requirements-docker.txt /tmp/requirements-docker.txt

# 运行时 Python 依赖装到独立前缀 /install（playwright 已包含在清单内），
# 便于 runtime 阶段整目录拷贝，builder 中的 pip/缓存等杂物不进最终镜像
RUN python -m pip install --no-cache-dir --prefix=/install \
        -r /tmp/requirements-docker.txt

# 让解释器能从 /install 找到 playwright 包，再把 chromium 二进制下载到 /ms-playwright。
# 不使用 --with-deps：浏览器的系统运行库改在 runtime 阶段用 apt 显式安装，更可控、镜像更小
ENV PYTHONPATH=/install/lib/python3.14/site-packages
RUN python -m playwright install chromium

# -------------------- 阶段 2：runtime（最终镜像）--------------------
FROM --platform=linux/amd64 python:3.14-slim-bookworm AS runtime

# 运行时环境变量：
#   APPDATA                    应用数据目录（配置/Cookie/本地库/日志/心跳都在此）
#   WXREAD_RUNTIME=pure        强制纯 Python 运行时（app/core/runtime.py，无任何 Qt 依赖）
#   PLAYWRIGHT_BROWSERS_PATH   与 builder 下载位置一致
#   WXREAD_LOG_RETENTION_DAYS  日志默认保留 7 天
#   WXREAD_IN_CONTAINER=1      容器标记：入口据此在未设 Token 时仍绑 0.0.0.0
#                              （bridge 端口映射要求），安全边界交给端口发布范围
#   TZ                         容器时区（日志时间/调度节奏与本地一致）
ENV APPDATA=/data \
    WXREAD_RUNTIME=pure \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    WXREAD_LOG_RETENTION_DAYS=7 \
    WXREAD_IN_CONTAINER=1 \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 安装：CA 证书（HTTPS 请求）、tzdata（时区）、chromium headless 运行所需系统库、
# 中文字体与 emoji 字体（扫码页/页面截图中文不乱码）。
# 库清单依据 Playwright 官方对 Debian bookworm 的 chromium 依赖列表精简
# （libnss3/libnspr4、atk/atspi、cups、drm、xkbcommon、X 合成/损坏/修复/分辨率扩展、
#   gbm/xshmfence、asound/dbus、pango/cairo 文本渲染）
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        libnss3 libnspr4 \
        libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
        libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libxshmfence1 libgbm1 \
        libasound2 libdbus-1-3 \
        libpango-1.0-0 libpangocairo-1.0-0 libcairo2 \
        fonts-liberation fonts-noto-cjk fonts-noto-color-emoji \
 && ln -fs /usr/share/zoneinfo/$TZ /etc/localtime \
 && dpkg-reconfigure -f noninteractive tzdata \
 && rm -rf /var/lib/apt/lists/*

# 从 builder 拷入 Python 依赖，落到官方镜像默认的 site-packages / bin 路径
COPY --from=builder /install/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /install/bin /usr/local/bin
# 拷入 Playwright chromium（含 headless shell），与 PLAYWRIGHT_BROWSERS_PATH 对应
COPY --from=builder /ms-playwright /ms-playwright

WORKDIR /app

# 最后才拷业务源码：源码频繁变更不会使前面的依赖/浏览器层缓存失效
COPY app/ ./app/
COPY tools/ ./tools/
COPY main_headless.py ./

# 持久化数据目录（main_headless.py 启动也会自行 mkdir，此处显式声明保证卷挂载点存在）
RUN mkdir -p /data
VOLUME ["/data"]

# Web 服务端口
EXPOSE 5911

# 健康检查：/data/heartbeat 由入口守护线程每 60s 原子刷新一次（main_headless.py）。
# 文件缺失或 mtime 距今超过 600 秒（10 分钟无心跳）即判定容器不健康
HEALTHCHECK --interval=5m --timeout=10s --start-period=30s --retries=2 \
  CMD python3 -c "import os,sys,time;p='/data/heartbeat';sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<600 else 1)"

# 直接以前台进程方式运行 headless 入口（uvicorn + 后台调度器）
CMD ["python", "main_headless.py"]
