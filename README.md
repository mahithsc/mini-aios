Inspired by openclaw
My own naive implementaion of it + a little more.

Features:
- Tools: read, write, grep, exec, and processes.
- Crons
- Learning and dreaming: agent does a longer horizon task and when your not using the computer, it compacts the process through tool call chain into a skill for next time.

## Signed updater

The repository includes a signed release publisher and host updater for Linux appliances, plus a Docker Desktop test path for macOS:

```bash
make mac-updater-demo
```

To test the actual GitHub-hosted signed dev feed on a Mac (with Docker Desktop running and at least 5 GiB free):

```bash
make mac-github-updater-test
```

See [the updater implementation guide](docs/updater-implementation.md) for publishing, Linux installation, security boundaries, and local testing.
