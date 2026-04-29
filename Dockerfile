FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV APP_ENV=production \
    AIOS_ENV=production \
    AIOS_HEARTBEAT_ENABLED=0 \
    AIOS_SERVER_HOST=0.0.0.0 \
    AIOS_SERVER_PORT=8765 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-workspace

COPY . .

EXPOSE 8765

CMD ["sh", "-c", "uv run --no-sync uvicorn main:app --host 0.0.0.0 --port ${PORT:-${AIOS_SERVER_PORT:-8765}}"]
