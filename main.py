from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.getenv("AIOS_SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("AIOS_SERVER_PORT", "8000"))
    uvicorn.run("server.server:app", host=host, reload=True)


if __name__ == "__main__":
    main()
