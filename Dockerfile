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

# cloudflared binary — the box process spawns it (server/tunnel.py) with the
# per-device connector token issued at pairing, so it lives in the same image
# and forwards the tunnel to localhost:8765.
ARG TARGETARCH=amd64
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${TARGETARCH}" \
        -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

ENV AIOS_ENV=production \
    AIOS_SERVER_HOST=0.0.0.0 \
    AIOS_SERVER_PORT=8765 \
    AIOS_VERSION=${AIOS_VERSION} \
    AIOS_RELEASE_ID=${AIOS_RELEASE_ID} \
    AIOS_RELEASE_SEQUENCE=${AIOS_RELEASE_SEQUENCE} \
    AIOS_REVISION=${AIOS_REVISION}

# Deps first for layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-workspace

COPY . .

EXPOSE 8765
CMD ["uv", "run", "--no-sync", "python", "main.py"]
