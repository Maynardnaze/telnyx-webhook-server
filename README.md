# Miswitch Telnyx Webhook Server

FastAPI webhook receiver for [Telnyx](https://telnyx.com/) — currently focused on **Insight Group** delivery and inspection.

Designed to run behind Traefik at a public hostname such as:

```text
https://webhook.miswitch.cloud
```

## What this server does

- **Receives Telnyx webhooks** at `POST /telnyx/insights`
- **Verifies Telnyx Ed25519 signatures** when `TELNYX_PUBLIC_KEY` is configured
- **Stores payloads in SQLite** for later inspection
- **Exposes a read-only listing** at `GET /telnyx/insights` (shared-secret protected)

The server returns fast `200` acknowledgements so Telnyx does not retry deliveries.

**Insight payload reference:** [docs/telnyx-insights.md](docs/telnyx-insights.md) — webhook shape, stored record format, channel metadata, and real-world examples.

## Repository layout

```text
telnyx-webhook-server/
├── app.py                 # FastAPI application
├── docs/
│   └── telnyx-insights.md # Insight Group webhook format and examples
├── docker-compose.yml     # Production Compose stack
├── Dockerfile
├── data/                  # Runtime data (gitignored except .gitkeep)
│   └── webhook.db         # SQLite database (created on first run)
└── secrets/
    └── telnyx_webhook_secret   # Shared secret for inspection endpoints and curl tests
```

## Quick start (Docker)

Prerequisites:

- Docker and Docker Compose
- An external Docker network named `t3_proxy` (used by the Traefik stack in `~/docker`)

```bash
git clone https://github.com/Maynardnaze/telnyx-webhook-server.git
cd telnyx-webhook-server
cp .env.example .env
mkdir -p data secrets

# Put only the secret value in the file — no "WEBHOOK_SECRET=" prefix
nano secrets/telnyx_webhook_secret
chmod 600 secrets/telnyx_webhook_secret

docker compose up -d --build
curl -skS https://webhook.miswitch.cloud/health
```

## Local development

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi 'uvicorn[standard]' pynacl
```

Run without auth (development only):

```bash
WEBHOOK_ALLOW_NO_SECRET=1 WEBHOOK_DB_PATH=/tmp/telnyx-webhook.db \
  uvicorn app:app --host 127.0.0.1 --port 8787 --reload
```

Smoke tests:

```bash
# Health (no secret)
curl -s http://127.0.0.1:8787/health

# Simulate an insight delivery
curl -s http://127.0.0.1:8787/telnyx/insights \
  -H 'content-type: application/json' \
  -d '{"event_type":"conversation_insight_result","payload":{}}'

# List stored insights (requires secret when WEBHOOK_ALLOW_NO_SECRET is not set)
curl -s http://127.0.0.1:8787/telnyx/insights -H 'x-webhook-secret: your-secret'
```

Interactive API docs are available at `http://127.0.0.1:8787/docs` while the server is running.

## Configuration

Copy `.env.example` to `.env` and adjust values as needed. Compose reads `.env` for variable substitution.

| Variable | Default | Purpose |
|----------|---------|---------|
| `WEBHOOK_DB_PATH` | `/data/webhook.db` | SQLite database file for insight payloads |
| `WEBHOOK_SECRET` | `change-me-local-test` | Shared secret for protected endpoints |
| `WEBHOOK_SECRET_FILE` | — | Read secret from a file instead of `WEBHOOK_SECRET` (used in Compose) |
| `TELNYX_PUBLIC_KEY` | — | Telnyx Ed25519 public key for `/telnyx/insights` signature verification |
| `WEBHOOK_ALLOW_NO_SECRET` | `0` | Set to `1` to disable secret checks (local dev only) |
| `WEBHOOK_INSIGHTS_PATH` | `/data/insights.json` | Legacy JSON path; used only for one-time migration into SQLite |
| `TZ` | `America/Detroit` | Container timezone |
| `DOMAINNAME_1` | `miswitch.cloud` | Hostname suffix for Traefik routing |

### Shared secret

Production uses a Docker secret file:

```text
secrets/telnyx_webhook_secret
```

Compose mounts it through `WEBHOOK_SECRET_FILE`. Put **only** the raw secret string in that file.

Use the same value when inspecting stored payloads:

```text
x-webhook-secret: <secret>
```

### Telnyx public key

Set `TELNYX_PUBLIC_KEY` in `.env` to the Ed25519 verification key from the Telnyx Mission Control Portal (webhook/signing settings). This is a **public** key, not an API secret.

When configured, Telnyx can POST to the clean URL without `?secret=`:

```text
https://webhook.miswitch.cloud/telnyx/insights
```

## Data storage (SQLite)

Insight payloads are stored in a single SQLite file:

```text
./data/webhook.db
```

| Table | Contents | Retention |
|-------|----------|-----------|
| `insights` | Received Telnyx Insight Group payloads | Last **500** records (oldest pruned on insert) |

The `./data` directory is bind-mounted into the container at `/data`. Back up by copying `data/webhook.db`.

### Migrating from legacy JSON

Older deployments stored insights in `/data/insights.json`. On first startup, if the SQLite database is empty, the server automatically imports that file once.

To force a fresh import:

1. Stop the container.
2. Remove or rename `data/webhook.db`.
3. Ensure `data/insights.json` is still present.
4. Start the container again.

## Authentication

| Endpoint | Auth required | Accepted methods |
|----------|---------------|------------------|
| `GET /health` | No | — |
| `POST /telnyx/insights` | Yes | Telnyx Ed25519 signature **or** shared secret |
| `GET /telnyx/insights` | Yes | Shared secret header or `?secret=` query param |

Do **not** put OAuth, Authelia, or Cloudflare Access in front of this route unless Telnyx can satisfy that challenge. Use Telnyx signature verification and the shared secret for app-level protection instead.

## API reference

### `GET /health`

No authentication. Returns service status.

```bash
curl -s https://webhook.miswitch.cloud/health
```

Example response:

```json
{
  "ok": true,
  "service": "miswitch-telnyx-webhook",
  "host": "webhook.miswitch.cloud"
}
```

### `POST /telnyx/insights`

Telnyx Insight Group webhook receiver. Stores each payload in SQLite and returns:

```json
{"accepted": true, "id": "..."}
```

**Production URL (with `TELNYX_PUBLIC_KEY` configured):**

```text
https://webhook.miswitch.cloud/telnyx/insights
```

Telnyx signs requests with `telnyx-signature-ed25519` and `telnyx-timestamp`.

**Manual/curl fallback:**

```bash
curl -s https://webhook.miswitch.cloud/telnyx/insights?secret=<secret> \
  -H 'content-type: application/json' \
  -d '{"event_type":"conversation_insight_result","payload":{}}'
```

### `GET /telnyx/insights`

Lists stored insight records. Returns total `count` and the most recent **50** entries.

```bash
curl -s "https://webhook.miswitch.cloud/telnyx/insights?secret=<secret>"
```

Real Telnyx deliveries should show:

```json
"telnyx_signature_present": true,
"telnyx_signature_verified": true
```

For field-by-field documentation, result format variations, and full payload examples, see [docs/telnyx-insights.md](docs/telnyx-insights.md).

## Telnyx setup

### Insight Groups

In the Telnyx Mission Control Portal:

1. Copy the Ed25519 public key into `TELNYX_PUBLIC_KEY` in `.env`.
2. Set the Insight Group webhook URL to `https://webhook.<your-domain>/telnyx/insights`.
3. Redeploy so the container picks up the new key.

Verify delivery:

```bash
docker logs -f telnyx-webhook-server
```

A successful delivery logs:

```text
POST /telnyx/insights HTTP/1.1" 200 OK
```

Inspect stored records:

```bash
curl -skS "https://webhook.miswitch.cloud/telnyx/insights?secret=$(cat secrets/telnyx_webhook_secret)"
```

## Production deploy beside `~/docker`

This repo runs as its own Compose project while Traefik stays in a separate `~/docker` stack. The webhook container joins the external `t3_proxy` network so Traefik can route to it by Docker labels.

### 1. Clone or update

```bash
cd ~
git clone https://github.com/Maynardnaze/telnyx-webhook-server.git
cd ~/telnyx-webhook-server
cp .env.example .env
mkdir -p data secrets
```

### 2. Disable any old infra-managed copy

If `~/docker/docker-compose-gpu-server.yml` still includes an older webhook service, comment it out so only one container owns the hostname:

```yaml
# - compose/$HOSTNAME/telnyx-webhook-server.yml
```

Remove a stale container if needed:

```bash
docker stop telnyx-webhook-server 2>/dev/null || true
docker rm telnyx-webhook-server 2>/dev/null || true
```

### 3. Configure `.env`

```bash
nano ~/telnyx-webhook-server/.env
```

Minimum recommended values:

```env
TZ=America/Detroit
DOMAINNAME_1=miswitch.cloud
TELNYX_PUBLIC_KEY=PASTE_TELNYX_PUBLIC_KEY_HERE
```

### 4. Configure the shared secret

```bash
nano ~/telnyx-webhook-server/secrets/telnyx_webhook_secret
chmod 600 ~/telnyx-webhook-server/secrets/telnyx_webhook_secret
```

### 5. Deploy

```bash
cd ~/telnyx-webhook-server
docker compose up -d --build
```

### 6. Verify

```bash
docker exec telnyx-webhook-server printenv | grep -E 'TELNYX|WEBHOOK'
curl -skS https://webhook.miswitch.cloud/health
ls -lh ~/telnyx-webhook-server/data/webhook.db
```

`TELNYX_PUBLIC_KEY` should not be blank. `WEBHOOK_DB_PATH` should be `/data/webhook.db`.

## Traefik / Cloudflare Tunnel

Cloudflare Tunnel public hostname:

```text
webhook.miswitch.cloud
```

Origin service:

```text
https://traefik:444
```

Recommended TLS/origin settings:

```text
No TLS Verify: enabled
Origin Server Name: webhook.miswitch.cloud
```

Traefik route (set by Compose labels):

```text
Host(`webhook.miswitch.cloud`)
entrypoint: websecure-external
middleware: chain-no-auth@file
```

## Troubleshooting

| Symptom | Likely cause | What to check |
|---------|--------------|---------------|
| `401` on `POST /telnyx/insights` | Missing signature and secret | Set `TELNYX_PUBLIC_KEY`, or use `?secret=` for testing |
| `401` on `GET /telnyx/insights` | Wrong or missing secret | `secrets/telnyx_webhook_secret` and request header |
| Empty insight list | No deliveries yet | `docker logs telnyx-webhook-server` during a call |
| Database permission errors | `data/` not writable | `mkdir -p data` and check volume mount permissions |

## License

Private / internal Miswitch infrastructure. Adjust as needed for your deployment.
