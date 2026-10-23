FROM python:3.12-slim

ARG INSTALL_PLAYWRIGHT=0
# 国内可换成 https://mirrors.aliyun.com/pypi/simple/ 或 https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
# composer（packagist）国内镜像，构建期写入镜像，运行期可被 COMPOSER_MIRROR 覆盖
ARG COMPOSER_MIRROR=https://mirrors.aliyun.com/composer/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=100 \
    PIP_RETRIES=10 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    TZ=Asia/Shanghai \
    PANEL_PORT=8100 \
    PORT_RANGE_START=8600 \
    PORT_RANGE_END=8699 \
    MOUNT_DIR=/mount \
    DATA_DIR=/data \
    COMPOSER_HOME=/data/composer \
    COMPOSER_ALLOW_SUPERUSER=1 \
    COMPOSER_MIRROR=${COMPOSER_MIRROR} \
    PHP_CLI_SERVER_WORKERS=4

# apt 换阿里源（国内构建更快）：Debian 12 用 deb822 格式的 debian.sources，兼容老的 sources.list
RUN for f in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do \
        [ -f "$f" ] && sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' "$f" || true; \
    done; true

# nodejs/npm：用于运行 .js 任务与安装 JS 依赖（Debian 源，node 18+）
# php-cli + 常用扩展 + composer：用于运行 .php 任务与 composer 依赖（Debian 预编译，免编译，PHP 8.2/8.4 随发行版）
RUN apt-get update && apt-get install -y --no-install-recommends \
        procps curl ca-certificates tzdata nodejs npm \
        git unzip \
        php-cli php-common php-mysql php-gd php-zip php-mbstring php-xml \
        php-curl php-bcmath php-intl php-redis php-sqlite3 php-opcache \
        composer \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# docker CLI：配合宿主机挂载 /var/run/docker.sock 使用，容器内可直接控制宿主机 Docker
# （静态二进制，按构建架构 amd64/arm64 自动选择对应包）
ARG TARGETARCH
ARG DOCKER_CLI_VERSION=27.5.1
RUN set -ex; \
    case "${TARGETARCH}" in \
      amd64) DOCKER_ARCH=x86_64 ;; \
      arm64) DOCKER_ARCH=aarch64 ;; \
      *) echo "unsupported TARGETARCH ${TARGETARCH}"; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz \
    && docker --version

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt ./
# 下载被截断/镜像抖动会造成 hash 不匹配，自动重试 3 次
RUN n=0; until pip install --no-cache-dir -r requirements.txt; do \
        n=$((n+1)); \
        if [ "$n" -ge 3 ]; then echo "pip install 重试 3 次仍失败"; exit 1; fi; \
        echo "安装失败（多为下载被截断），5 秒后重试 $n/3 ..."; sleep 5; \
    done \
    && if [ "$INSTALL_PLAYWRIGHT" = "1" ]; then \
         pip install playwright && playwright install --with-deps chromium; \
       fi

COPY app ./app

RUN mkdir -p /data/uploads /data/jobs /data/logs /data/pids /data/composer /mount \
    && composer config -g repos.packagist composer "${COMPOSER_MIRROR}" \
    && composer config -g process-timeout 600 \
    && php -v && composer --version

EXPOSE 8100 8600-8699

# 面板端口统一走 PANEL_PORT（改了它记得同步 compose 的 ports 与上面 EXPOSE）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PANEL_PORT:-8100}/api/health" || exit 1

CMD uvicorn app.main:app --host 0.0.0.0 --port "${PANEL_PORT:-8100}" --proxy-headers --forwarded-allow-ips '*'
