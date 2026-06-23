# Telnyx Webhook Server Web Frontend Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a private admin web frontend for Andrew to inspect Telnyx webhook traffic, review Insight Group payloads, test assistant initialization payloads, and operate the `telnyx-webhook-server` without using curl or raw SQLite.

**Architecture:** Keep the frontend integrated into the existing FastAPI backend as a private `/admin` UI served by the same container and hostname. Use minimal server-rendered/static assets first, with JSON API endpoints under `/admin/api/*`; avoid a separate Node/Vite service until the UI needs heavier interactivity.

**Tech Stack:** FastAPI, Jinja2 templates, vanilla JavaScript or small HTMX-style progressive enhancement, SQLite, existing Docker/Traefik deployment. Optional dependency: `jinja2` only. No external SaaS or external writes in Phase 1.

---

## Current Backend Context

Repo: `/home/hermes/telnyx-webhook-server`

Current app shape inspected on 2026-06-23:

- `app.py` FastAPI app, version `0.2.0`
- SQLite database path: `WEBHOOK_DB_PATH`, default `/data/webhook.db`
- Current table: `insights(id, received_at, data)`
- Current public endpoints:
  - `GET /health`
  - `POST /telnyx/assistant/init`
  - `POST /telnyx/insights`
  - `GET /telnyx/insights`
- Auth model:
  - Telnyx webhook posts: Telnyx Ed25519 signature or shared secret fallback
  - Read endpoints: shared secret via `x-webhook-secret` or `?secret=`
- Production route: `https://webhook.miswitch.cloud`
- Existing deployment pattern: standalone repo/container sharing Traefik `t3_proxy`

## Product Scope

### Phase 1: Personal admin viewer

Build the smallest useful dashboard for Andrew:

1. Login page using the existing shared secret.
2. Dashboard summary: health, record count, latest received time, signature status counts.
3. Insight list: recent records, searchable/filterable client-side.
4. Insight detail: pretty JSON, extracted useful fields, copy buttons.
5. Assistant init tester: paste sample payload, call local `/telnyx/assistant/init`, inspect response.
6. Webhook simulator: post sample Insight payload into local backend in dev mode or secret-auth mode.

### Phase 2: Operational QA helpers

1. Normalized call cards: caller, assistant ID, event type, summary, action.
2. Basic labels/status: reviewed, follow-up needed, ignored.
3. Download/export JSON and Markdown review note.
4. Filter by assistant/customer/event type/date.
5. Problem-call flags: short call, unresolved, transfer failure, no user messages, low confidence.

### Phase 3: Multi-customer/operator dashboard

1. Tenant/customer metadata management.
2. Per-assistant configuration view.
3. Safe tool-test harnesses for SMS and dynamic variables.
4. Optional live actions guarded by explicit confirmation.
5. User auth beyond shared-secret login if more people use it.

## Key Design Decisions

### Decision 1: Serve frontend from FastAPI, not separate app yet

For the first private version, add templates/static files directly to the FastAPI service:

```text
telnyx-webhook-server/
├── app.py
├── templates/
│   ├── admin_base.html
│   ├── admin_login.html
│   ├── admin_dashboard.html
│   ├── admin_insights.html
│   └── admin_insight_detail.html
├── static/
│   ├── admin.css
│   └── admin.js
└── tests/
    └── test_admin_ui.py
```

This avoids another container, build chain, npm audit surface, and Traefik router while Andrew is the only user.

### Decision 2: Cookie session from shared secret

Browsers cannot conveniently set `x-webhook-secret` for normal navigation. Add a lightweight admin login:

- `GET /admin/login` renders form.
- `POST /admin/login` accepts shared secret.
- On success, set `admin_session` signed cookie.
- `POST /admin/logout` clears cookie.
- Admin routes require a valid signed cookie.

Use stdlib HMAC signing with `WEBHOOK_SECRET`; no new auth DB in Phase 1.

### Decision 3: Admin routes separate from Telnyx routes

Keep machine-facing Telnyx endpoints stable. Add browser-facing routes under:

```text
/admin
/admin/login
/admin/insights
/admin/insights/{id}
/admin/tools/assistant-init
/admin/tools/webhook-simulator
/admin/api/insights
/admin/api/insights/{id}
/admin/api/stats
```

Do not change `/telnyx/insights` behavior just to support the UI.

### Decision 4: Redaction by default

The UI should default to redacted display for phone numbers/transcripts where practical, with a clear toggle for raw JSON only after authentication.

Phase 1 can show raw JSON because it is Andrew-only, but prepare helpers like:

```python
def redact_for_display(value: Any) -> Any:
    ...
```

## Information Architecture

### `/admin` dashboard

Cards:

