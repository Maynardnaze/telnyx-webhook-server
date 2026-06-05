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

## Production deploy

Clone this repo as a standalone app next to the main Docker-Traefik stack:

```bash
cd ~
git clone https://github.com/Maynardnaze/telnyx-webhook-server.git
cd ~/telnyx-webhook-server
cp .env.example .env
mkdir -p data secrets
```

Create the shared-secret file used by directory tools and curl fallback tests:

```bash
nano secrets/telnyx_webhook_secret
```

Optional but recommended: add your Telnyx public key to `.env` as `TELNYX_PUBLIC_KEY=...` so Insight Groups can post to the clean URL without `?secret=`.

Deploy with the standalone compose file:

```bash
docker compose up -d --build
```

This compose project attaches to the existing external `t3_proxy` network, so the main Traefik instance in `~/docker` can route to it by label.

Then test from anywhere:

```bash
curl -skS https://webhook.miswitch.cloud/health
```

Expected: HTTP 200 with service status.
