from __future__ import annotations

import os

import uvicorn

from server.server import app


def main() -> None:
    host = os.getenv("AIOS_SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("AIOS_SERVER_PORT", "8765"))
    uvicorn.run("main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
