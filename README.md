# Mini AIOS

## Signed updater

The repository includes a signed release publisher and host updater for Linux appliances, plus a Docker Desktop test path for macOS:

```bash
make mac-updater-demo
```

See [the updater implementation guide](docs/updater-implementation.md) for publishing, Linux installation, security boundaries, and local testing.

# TV Remote (React + FastAPI)

This repo now includes a simple LAN TV remote aimed at **Samsung Tizen TVs** (many 2016+ models) using Samsung's websocket remote-control API.

## Backend (FastAPI)

Endpoints:
- `POST /tv/samsung/probe` – checks `http(s)://{ip}:{port}/api/v2/`
- `POST /tv/samsung/key` – sends one remote key (e.g. `KEY_VOLUP`)
- `GET /tv/samsung/keys` – subset used by the UI

## Frontend (React)

UI lives in `tv-remote-ui/`.

It supports two modes:
- **Proxy mode (default):** calls `/api/*` which Vite proxies to `http://127.0.0.1:8000`
- **Direct mode:** set FastAPI base URL manually

## Running

### Option A: dev mode (Vite)

Backend:
- `python main.py` (FastAPI on `0.0.0.0:8000` by default)

Frontend:
- `cd tv-remote-ui && npm install && npm run dev`

Then open:
- `http://localhost:5173` on your computer
- `http://<your-computer-lan-ip>:5173` on your phone

### Option B: single-server mode (FastAPI serves the built UI)

- `cd tv-remote-ui && npm install && npm run build`
- `python main.py`

Then open:
- `http://localhost:8000/` (or `/remote`)
- `http://<your-computer-lan-ip>:8000/` from your phone

Pairing:
- First time you send a key with an empty token, the TV may show a pairing prompt.
- If the TV returns a token, the UI auto-saves it.
