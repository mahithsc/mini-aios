FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates nodejs npm tini \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir "mcp>=1.27,<2" uv \
    && npm install --global typescript tsx \
    && groupadd --gid 65532 aios \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin aios \
    && mkdir --parents /app /runtime /data \
    && chown --recursive 65532:65532 /runtime /data

WORKDIR /app
USER 65532:65532
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-c", "raise SystemExit('an App command is required')"]
