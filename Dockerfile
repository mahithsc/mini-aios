FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG AIOS_VERSION=0.1.0
ARG AIOS_RELEASE_ID=development
ARG AIOS_RELEASE_SEQUENCE=0
ARG AIOS_REVISION=unknown

LABEL org.opencontainers.image.source="https://github.com/mahithsc/mini-aios" \
      org.opencontainers.image.version="${AIOS_VERSION}" \
      org.opencontainers.image.revision="${AIOS_REVISION}" \
      io.mini-aios.release-id="${AIOS_RELEASE_ID}" \
      io.mini-aios.sequence="${AIOS_RELEASE_SEQUENCE}" \
      io.mini-aios.db-schema="1"

WORKDIR /app

ENV APP_ENV=production \
    AIOS_ENV=production \
    AIOS_HEARTBEAT_ENABLED=0 \
    AIOS_SERVER_HOST=0.0.0.0 \
    AIOS_SERVER_PORT=8765 \
    AIOS_CLOUD_URL=https://computer.winkapiserver.org \
    AIOS_VERSION=${AIOS_VERSION} \
    AIOS_RELEASE_ID=${AIOS_RELEASE_ID} \
    AIOS_RELEASE_SEQUENCE=${AIOS_RELEASE_SEQUENCE} \
    AIOS_REVISION=${AIOS_REVISION} \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-workspace

COPY . .

EXPOSE 8765

CMD ["sh", "-c", "uv run --no-sync uvicorn main:app --host 0.0.0.0 --port ${PORT:-${AIOS_SERVER_PORT:-8765}}"]
