# Doppler Migration Guide

This guide describes how to switch this repository to use [Doppler](https://www.doppler.com/) as the source of truth for all runtime configuration and secrets.

Target outcome:

- No committed or long-lived .env file with app config.
- No required ./secrets/* files for Telnyx/webhook secrets.
- Local development, production deploys, and CI all read the same named variables from Doppler.
- Docker Compose still owns containers, volumes, Traefik labels, and networks.
- Doppler owns environment-specific values.

> Current repo context: this app already reads most settings from environment variables in app.py. It also supports *_FILE for WEBHOOK_SECRET, DIRECTORY_WEBHOOK_SECRET, and TELNYX_API_KEY. The cleanest Doppler migration is to stop using Docker secret files for this app and pass those values as normal environment variables injected by Doppler.

---

## 1. Decide the Doppler project/config layout

Recommended layout:

| Environment | Doppler project       | Doppler config | Used by                                         |
|-------------|-----------------------|----------------|-------------------------------------------------|
| Local dev   | telnyx-webhook-server | dev            | Developer shells, local uvicorn, local Compose  |
| Staging     | telnyx-webhook-server | stg            | Pre-prod testing on the staging host            |
| Production  | telnyx-webhook-server | prd            | Host running docker compose up                  |
| CI/test     | telnyx-webhook-server | ci             | GitHub Actions, smoke tests                     |

Use one project because the variable names are the same everywhere; use separate configs so values differ safely.

---

## 2. Create the full variable inventory in Doppler

Create these secrets/config values in Doppler. Include non-secret config too if the goal is to use Doppler fully.

| Name | Required | Secret? | Current source | Notes |
|---|---:|---:|---|---|
| TZ | Yes | No | .env.example | Usually America/Detroit |
| DOMAINNAME_1 | Yes | No | .env.example | Usually miswitch.cloud; used by Traefik labels |
| WEBHOOK_SECRET | Yes | Yes | secrets/telnyx_webhook_secret or env | Admin login + protected API endpoints |
| TELNYX_PUBLIC_KEY | Production: yes | No | .env | Telnyx Ed25519 webhook signing public key |
| TELNYX_API_KEY | Only outbound Telnyx API/SMS tools | Yes | secrets/telnyx_api_key or env | Used for Add Messages / SMS helper code |
| ASSISTANT_MEMORY_FAMILY | No | No | Compose default | Default: miswitch-ai-assistants |
| ASSISTANT_MEMORY_LIMIT | No | No | Compose default | Default: 5 |
| ASSISTANT_MEMORY_INSIGHT_QUERY | No | No | Compose default | Optional query/filter string |
| ASSISTANT_MEMORY_PROFILES | No | No | Compose default | Optional profile list |
| WEBHOOK_ALLOW_NO_SECRET | Local dev / CI only | No | local env | Set 1 only for local isolated dev or CI; never production |

Usually keep these as app-owned constants in Compose instead of Doppler:

| Name | Value | Why |
|---|---|---|
| WEBHOOK_DB_PATH | /data/webhook.db | Container path tied to the bind-mounted volume |
| WEBHOOK_INSIGHTS_PATH | /data/insights.json | Legacy import path; default already matches container data path |

### Import existing values

From the production repo directory, after logging in to Doppler and selecting the project/config:

```bash
cd ~/telnyx-webhook-server

# Public/non-secret config from the old .env file, if present.
doppler secrets set TZ="America/Detroit"
doppler secrets set DOMAINNAME_1="miswitch.cloud"
doppler secrets set TELNYX_PUBLIC_KEY="PASTE_TELNYX_PUBLIC_KEY_HERE"

# Existing secret files. These commands read only the raw file values.
doppler secrets set WEBHOOK_SECRET="$(cat secrets/telnyx_webhook_secret)"

# Optional: only if the file exists and outbound Telnyx API features are enabled.
[ -f secrets/telnyx_api_key ] && doppler secrets set TELNYX_API_KEY="$(cat secrets/telnyx_api_key)"

# Optional app knobs.
doppler secrets set ASSISTANT_MEMORY_FAMILY="miswitch-ai-assistants"
doppler secrets set ASSISTANT_MEMORY_LIMIT="5"
```

Verify names without printing values:

```bash
doppler secrets --only-names
```

---

## 3. Install and authenticate Doppler CLI

### Local developer machine

```bash
# Install from Doppler's official instructions for the OS, then:
doppler login
doppler setup --project telnyx-webhook-server --config dev
```

Check the selected project/config:

```bash
doppler configure
```

### Production host

Use a read-only service token scoped to the production config.

Recommended pattern:

1. In Doppler, create a service token for project telnyx-webhook-server, config prd.
2. Store the token outside the repo, root-readable only.
3. Use it only to start/recreate the Compose stack.

Example host file:

```bash
sudo install -m 0700 -o root -g root -d /etc/doppler
sudo sh -c 'printf %s "dp.st.xxxxx" > /etc/doppler/telnyx-webhook-server.token'
sudo chmod 0600 /etc/doppler/telnyx-webhook-server.token
```

Do not commit service tokens, .env files containing secrets, or downloaded Doppler secret dumps.

---

## 4. Update Docker Compose to expect Doppler-injected env vars

Doppler's Docker Compose env-var approach only passes variables explicitly listed under environment:. Update the service to list every variable it should receive.

Recommended docker-compose.yml service environment block:

```yaml
services:
  telnyx-webhook-server:
    container_name: telnyx-webhook-server
    build:
      context: .
    restart: unless-stopped
    networks:
      - t3_proxy
    environment:
      TZ: ${TZ:-America/Detroit}
      WEBHOOK_DB_PATH: /data/webhook.db
      WEBHOOK_SECRET: ${WEBHOOK_SECRET:?WEBHOOK_SECRET must be supplied by Doppler}
      TELNYX_PUBLIC_KEY: ${TELNYX_PUBLIC_KEY:?TELNYX_PUBLIC_KEY must be supplied by Doppler}
      TELNYX_API_KEY: ${TELNYX_API_KEY:-}
      ASSISTANT_MEMORY_FAMILY: ${ASSISTANT_MEMORY_FAMILY:-miswitch-ai-assistants}
      ASSISTANT_MEMORY_LIMIT: ${ASSISTANT_MEMORY_LIMIT:-5}
      ASSISTANT_MEMORY_INSIGHT_QUERY: ${ASSISTANT_MEMORY_INSIGHT_QUERY:-}
      ASSISTANT_MEMORY_PROFILES: ${ASSISTANT_MEMORY_PROFILES:-}
    volumes:
      - ./data:/data
    labels:
      - "traefik.enable=true"
      - "traefik.docker.network=t3_proxy"
      - "traefik.http.routers.telnyx-webhook-rtr.rule=Host(`webhook.${DOMAINNAME_1:-miswitch.cloud}`)"
      - "traefik.http.routers.telnyx-webhook-rtr.entrypoints=websecure-external"
      - "traefik.http.routers.telnyx-webhook-rtr.tls=true"
      - "traefik.http.routers.telnyx-webhook-rtr.middlewares=chain-no-auth@file"
      - "traefik.http.routers.telnyx-webhook-rtr.service=telnyx-webhook-svc"
      - "traefik.http.services.telnyx-webhook-svc.loadbalancer.server.port=8787"
```

Key changes from the current Compose file:

- Replace WEBHOOK_SECRET_FILE: /run/secrets/telnyx_webhook_secret with WEBHOOK_SECRET: ${WEBHOOK_SECRET:?...}.
- Replace TELNYX_API_KEY_FILE: /run/secrets/telnyx_api_key with TELNYX_API_KEY: ${TELNYX_API_KEY:-}.
- Remove the ./secrets:/run/secrets:ro volume once no other file-based secrets are needed.
- Keep WEBHOOK_DB_PATH: /data/webhook.db and ./data:/data unchanged.

> Why not keep *_FILE? Doppler can mount ephemeral files, but this repo already supports direct env vars. Direct env injection is simpler for Compose and avoids managing generated secret files. If a future dependency requires file-based secrets, use doppler run --mount for that specific file.

---

## 5. Update .env.example and gitignore expectations

After Doppler is the source of truth, .env.example should become a minimal developer hint, not the canonical config list.

Suggested .env.example replacement:

```env
# Runtime config is managed in Doppler.
# Local dev:
#   doppler setup --project telnyx-webhook-server --config dev
#   doppler run -- docker compose up -d --build
#
# Production:
#   DOPPLER_TOKEN=$(cat /etc/doppler/telnyx-webhook-server.token) \
#     doppler run --project telnyx-webhook-server --config prd -- docker compose up -d --build
#
# Keep this file secret-free. Do not put WEBHOOK_SECRET, TELNYX_API_KEY,
# service tokens, or downloaded Doppler secret dumps here.
```

Confirm .gitignore ignores:

```gitignore
.env
.env.*
!.env.example
secrets/
```

If historical commits contain real secrets, rotate those secrets in Doppler. Do not rely on deleting local files as a rotation strategy.

---

## 6. Run locally with Doppler

### Docker Compose local smoke test

```bash
cd ~/telnyx-webhook-server
doppler setup --project telnyx-webhook-server --config dev

doppler run -- docker compose up -d --build
curl -s http://127.0.0.1:8787/health || docker logs --tail=100 telnyx-webhook-server
```

If the service is only reachable through Traefik on the production host, use the public health check instead:

```bash
curl -skS https://webhook.miswitch.cloud/health
```

### Local uvicorn development

```bash
cd ~/telnyx-webhook-server
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi 'uvicorn[standard]' pynacl

# Local dev can choose either a real WEBHOOK_SECRET or WEBHOOK_ALLOW_NO_SECRET=1 in the Doppler dev config.
doppler run -- uvicorn app:app --host 127.0.0.1 --port 8787 --reload
```

---

## 7. Deploy production with Doppler

Manual deploy:

```bash
cd ~/telnyx-webhook-server
git pull --ff-only

DOPPLER_TOKEN="$(sudo cat /etc/doppler/telnyx-webhook-server.token)" \
  doppler run --project telnyx-webhook-server --config prd -- \
  docker compose up -d --build
```

Verify container env without printing secret values:

```bash
docker exec telnyx-webhook-server sh -lc '
  for name in TZ DOMAINNAME_1 WEBHOOK_DB_PATH WEBHOOK_SECRET TELNYX_PUBLIC_KEY TELNYX_API_KEY ASSISTANT_MEMORY_FAMILY ASSISTANT_MEMORY_LIMIT; do
    if [ -n "$(printenv "$name")" ]; then
      printf "%s=set\n" "$name"
    else
      printf "%s=MISSING\n" "$name"
    fi
  done
'
```

Expected:

- WEBHOOK_SECRET=set
- TELNYX_PUBLIC_KEY=set in production
- TELNYX_API_KEY=set only if outbound Telnyx API/SMS features are enabled
- WEBHOOK_DB_PATH=set

Health check:

```bash
curl -skS https://webhook.miswitch.cloud/health
```

Protected endpoint check without exposing the secret in shell history:

```bash
DOPPLER_TOKEN="$(sudo cat /etc/doppler/telnyx-webhook-server.token)" \
  doppler run --project telnyx-webhook-server --config prd --command '
    curl -skS "https://webhook.miswitch.cloud/telnyx/insights" \
      -H "x-webhook-secret: $WEBHOOK_SECRET" | head -c 500
  '
```

---

## 8. Optional: systemd deploy wrapper for production

Use this if the host needs a consistent one-command service refresh after reboot or code update.

Create /usr/local/sbin/deploy-telnyx-webhook-server:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /home/USER/telnyx-webhook-server
export DOPPLER_TOKEN="$(cat /etc/doppler/telnyx-webhook-server.token)"
exec doppler run --project telnyx-webhook-server --config prd -- docker compose up -d --build
```

Secure it:

```bash
sudo chmod 0750 /usr/local/sbin/deploy-telnyx-webhook-server
```

Optional systemd unit /etc/systemd/system/telnyx-webhook-server-deploy.service:

```ini
[Unit]
Description=Deploy Telnyx Webhook Server with Doppler-managed config
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/deploy-telnyx-webhook-server
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

Enable/run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable telnyx-webhook-server-deploy.service
sudo systemctl start telnyx-webhook-server-deploy.service
sudo systemctl status telnyx-webhook-server-deploy.service --no-pager
```

Note: Docker's restart: unless-stopped can restart the already-created container without Doppler because the injected env values are stored in the container configuration. Doppler is required when the container is recreated, rebuilt, or the Compose project is updated.

---

## 9. CI/GitHub Actions pattern

Store only the Doppler service token in GitHub Actions secrets, for example:

```text
DOPPLER_TOKEN_TELNYX_WEBHOOK_CI
```

Then run commands through Doppler:

```yaml
name: test

on:
  pull_request:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    env:
      DOPPLER_TOKEN: ${{ secrets.DOPPLER_TOKEN_TELNYX_WEBHOOK_CI }}
    steps:
      - uses: actions/checkout@v4
      - uses: dopplerhq/cli-action@v3
      - run: doppler run --project telnyx-webhook-server --config ci -- python -m pytest
```

The CI service token must be scoped to the ci config only. Do not give CI access to the prd config.

---

## 10. Cutover checklist

- [ ] Create Doppler project telnyx-webhook-server.
- [ ] Confirm configs exist: dev, stg, prd, ci.
- [ ] Add all variables listed in section 2.
- [ ] Create a read-only production service token (config prd).
- [ ] Create a CI service token scoped to the ci config.
- [ ] Store the production token outside the repo, e.g. /etc/doppler/telnyx-webhook-server.token.
- [ ] Update docker-compose.yml to use direct env vars, not *_FILE vars.
- [ ] Remove the ./secrets:/run/secrets:ro mount if no file secrets remain.
- [ ] Replace .env.example with Doppler instructions.
- [ ] Run local Compose via doppler run -- docker compose up -d --build.
- [ ] Deploy production via DOPPLER_TOKEN=... doppler run --project ... --config prd -- docker compose up -d --build.
- [ ] Verify /health.
- [ ] Verify protected endpoint using WEBHOOK_SECRET from Doppler.
- [ ] Confirm Telnyx Insight webhook deliveries still verify signatures.
- [ ] Rotate any secrets that ever lived in committed files or shared terminals.
- [ ] Delete old local secrets/telnyx_webhook_secret, secrets/telnyx_api_key, and .env only after production is verified.

---

## 11. Rollback plan

If Doppler cutover fails during deploy:

1. Restore the previous docker-compose.yml from git.
2. Restore the previous .env and ./secrets/* files from the host backup, not from chat logs or GitHub.
3. Recreate the container:

```bash
   docker compose up -d --build
```

4. Verify:

```bash
   curl -skS https://webhook.miswitch.cloud/health
   docker logs --tail=100 telnyx-webhook-server
```

Rollback should be temporary. Fix the Doppler variable/config mismatch, then rerun the cutover.

---

## 12. Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Compose errors with WEBHOOK_SECRET must be supplied by Doppler | Running docker compose without doppler run, wrong project/config, or missing secret | Run doppler configure, then doppler secrets --only-names; use doppler run -- docker compose ... |
| Container starts but admin login fails | WEBHOOK_SECRET value differs from the old secret | Use the Doppler value for login/API checks or update the secret intentionally |
| Telnyx webhooks return 401/403 | Missing/wrong TELNYX_PUBLIC_KEY, or Telnyx is not signing as expected | Verify TELNYX_PUBLIC_KEY=set; check logs for signature verification fields |
| Traefik route uses wrong hostname | DOMAINNAME_1 missing from Doppler at Compose render time | Add DOMAINNAME_1 to Doppler or pass it explicitly during deploy |
| Outbound SMS/Add Messages fails | TELNYX_API_KEY missing | Add TELNYX_API_KEY to Doppler prd config and recreate the container |
| Variable exists in Doppler but not in the container | Variable not listed in Compose environment: | Add it to the explicit environment: block and recreate container |
| Secrets appear in docker inspect | Expected with env-var injection | Limit host/Docker access; use ephemeral mounts only for secrets that must not appear in container metadata |

---

## References

- Doppler Docker Compose docs: <https://docs.doppler.com/docs/docker-compose>
- Doppler secrets access guide: <https://docs.doppler.com/docs/accessing-secrets>
- Doppler container env vars: <https://docs.doppler.com/docs/docker-container-env-vars>