- Service health
- SQLite DB path/status
- Insight count
- Latest delivery time
- Signature verified count
- Shared-secret fallback count
- Last 10 events/insights

Primary buttons:

- View Insights
- Test Assistant Init
- Simulate Insight Webhook
- Open FastAPI Docs

### `/admin/insights`

Table columns:

- Received at
- Event type
- Assistant ID if present
- Conversation ID if present
- Caller/from if present
- Signature verified
- Shared-secret verified
- Summary/extracted label
- Detail link

Client-side controls:

- search box
- event type filter
- verified only toggle
- date range later

### `/admin/insights/{id}`

Sections:

1. Metadata card
2. Extracted fields
3. Raw JSON viewer
4. Copy JSON button
5. Potential action suggestions later

### `/admin/tools/assistant-init`

Form:

- Assistant ID override
- Customer ID override
- Tenant ID override
- Environment
- JSON payload textarea
- Submit button

Output:

- HTTP status
- dynamic variables
- conversation metadata
- memory query
- raw JSON response

### `/admin/tools/webhook-simulator`

Form:

- Sample type: blank, Insight Group, assistant init, simple event
- JSON textarea
- Secret mode: session secret only; do not expose real secret in page
- Submit to internal handler

Output:

- accepted/id
- stored record link
- raw response

## Data Model Changes

Phase 1 can reuse the existing `insights` table.

Recommended Phase 2 migration:

```sql
CREATE TABLE IF NOT EXISTS insight_reviews (
    insight_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (insight_id) REFERENCES insights(id)
);
```

Potential normalized-event table later:

```sql
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    event_type TEXT,
    assistant_id TEXT,
    conversation_id TEXT,
    call_control_id TEXT,
    caller TEXT,
    callee TEXT,
    summary TEXT,
    recommended_action TEXT,
    data TEXT NOT NULL
);
```

Do not add these until the UI needs persistent review state or normalized query performance.

## Security Requirements

1. `/admin/*` must require authentication except `/admin/login`.
2. Do not use query-string secrets for admin links.
3. Use `HttpOnly`, `SameSite=Lax`, and `Secure` cookie in production.
4. Avoid exposing `.env`, Docker secrets, API keys, or `WEBHOOK_SECRET` in rendered pages.
5. Keep Telnyx public webhook paths free from Authelia/OAuth, but consider putting `/admin/*` behind Authelia/Cloudflare Access later if Traefik path-specific middleware is easy.
6. Never add live external writes from the frontend in Phase 1.
7. For future live actions, require explicit confirmation screens and server-side allowlists.

## Implementation Tasks

### Task 1: Add Jinja2 dependency and static/template structure

**Objective:** Prepare the backend to render HTML pages.

**Files:**
- Modify: `Dockerfile`
- Create: `templates/admin_base.html`
- Create: `static/admin.css`
- Test: `tests/test_admin_ui.py`

**Steps:**
1. Add `jinja2` to the Dockerfile pip install line.
2. Import `Jinja2Templates` and `StaticFiles` in `app.py`.
3. Mount static files at `/static` or `/admin/static`.
4. Create a base template with title, nav, content block, and minimal CSS link.
5. Add a test that `GET /admin/login` returns HTML.
6. Run `python3 -m pytest -q`.

### Task 2: Add signed cookie helpers

**Objective:** Authenticate browser sessions using the existing shared secret.

**Files:**
- Modify: `app.py`
- Test: `tests/test_admin_ui.py`

**Steps:**
1. Add `hmac`, `hashlib`, and `secrets` imports.
2. Implement `sign_admin_session(value: str) -> str`.
3. Implement `verify_admin_session(cookie: str | None) -> bool`.
4. Implement `require_admin(request: Request)` helper.
5. Test valid cookie succeeds and missing/invalid cookie redirects or returns 401.

### Task 3: Login/logout pages

**Objective:** Let Andrew log in through the browser without manually setting headers.

**Files:**
- Modify: `app.py`
- Create: `templates/admin_login.html`
- Test: `tests/test_admin_ui.py`

**Steps:**
1. Add `GET /admin/login`.
2. Add `POST /admin/login` with form field `secret`.
3. On valid secret, set signed `admin_session` cookie and redirect to `/admin`.
4. On invalid secret, show error without echoing secret.
5. Add `POST /admin/logout`.
6. Test valid login sets cookie and invalid login does not.

### Task 4: Dashboard stats API

**Objective:** Provide safe summary data for the dashboard.

**Files:**
- Modify: `app.py`
- Test: `tests/test_admin_ui.py`

**Steps:**
1. Add `get_insight_stats()` helper using SQLite aggregation.
2. Return count, latest received time, signature verified count, shared secret count.
3. Add `GET /admin/api/stats` guarded by `require_admin`.
4. Test unauthenticated request fails and authenticated request returns expected JSON.

