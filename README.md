# Miswitch Telnyx Webhook Server

FastAPI webhook server for [Telnyx AI Assistants](https://telnyx.com/products/voice-ai): mall directory lookups, async tool callbacks, and Insight Group delivery.

Designed to run behind Traefik at a public hostname such as:

```text
https://webhook.miswitch.cloud
```

## What this server does

- **Synchronous directory lookup** — Telnyx tool calls `POST /telnyx/mall-directory/search` and get store matches immediately.
- **Async directory lookup** — Telnyx tool calls `POST /telnyx/tools/directory-lookup-async`; the server returns `200` right away and finishes work in the background.
- **Insight Group receiver** — Telnyx posts conversation insight results to `POST /telnyx/insights`.
- **Local persistence** — background jobs and received insights are stored in a SQLite database on disk.
- **App-level auth** — protected endpoints require a shared secret header (or Telnyx Ed25519 signature for insight delivery).

The async pattern matches the daily-cool Telnyx webhook harness: acknowledge fast, process later, inspect results through HTTP.

## Repository layout

```text
telnyx-webhook-server/
├── app.py                 # FastAPI application
├── tenants.json           # Mall directory data (read-only config)
├── docker-compose.yml     # Production Compose stack
├── Dockerfile
├── data/                  # Runtime data (gitignored except .gitkeep)
│   └── webhook.db         # SQLite database (created on first run)
└── secrets/
    └── telnyx_webhook_secret   # Shared secret for tools and inspection endpoints
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

For local testing without Traefik, use the smoke-test section below.

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

# Directory lookup
curl -s http://127.0.0.1:8787/telnyx/mall-directory/search \
  -H 'content-type: application/json' \
  -d '{"query":"apple"}'

# Async job
curl -s http://127.0.0.1:8787/telnyx/tools/directory-lookup-async \
  -H 'content-type: application/json' \
  -d '{"query":"foot locker"}'

# List jobs (requires secret when WEBHOOK_ALLOW_NO_SECRET is not set)
curl -s http://127.0.0.1:8787/jobs -H 'x-webhook-secret: your-secret'
```

Interactive API docs are available at `http://127.0.0.1:8787/docs` while the server is running.

## Configuration

Copy `.env.example` to `.env` and adjust values as needed. Compose reads `.env` for variable substitution.

| Variable | Default | Purpose |
|----------|---------|---------|
| `WEBHOOK_DB_PATH` | `/data/webhook.db` | SQLite database file for jobs and insights |
| `WEBHOOK_SECRET` | `change-me-local-test` | Shared secret for protected endpoints |
| `WEBHOOK_SECRET_FILE` | — | Read secret from a file instead of `WEBHOOK_SECRET` (used in Compose) |
| `DIRECTORY_WEBHOOK_SECRET` | — | Alias for `WEBHOOK_SECRET` (older name) |
| `DIRECTORY_WEBHOOK_SECRET_FILE` | — | File-based alias for the shared secret |
| `TELNYX_PUBLIC_KEY` | — | Telnyx Ed25519 public key for `/telnyx/insights` signature verification |
| `WEBHOOK_ALLOW_NO_SECRET` | `0` | Set to `1` to disable secret checks (local dev only) |
| `DIRECTORY_DATA` | `tenants.json` | Path to mall directory JSON |
| `GUEST_SERVICES_PHONE` | `+124****0000` | Fallback phone returned when no store match is found |
| `TELNYX_DIRECTORY_GUEST_SERVICES_PHONE` | — | Compose alias mapped to `GUEST_SERVICES_PHONE` |
| `ASYNC_MOCK_DELAY_SECONDS` | `0.25` | Artificial delay for async directory jobs |
| `WEBHOOK_JOBS_PATH` | `/data/jobs.json` | Legacy JSON path; used only for one-time migration into SQLite |
| `WEBHOOK_INSIGHTS_PATH` | `/data/insights.json` | Legacy JSON path; used only for one-time migration into SQLite |
| `TZ` | `America/Detroit` | Container timezone |
| `DOMAINNAME_1` | `miswitch.cloud` | Hostname suffix for Traefik routing |

### Shared secret

Production uses a Docker secret file:

```text
secrets/telnyx_webhook_secret
```

Compose mounts it through `WEBHOOK_SECRET_FILE` and `DIRECTORY_WEBHOOK_SECRET_FILE`. Put **only** the raw secret string in that file.

Telnyx AI Assistant tools should send the same value in a request header:

```text
x-webhook-secret: <secret>
```

`x-directory-secret` is also accepted for compatibility with older prototypes.

### Telnyx public key

Set `TELNYX_PUBLIC_KEY` in `.env` to the Ed25519 verification key from the Telnyx Mission Control Portal (webhook/signing settings). This is a **public** key, not an API secret.

When configured, Insight Group webhooks can use the clean URL without `?secret=`:

```text
https://webhook.miswitch.cloud/telnyx/insights
```

## Data storage (SQLite)

Runtime writes go to a single SQLite file, not JSON.

| Table | Contents | Retention |
|-------|----------|-----------|
| `jobs` | Async directory lookup job state and results | Kept until manually deleted |
| `insights` | Received Telnyx Insight Group payloads | Last **500** records (oldest pruned on insert) |

Default path in Docker:

```text
./data/webhook.db
```

The `./data` directory is bind-mounted into the container at `/data`. Back up by copying `data/webhook.db`.

### Migrating from legacy JSON files

Older deployments stored data in:

```text
/data/jobs.json
/data/insights.json
```

On first startup, if the SQLite database is empty, the server automatically imports those files once. After migration, new data is written only to SQLite.

To force a fresh import:

1. Stop the container.
2. Remove or rename `data/webhook.db`.
3. Ensure legacy JSON files are still present in `data/`.
4. Start the container again.

### Mall directory data

Store listings are **not** stored in SQLite. They live in `tenants.json` (or a custom path via `DIRECTORY_DATA`). Edit that file and restart the container to change directory entries. Rebuild the image if you changed the bundled `tenants.json` copied in the Dockerfile.

## Authentication

| Endpoint | Auth required | Accepted methods |
|----------|---------------|------------------|
| `GET /health` | No | — |
| `POST /telnyx/insights` | Yes | Telnyx Ed25519 signature **or** shared secret |
| `GET /telnyx/insights` | Yes | Shared secret header or `?secret=` query param |
| `POST /telnyx/mall-directory/search` | Yes | Shared secret header |
| `POST /telnyx/tools/directory-lookup-async` | Yes | Shared secret header |
| `GET /jobs`, `GET /jobs/{job_id}` | Yes | Shared secret header |

Do **not** put OAuth, Authelia, or Cloudflare Access in front of this route unless Telnyx can satisfy that challenge. Use the shared secret and Telnyx signature verification for app-level protection instead.

## API reference

### `GET /health`

No authentication. Returns service status and tenant count.

```bash
curl -s https://webhook.miswitch.cloud/health
```

Example response:

```json
{
  "ok": true,
  "service": "miswitch-telnyx-webhook",
  "host": "webhook.miswitch.cloud",
  "tenant_count": 5
}
```

### `POST /telnyx/mall-directory/search`

Synchronous mall directory lookup for Telnyx AI Assistant tools.

Headers:

```text
x-webhook-secret: <secret>
content-type: application/json
```

Body:

```json
{
  "query": "apple",
  "caller_intent": "transfer",
  "caller_number": "+12485551212"
}
```

The response includes `status` (`single_match`, `multiple_matches`, or `no_match`), ranked `matches`, and a `fallback` guest-services entry.

### `POST /telnyx/tools/directory-lookup-async`

Async directory lookup. Returns immediately; work continues in the background.

Headers:

```text
x-webhook-secret: <secret>
x-telnyx-call-control-id: <optional call control id>
content-type: application/json
```

Body:

```json
{
  "query": "journeys",
  "caller_number": "+12485551212"
}
```

Immediate response:

```json
{
  "accepted": true,
  "job_id": "abc123...",
  "dry_run": true
}
```

Poll the job record:

```bash
curl -s https://webhook.miswitch.cloud/jobs/<job_id> \
  -H 'x-webhook-secret: <secret>'
```

Completed jobs include a `would_inject` object describing the system message that would be sent through Telnyx Add Messages API. Live Add Messages calls remain disabled (`dry_run: true`) until the exact production payload is confirmed.

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

### `GET /jobs` and `GET /jobs/{job_id}`

Inspect async directory lookup jobs.

```bash
curl -s https://webhook.miswitch.cloud/jobs -H 'x-webhook-secret: <secret>'
curl -s https://webhook.miswitch.cloud/jobs/<job_id> -H 'x-webhook-secret: <secret>'
```

## Telnyx setup

### AI Assistant tools

Point Telnyx tool URLs at this server:

| Tool type | Method | URL |
|-----------|--------|-----|
| Sync directory lookup | `POST` | `https://webhook.<your-domain>/telnyx/mall-directory/search` |
| Async directory lookup | `POST` | `https://webhook.<your-domain>/telnyx/tools/directory-lookup-async` |

Add the shared secret as a custom header in the Telnyx tool configuration:

```text
x-webhook-secret: <your-secret>
```

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

If the repo already exists:

```bash
cd ~/telnyx-webhook-server
git pull
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
TELNYX_DIRECTORY_GUEST_SERVICES_PHONE=+12400000000
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

Confirm Compose project path:

```bash
docker inspect telnyx-webhook-server \
  --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}'
