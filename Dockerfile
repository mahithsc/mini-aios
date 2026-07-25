FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

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
    AIOS_SERVER_PORT=8765

# Deps first for layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-workspace

COPY . .

EXPOSE 8765
CMD ["uv", "run", "--no-sync", "python", "main.py"]