### Task 5: Dashboard page

**Objective:** Show high-level service status at `/admin`.

**Files:**
- Modify: `app.py`
- Create: `templates/admin_dashboard.html`
- Test: `tests/test_admin_ui.py`

**Steps:**
1. Add `GET /admin`.
2. Query stats helper.
3. Render cards and links to insights/tools.
4. Test authenticated page contains count and nav links.

### Task 6: Insight list API and page

**Objective:** Make stored Insight Group deliveries browseable.

**Files:**
- Modify: `app.py`
- Create: `templates/admin_insights.html`
- Modify: `static/admin.js`
- Test: `tests/test_admin_ui.py`

**Steps:**
1. Add helper `extract_insight_summary(record)`.
2. Add `GET /admin/api/insights?limit=50&offset=0&q=`.
3. Add `GET /admin/insights` page.
4. Render recent rows server-side first.
5. Add client-side search/filter later if simple.
6. Test list page includes inserted fixture record.

### Task 7: Insight detail page

**Objective:** Inspect one webhook payload without opening SQLite or curl output.

**Files:**
- Modify: `app.py`
- Create: `templates/admin_insight_detail.html`
- Modify: `static/admin.js`
- Test: `tests/test_admin_ui.py`

**Steps:**
1. Add `get_insight_by_id(id)` helper.
2. Add `GET /admin/api/insights/{id}`.
3. Add `GET /admin/insights/{id}`.
4. Pretty-print JSON with copy button.
5. Test known record returns 200 and unknown ID returns 404.

### Task 8: Assistant init tester

**Objective:** Let Andrew test Dynamic Variables Webhook behavior from the browser.

**Files:**
- Modify: `app.py`
- Create: `templates/admin_assistant_init.html`
- Test: `tests/test_admin_ui.py`

**Steps:**
1. Add `GET /admin/tools/assistant-init`.
2. Add `POST /admin/tools/assistant-init` that parses textarea JSON and calls `build_assistant_initialization_response()` directly.
3. Render response JSON.
4. Validate invalid JSON gives a useful error.
5. Test happy path and invalid JSON.

### Task 9: Webhook simulator

**Objective:** Let Andrew paste a sample Insight payload and store it through the same core ingestion path.

**Files:**
- Modify: `app.py`
- Create: `templates/admin_webhook_simulator.html`
- Test: `tests/test_admin_ui.py`

**Steps:**
1. Extract core insight persistence logic from `receive_telnyx_insights()` into `store_insight(payload, auth_metadata)`.
2. Make `POST /telnyx/insights` call `store_insight()`.
3. Add `GET/POST /admin/tools/webhook-simulator` guarded by admin session.
4. Test simulator creates a stored insight and links to detail page.

### Task 10: Docker and README updates

**Objective:** Make the frontend deployable and documented.

**Files:**
- Modify: `Dockerfile`
- Modify: `README.md`
- Modify: `docker-compose.yml` if any env needed

**Steps:**
1. Ensure `COPY templates static /app/` in Dockerfile.
2. Document `/admin` and login flow.
3. Document that the shared secret is used for initial personal admin auth.
4. Add caution about putting `/admin` behind extra access control before multi-user use.
5. Run local server and verify with browser/curl.

## Verification Checklist

Run before merging:

```bash
cd /home/hermes/telnyx-webhook-server
python3 -m py_compile app.py tests/test_app.py tests/test_admin_ui.py
python3 -m pytest -q
WEBHOOK_ALLOW_NO_SECRET=1 WEBHOOK_DB_PATH=/tmp/telnyx-webhook-ui.db uvicorn app:app --host 127.0.0.1 --port 8787
```

Manual smoke checks:

1. `GET http://127.0.0.1:8787/health` returns OK.
2. `GET http://127.0.0.1:8787/admin/login` renders login page.
3. Login with local shared secret.
4. `/admin` dashboard renders.
5. Post a sample insight.
6. `/admin/insights` shows it.
7. Detail page renders pretty JSON.
8. Assistant init tester returns `dynamic_variables`, `conversation.metadata`, and optional `memory.conversation_query`.

## Future Enhancements

- Add normalized `events` table once ingestion supports multiple event types again.
- Add review status and notes per insight.
- Add markdown export for call-review notes.
- Add Telnyx API read-only call lookup from dashboard using `TELNYX_API_KEY` server-side only.
- Add problem-only alerting views: unresolved, short calls, transfer failures.
- Add multi-user auth only when someone besides Andrew needs access.
- Add path-specific Traefik middleware for `/admin/*` if we want Authelia/Cloudflare Access on UI while keeping `/telnyx/*` open to signed Telnyx webhooks.
