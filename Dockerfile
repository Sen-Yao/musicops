ARG MUSICOPS_BASE_IMAGE=python:3.12-slim
FROM ${MUSICOPS_BASE_IMAGE}

USER root

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MUSICOPS_DATA_DIR=/data

RUN if ! id -u musicops >/dev/null 2>&1; then useradd --create-home --shell /usr/sbin/nologin musicops; fi \
    && mkdir -p /data \
    && chown musicops:musicops /data

# An interrupted image layer is discarded, so per-file dpkg fsyncs only
# amplify btrfs build latency without improving the completed image.
RUN if ! command -v ffmpeg >/dev/null 2>&1; then \
        apt-get update \
        && apt-get -o Dpkg::Options::=--force-unsafe-io install -y --no-install-recommends ffmpeg \
        && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /
RUN (python -m pip uninstall -y musicops || true) \
    && rm -rf /app
WORKDIR /app

COPY --chown=musicops:musicops pyproject.toml README.md ./
COPY --chown=musicops:musicops src ./src

RUN pip install --no-cache-dir --timeout 120 --retries 8 .
RUN rm -rf build src/*.egg-info
RUN PYTHONPATH=/app/src python -m musicops.provenance write --root /app --output /app/.musicops-source-manifest

ARG MUSICOPS_BUILD_COMMIT=unknown
ARG MUSICOPS_BUILD_TIME=unknown
ARG MUSICOPS_BUILD_TREE=unknown
ARG MUSICOPS_SOURCE_MANIFEST_SHA256=
ARG MUSICOPS_ACTION_SCHEMA_DIGEST=
ENV MUSICOPS_BUILD_COMMIT=${MUSICOPS_BUILD_COMMIT} \
    MUSICOPS_BUILD_TIME=${MUSICOPS_BUILD_TIME} \
    MUSICOPS_BUILD_TREE=${MUSICOPS_BUILD_TREE} \
    MUSICOPS_SOURCE_ROOT=/app \
    MUSICOPS_SOURCE_MANIFEST_PATH=/app/.musicops-source-manifest \
    MUSICOPS_SOURCE_MANIFEST_SHA256=${MUSICOPS_SOURCE_MANIFEST_SHA256} \
    MUSICOPS_ACTION_SCHEMA_DIGEST=${MUSICOPS_ACTION_SCHEMA_DIGEST}

LABEL org.opencontainers.image.source="https://github.com/Sen-Yao/musicops" \
      org.opencontainers.image.revision="${MUSICOPS_BUILD_COMMIT}"

RUN if [ -n "$MUSICOPS_SOURCE_MANIFEST_SHA256" ]; then \
      PYTHONPATH=/app/src python -m musicops.provenance verify --root /app --manifest /app/.musicops-source-manifest --expected-sha256 "$MUSICOPS_SOURCE_MANIFEST_SHA256"; \
    fi

USER musicops

EXPOSE 8088

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=12 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/health/ready', timeout=4).read()"]

CMD ["uvicorn", "musicops.main:app", "--host", "0.0.0.0", "--port", "8088"]
