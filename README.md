# Miswitch Telnyx Webhook Server

Production-oriented FastAPI webhook server for Telnyx AI Assistant tools, intended to be published at:

```text
https://webhook.miswitch.cloud
```

It follows the daily-cool Telnyx async webhook harness pattern:

- return fast `200` acknowledgements for async tools
- track background jobs locally in `/data/jobs.json`
- expose a synchronous mall-directory lookup tool
- keep the public Traefik route out from behind OAuth/Authelia
- require a shared secret header from Telnyx

## Endpoints

### Health

```text
GET /health
```

No secret required. Returns service status and tenant count.

### Synchronous directory lookup

```text
POST /telnyx/mall-directory/search
```

Headers:

```text
x-webhook-secret: <secret>
```

`x-directory-secret` is also accepted for compatibility with the older prototype.

Example body:

```json
{
  "query": "apple",
  "caller_intent": "transfer",
  "caller_number": "+12485551212"
}
```

### Async directory lookup tool

```text
POST /telnyx/tools/directory-lookup-async
```

Headers:

```text
x-webhook-secret: <secret>
x-telnyx-call-control-id: <optional Telnyx call control id>
```

Returns immediately:

```json
{
  "accepted": true,
  "job_id": "...",
  "dry_run": true
}
```

The background worker stores a `would_inject` system-message object in the job record. Live Telnyx Add Messages API calls should stay disabled until the exact account/API payload is confirmed.

### Insight Groups receiver

Use this as the Telnyx AI Insight Group webhook URL when `TELNYX_PUBLIC_KEY` is configured:

```text
https://webhook.miswitch.cloud/telnyx/insights
```

For curl/manual testing, or if Telnyx signature verification is not configured yet, use the shared-secret fallback:

```text
https://webhook.miswitch.cloud/telnyx/insights?secret=<secret>
```

The receiver accepts either a valid Telnyx Ed25519 signature (`telnyx-signature-ed25519` + `telnyx-timestamp`) or the shared secret.

The receiver appends incoming insight payloads to `/data/insights.json` and returns:

```json
{"accepted": true, "id": "..."}
```

Inspect recent received insight payloads with:

```text
GET /telnyx/insights
```

Requires the same secret header or `?secret=<secret>` query string.

### Job inspection

```text
GET /jobs
GET /jobs/{job_id}
```

Requires the same secret header.

## Traefik / Cloudflare Tunnel

Cloudflare Tunnel public hostname:

```text
webhook.miswitch.cloud
```

Origin service:

```text
https://traefik:444
```

TLS/origin settings:

```text
No TLS Verify: enabled
Origin Server Name: webhook.miswitch.cloud
```

Traefik route:

```text
Host(`webhook.miswitch.cloud`)
entrypoint: websecure-external
middleware: chain-no-auth@file
```

Do **not** add OAuth/Authelia/Cloudflare Access to this route unless Telnyx can be configured to satisfy it. Use `x-webhook-secret` for app-level auth.

## Local smoke test

```bash
WEBHOOK_ALLOW_NO_SECRET=1 WEBHOOK_JOBS_PATH=/tmp/telnyx-webhook-jobs.json \
  uvicorn app:app --host 127.0.0.1 --port 8787
```

Then:

```bash
curl -s http://127.0.0.1:8787/health
curl -s http://127.0.0.1:8787/telnyx/mall-directory/search \
  -H 'content-type: application/json' \
  -d '{"query":"apple"}'
```

## Production deploy: standalone repo beside `~/docker`

This repo is intended to run as its own Compose project at:

```text
~/telnyx-webhook-server
```

Keep the main Docker-Traefik repo separate at:

```text
~/docker
```

The standalone webhook container joins the existing external `t3_proxy` network, so the Traefik instance from `~/docker` can route to it by Docker labels.

### 1. Clone/update this app repo

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

### 2. Make sure the old `~/docker` include is disabled

If `~/docker/docker-compose-gpu-server.yml` still includes the old infra-managed service, comment it out so one Traefik router/container owns `webhook.miswitch.cloud`:

```yaml
# - compose/$HOSTNAME/telnyx-webhook-server.yml
```

If the old container is still running, remove it before starting this standalone app:

```bash
docker stop telnyx-webhook-server 2>/dev/null || true
docker rm telnyx-webhook-server 2>/dev/null || true
```

### 3. Configure runtime env

Edit the standalone app env file:

```bash
nano ~/telnyx-webhook-server/.env
```

Minimum recommended values:

```env
TZ=America/Detroit
DOMAINNAME_1=miswitch.cloud
TELNYX_PUBLIC_KEY=PASTE_TELNYX_PUBLIC_KEY_HERE
```

`TELNYX_PUBLIC_KEY` is Telnyx's public Ed25519 verification key from Mission Control Portal. It is a public key, not an API secret, so storing it in `.env` is fine.

### 4. Configure the shared-secret fallback

Create the shared-secret file used by directory tools, the read-only `/jobs` and `/telnyx/insights` inspection endpoints, and curl fallback tests:

```bash
nano ~/telnyx-webhook-server/secrets/telnyx_webhook_secret
chmod 600 ~/telnyx-webhook-server/secrets/telnyx_webhook_secret
```

Put only the secret value in the file. Do not include `WEBHOOK_SECRET=`.

### 5. Deploy standalone

```bash
cd ~/telnyx-webhook-server
docker compose up -d --build
```

### 6. Verify it is running from the standalone repo

```bash
docker inspect telnyx-webhook-server \
  --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}'
```

Expected:

```text
/home/andrew/telnyx-webhook-server
```

Check env wiring:

```bash
docker exec telnyx-webhook-server printenv | grep -E 'TELNYX|WEBHOOK'
```

`TELNYX_PUBLIC_KEY` should not be blank.

Then test from anywhere:

```bash
curl -skS https://webhook.miswitch.cloud/health
```

Expected: HTTP 200 with service status.

### 7. Confirm Telnyx Insight Group delivery

The clean Insight Group webhook URL should be:

```text
https://webhook.miswitch.cloud/telnyx/insights
```

Watch logs during/after a call:

```bash
docker logs -f telnyx-webhook-server
```

A successful Telnyx delivery should show:

```text
POST /telnyx/insights HTTP/1.1" 200 OK
```

Rejected signature/auth attempts show `401 Unauthorized`.

Inspect accepted records with the shared secret:

```bash
curl -skS "https://webhook.miswitch.cloud/telnyx/insights?secret=$(cat ~/telnyx-webhook-server/secrets/telnyx_webhook_secret)"
```

New real Telnyx records should include:

```json
"telnyx_signature_present": true,
"telnyx_signature_verified": true
```
