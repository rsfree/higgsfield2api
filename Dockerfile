# syntax=docker/dockerfile:1
#
# higgsfield2api —— 只用官方 slim 基础镜像 + pip wheel，不编译、不装 apt 包：
# curl_cffi 的 manylinux wheel 自带 libcurl-impersonate，省掉一层系统依赖。
FROM python:3.12-slim

ARG VERSION=0.0.0
LABEL org.opencontainers.image.title="higgsfield2api" \
      org.opencontainers.image.description="Higgsfield MAGI-2 -> Ark(Seedance) video protocol gateway" \
      org.opencontainers.image.source="https://github.com/rsfree/higgsfield2api" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="UNLICENSED"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 只搬运行时需要的目录：.env / secrets / data 一律不进镜像
COPY hfapi ./hfapi
COPY tools ./tools
COPY README.md ./

# 写目录先建好并交出属主，容器以非 root 运行；绑卷时卷会继承这里的属主
RUN mkdir -p /app/data /app/secrets && chown -R 10001:10001 /app

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "hfapi.server:app", "--host", "0.0.0.0", "--port", "8000"]
