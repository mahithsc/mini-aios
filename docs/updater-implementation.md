# Updater implementation guide

The repository now contains both sides of the first working update system:

- `updater/`: native Go receiver for Linux appliances and macOS testing;
- `release/publish_update.py`: manifest creation, Ed25519 signing, and verification;
- `.github/workflows/publish-update.yml`: tests, multi-platform image build, provenance, signed feed, updater binaries, GitHub Release, and optional R2 publication;
- `server/updater.py`: authenticated AIOS drain, liveness, readiness, and resume API; and
- `scripts/mac-updater-demo.sh`: local end-to-end Docker Desktop demonstration.

## Security level of this implementation

The working v1 feed signs the exact manifest bytes with an offline/protected Ed25519 release key. The receiver pins the public key and enforces signature, expiry, channel, repository, digest, platform, minimum updater version, monotonic sequence, and database compatibility.

This is a secure deployable first slice, but it is not the complete TUF repository described in the production design. TUF remains the next hardening layer for threshold root keys, delegated roles, key rotation, and freeze/mix-and-match defenses. The image, drain, backup, activation, observation, and rollback engine does not need to change when the feed verifier is upgraded to `go-tuf`.

## Run the full updater on a Mac

Prerequisites:

- macOS on Apple Silicon or Intel;
- Docker Desktop running;
- Go 1.23 or newer;
- `uv`; and
- at least 5 GiB of free disk for two local application images, registry
  storage, build layers, and database backups.

From the repository root:

```bash
make mac-updater-demo
```

The command:

1. detects `darwin/arm64` or `darwin/amd64`;
2. starts a local registry on `localhost:5001`;
3. creates a local Ed25519 test keypair and updater token under `.dev/`;
4. builds and starts a baseline Linux AIOS container through Docker Desktop;
5. builds and pushes a candidate Linux image for the Mac's CPU architecture;
6. creates and signs a local stable-channel feed;
7. builds the matching native Mac updater;
8. checks and installs the signed update;
9. drains and stops the baseline container;
10. backs up SQLite, activates the candidate, and waits for readiness;
11. observes health for approximately 30 seconds; and
12. prints the committed updater state.

Generated files live in:

```text
.dev/mac-updater/
```

The local private key is only a test key and is excluded by `.gitignore`. Never use it for production.

Useful commands after the demo:

```bash
.dev/mac-updater/mini-aios-updater status --config .dev/mac-updater/updater.toml
.dev/mac-updater/mini-aios-updater check --config .dev/mac-updater/updater.toml
curl http://127.0.0.1:8765/health
```

The demo also generates a user LaunchAgent plist. To poll the local feed in the background:

```bash
launchctl bootstrap gui/$(id -u) .dev/mac-updater/com.mahithsc.mini-aios-updater.plist
```

Unload it with:

```bash
launchctl bootout gui/$(id -u)/com.mahithsc.mini-aios-updater
```

## Build and test on GitHub

The `Updater build and integration test` workflow runs on pull requests, pushes
to `main`, or manual dispatch. It does not publish a release and requires no
signing or registry secrets. It:

- runs the Python and Go test suites plus `go vet`;
- cross-builds updater binaries for Linux and macOS on AMD64 and ARM64;
- uploads those four binaries as a 14-day workflow artifact;
- builds the Mini AIOS image for Linux AMD64 and ARM64; and
- runs a complete signed update transaction on a GitHub-hosted Linux runner.

The Linux integration test uses the same harness as the Mac demo. A GitHub
hosted macOS runner is not used for this step because the hosted environment
does not provide the Docker Desktop Linux-container path exercised locally.

Use `Publish signed Mini AIOS update` only for an approved release. That
separate workflow pushes the image to GHCR, signs the feed, and creates release
assets.

## Create production signing keys

Perform this on an offline/admin machine:

```bash
uv run --python 3.12 python release/publish_update.py keygen \
  --private-key update-private.pem \
  --public-key update-public.pem
```