```

Check environment wiring:

```bash
docker exec telnyx-webhook-server printenv | grep -E 'TELNYX|WEBHOOK'
```

`TELNYX_PUBLIC_KEY` should not be blank. `WEBHOOK_DB_PATH` should be `/data/webhook.db`.

Test the public route:

```bash
curl -skS https://webhook.miswitch.cloud/health
```

Confirm the database file exists on the host:

```bash
ls -lh ~/telnyx-webhook-server/data/webhook.db
```

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
| `401` on tool calls | Wrong or missing secret | `secrets/telnyx_webhook_secret` and Telnyx tool header `x-webhook-secret` |
| `401` on `/telnyx/insights` | Missing signature and secret | Set `TELNYX_PUBLIC_KEY`, or use `?secret=` for testing |
| Empty `/telnyx/insights` list | No deliveries yet, or wrong secret on GET | `docker logs telnyx-webhook-server` during a call |
| `tenant_count: 0` | Bad `tenants.json` path | `DIRECTORY_DATA` and file mount / image copy |
| Database permission errors | `data/` not writable | `mkdir -p data` and check volume mount permissions |
| Jobs never complete | Background worker error | `GET /jobs/{job_id}` and container logs |

## License

Private / internal Miswitch infrastructure. Adjust as needed for your deployment.
