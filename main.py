from __future__ import annotations

import os

import uvicorn


def main() -> None:
    os.environ.setdefault("AIOS_HEARTBEAT_ENABLED", "0")
    host = os.getenv("AIOS_SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("AIOS_SERVER_PORT", "8765"))
    uvicorn.run("server.server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