- Install `update-public.pem` as `/etc/mini-aios/update-signing-public.pem` on appliances.
- Store the base64-encoded private PEM as the protected GitHub environment secret `UPDATE_SIGNING_PRIVATE_KEY_B64`.
- Keep a separately protected offline backup of the private key.
- Never commit the private key or place it in the AIOS container.

The v1 format supports one signing key. Production TUF migration introduces threshold roots and key rotation before a large fleet rollout.

## Configure GitHub publishing

Create a GitHub environment named `stable-release` with:

- required reviewer;
- self-review disabled;
- only protected tags/branches allowed; and
- secret `UPDATE_SIGNING_PRIVATE_KEY_B64`.

Make the GHCR package `ghcr.io/mahithsc/mini-aios` public so devices can pull by digest without a shared registry credential.

Optional R2 environment configuration:

```text
Secrets:
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY

Variables:
  R2_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
  R2_BUCKET=<update-bucket-name>
```

The R2 credential should have write access only to the update-feed bucket. The workflow uploads:

```text
channels/dev.json
channels/beta.json
channels/stable.json
```

with `Cache-Control: no-store`. Without R2 configuration, the workflow still publishes the signed feed and binaries as GitHub Release assets.

## Publish a release

Run the `Publish signed Mini AIOS update` workflow and provide:

- application version;
- globally unique release ID;
- sequence greater than every previous release;
- channel; and
- health-observation duration.

The workflow:

- runs the Python and Go test suites;
- builds one OCI index for `linux/amd64` and `linux/arm64`;
- pushes it to GHCR and captures its immutable digest;
- creates GitHub container provenance;
- builds updater binaries for Linux and macOS on both architectures;
- creates and signs the channel manifest;
- validates both JSON Schemas;
- attests updater binaries;
- creates/uploads the GitHub Release; and
- publishes the signed channel feed to R2 when configured.

Devices use only `repository@sha256:digest`. Tags are present for humans and never select installed bytes.

## Install on a Linux appliance

Production assets are in `updater/packaging/linux/`:

```text
compose.yaml
updater.toml
mini-aios-updater.service
```

Install the correct updater binary as `/usr/local/bin/mini-aios-updater`, then install:

```text
/opt/mini-aios/compose.yaml
/etc/mini-aios/updater.toml
/etc/mini-aios/update-signing-public.pem
/etc/mini-aios/updater-admin-token
/etc/systemd/system/mini-aios-updater.service
```

Create root-owned data directories:

```text
/var/lib/mini-aios
/var/lib/mini-aios-updater
```

The admin token must be a random value readable only by root. It is mounted read-only into the AIOS container and accepted only by `/internal/updater/*`.

Before enabling automatic polling, bootstrap `/opt/mini-aios/release.env` with a known-good published digest and start the Compose service. Then:

```bash
systemctl daemon-reload
systemctl enable --now mini-aios-updater
mini-aios-updater doctor --config /etc/mini-aios/updater.toml
```

## Failure behavior implemented

- Feed unavailable: keep the current release and retry later.
- Signature, expiry, repository, channel, platform, or schema invalid: reject before Docker pull.
- Pull, disk, drain, or backup failure: retain/restart the old release.
- Startup or observation failure: stop the candidate, restore the database when required, and restart the previous digest.
- Reboot during download/preflight/drain: safely clear or resume the pre-activation state.
- Reboot during activation/observation: health-check and commit the candidate, or continue rollback.
- Failed candidate sequence: do not retry it forever; wait for a higher signed sequence.
- Rollback failure: enter `recovery_required` instead of alternating releases.

## Remaining production hardening

- Replace the single-key v1 feed verifier with the designed TUF repository/client.
- Add device-specific assignment and event APIs in the separate AIOS cloud repository.
- Add automatic backup pruning according to retention configuration.
- Add controlled updater self-update.
- Test Linux power-loss injection on real appliance storage.
- Add an OS/firmware A/B updater separately; this component updates only AIOS.
