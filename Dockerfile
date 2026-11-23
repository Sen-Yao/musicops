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

ARG MUSICOPS_BUILD_COMMIT=unknown
ARG MUSICOPS_BUILD_TIME=unknown
ENV MUSICOPS_BUILD_COMMIT=${MUSICOPS_BUILD_COMMIT} \
    MUSICOPS_BUILD_TIME=${MUSICOPS_BUILD_TIME}

USER musicops

EXPOSE 8088

CMD ["uvicorn", "musicops.main:app", "--host", "0.0.0.0", "--port", "8088"]
