# AI Assistant Performance Reporting + Lost Revenue Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build CallRail-like customer-facing performance reporting for AI voice assistants, with explicit lost-revenue / avoided-lost-revenue calculations that prove the assistant's business value.

**Architecture:** Extend the existing `~/telnyx-webhook-server` FastAPI admin/reporting app. Keep the current Telnyx AI Insights webhook storage as the source of truth, add a configurable revenue model layer, enrich per-call report rows with opportunity classification, and expose the results in the dashboard, preview/send email report, and JSON APIs. Start with heuristic + insight-derived revenue estimates; keep the design ready for later CRM/calendar/payment integrations.

**Tech Stack:** FastAPI, SQLite, Jinja templates, pytest, Telnyx Conversation Insights webhooks, existing SMTP report sender.

---

## Current context

The repository already contains:

- `app.py` monolith with:
  - SQLite initialization in `init_db()`.
  - Insight storage in `insights` table.
  - Review metadata in `insight_reviews`.
  - Parsed insight fields via `extract_insight_fields()`.
  - Dashboard stats via `get_insight_stats()`.
  - Assistant rollups via `list_assistant_rollups()`.
  - Email report generation via `build_email_report()`.
  - Email preview/send endpoints:
    - `POST /admin/api/email-report/preview`
    - `POST /admin/api/email-report/send`
  - Admin email UI at `templates/admin_email_report.html`.
- Tests in `tests/test_email_reports.py` covering assistant selection, preview, SMTP send, and duplicate-SMS flags.
- Existing report metrics include total conversations, lead-like conversations, callbacks, SMS tool calls, transfers, and duplicate SMS QA flags.

## Product goal

The customer should be able to see:

1. **What the assistant handled** — calls, leads, transfers, callbacks, appointments/messages.
2. **What revenue the assistant protected** — calls/leads captured that likely would have been missed.
3. **What revenue is still at risk** — missed calls, unresolved calls, failed transfers, no follow-up, low-confidence calls.
4. **Why the assistant is worth paying for** — estimated ROI vs assistant cost.

## Core revenue definitions

Use plain business terms in reports:

| Metric | Meaning | Suggested formula |
|---|---|---|
| Captured opportunity value | Estimated gross value of leads the assistant captured | `sum(lead_value * lead_probability)` for captured/qualified rows |
| Avoided lost revenue | Estimated value saved because the assistant answered/captured/qualified a call that might otherwise be missed | `captured_opportunity_value * missed_without_ai_probability` |
| Remaining lost revenue | Estimated value still at risk from unresolved/failed/abandoned/no-follow-up calls | `sum(lead_value * lead_probability)` for risk rows |
| Net value estimate | Assistant value after cost | `avoided_lost_revenue - assistant_cost_for_window` |
| ROI multiple | Simple proof metric | `avoided_lost_revenue / assistant_cost_for_window` |

Initial defaults should be conservative and configurable:

```python
DEFAULT_REVENUE_MODEL = {
    "default_job_value": 500.0,
    "default_lead_probability": 0.25,
    "missed_without_ai_probability": 0.60,
    "monthly_assistant_cost": 95.0,
    "category_values": {},
    "category_probabilities": {},
}
```

---

## Phase 1 — Revenue model foundation

### Task 1: Add revenue-model config loader

**Objective:** Load customer/assistant revenue assumptions from env JSON or optional file without changing DB schema yet.

**Files:**
- Modify: `app.py`
- Test: `tests/test_email_reports.py`

**Implementation notes:**

Add constants near the other env/config constants:

```python
REVENUE_MODEL_JSON = os.environ.get("REVENUE_MODEL", "").strip()
REVENUE_MODEL_PATH = Path(os.environ.get("REVENUE_MODEL_PATH", "/data/revenue-model.json"))
```

Add helpers:

```python
DEFAULT_REVENUE_MODEL = {
    "default_job_value": 500.0,
    "default_lead_probability": 0.25,
    "missed_without_ai_probability": 0.60,
    "monthly_assistant_cost": 95.0,
    "category_values": {},
    "category_probabilities": {},
    "assistant_overrides": {},
}


def _safe_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= 0 else fallback


def load_revenue_model() -> dict[str, Any]:
    model = dict(DEFAULT_REVENUE_MODEL)
    parsed = None
    if REVENUE_MODEL_JSON:
        try:
            parsed = json.loads(REVENUE_MODEL_JSON)
        except json.JSONDecodeError:
            parsed = None
    elif REVENUE_MODEL_PATH.exists():
        try:
            parsed = json.loads(REVENUE_MODEL_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            parsed = None
    if isinstance(parsed, dict):
        model.update(parsed)
    model["default_job_value"] = _safe_float(model.get("default_job_value"), DEFAULT_REVENUE_MODEL["default_job_value"])
    model["default_lead_probability"] = min(_safe_float(model.get("default_lead_probability"), DEFAULT_REVENUE_MODEL["default_lead_probability"]), 1.0)
    model["missed_without_ai_probability"] = min(_safe_float(model.get("missed_without_ai_probability"), DEFAULT_REVENUE_MODEL["missed_without_ai_probability"]), 1.0)
    model["monthly_assistant_cost"] = _safe_float(model.get("monthly_assistant_cost"), DEFAULT_REVENUE_MODEL["monthly_assistant_cost"])
    if not isinstance(model.get("category_values"), dict):
        model["category_values"] = {}
    if not isinstance(model.get("category_probabilities"), dict):
        model["category_probabilities"] = {}
    if not isinstance(model.get("assistant_overrides"), dict):
        model["assistant_overrides"] = {}
    return model
```

**Test:** Add `test_revenue_model_env_overrides_defaults`.

Expected assertions:

- Env JSON can override default job value, probability, missed-without-AI probability, monthly cost.
- Invalid numbers fall back safely.
- Probability values cap at `1.0`.

**Run:**

```bash
cd /home/hermes/telnyx-webhook-server
pytest tests/test_email_reports.py::test_revenue_model_env_overrides_defaults -v
```

---

### Task 2: Add money formatting and window-cost helpers

**Objective:** Make report math readable and consistent.

**Files:**
- Modify: `app.py`
- Test: `tests/test_email_reports.py`

Add helpers:

```python

def money(value: float) -> str:
    return "${:,.0f}".format(float(value or 0))


def assistant_cost_for_window(model: dict[str, Any], hours: int, assistant_count: int = 1) -> float:
    monthly = _safe_float(model.get("monthly_assistant_cost"), DEFAULT_REVENUE_MODEL["monthly_assistant_cost"])
    # 730 average hours/month.
    return monthly * max(1, assistant_count) * (max(1, hours) / 730.0)
```

**Test:** Add `test_money_and_assistant_cost_helpers`.

Expected:

- `money(1250.4) == "$1,250"`
- 730-hour window at `$95` and 1 assistant equals about `95.0`.
- 24-hour window cost is proportional and non-zero.

---

## Phase 2 — Classify captured value and lost-revenue risk per conversation

### Task 3: Add opportunity classification helper

**Objective:** Turn parsed insight fields into business outcome flags.

**Files:**
- Modify: `app.py`
- Test: `tests/test_email_reports.py`

Create `classify_opportunity(fields, summary)` returning:

```python
{
    "is_lead": bool,
    "is_captured": bool,
    "is_at_risk": bool,
    "risk_reasons": list[str],
    "outcome_label": str,
}
```

Initial rules:

- `is_lead` if existing lead-like heuristic is true OR category/summary includes terms:
  - `lead`, `quote`, `estimate`, `booking`, `appointment`, `reservation`, `event`, `catering`, `private`, `new customer`, `sales`, `pricing`.
- `is_captured` if lead and one of:
  - `resolution_key == "resolved"`
  - callback/follow-up info was captured
  - transfer tool was called
  - appointment/booking/reservation appears in summary.
- `is_at_risk` if lead and one of:
  - `resolution_key == "unresolved"`
  - negative sentiment
  - summary includes `missed`, `hung up`, `no answer`, `failed transfer`, `could not`, `didn't answer`, `needs callback`, `follow up`.
- `risk_reasons` should be customer-readable, e.g. `unresolved call`, `negative sentiment`, `callback needed`, `transfer/no-answer risk`.

**Test:** Add `test_classify_opportunity_flags_captured_and_at_risk_leads`.

Seed sample field dicts and assert:

- Resolved appointment request is captured, not at risk.
- Unresolved quote request is at risk.
- Negative sentiment lead is at risk.
- General FAQ is not a lead.

---

### Task 4: Add per-row revenue estimate helper

**Objective:** Calculate value estimates for each report row using category and assistant overrides.

**Files:**
- Modify: `app.py`
- Test: `tests/test_email_reports.py`

Create:

```python

def estimate_row_revenue(fields: dict[str, Any], opportunity: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    category = str(fields.get("primary_category") or "").strip()
    assistant_id = str(fields.get("assistant_id") or "")
    assistant_overrides = model.get("assistant_overrides") or {}
    active_model = {**model, **(assistant_overrides.get(assistant_id) if isinstance(assistant_overrides.get(assistant_id), dict) else {})}
    value = _safe_float((active_model.get("category_values") or {}).get(category), _safe_float(active_model.get("default_job_value"), 500.0))
    probability = min(_safe_float((active_model.get("category_probabilities") or {}).get(category), _safe_float(active_model.get("default_lead_probability"), 0.25)), 1.0)
    opportunity_value = value * probability if opportunity["is_lead"] else 0.0
    avoided_lost_revenue = opportunity_value * _safe_float(active_model.get("missed_without_ai_probability"), 0.60) if opportunity["is_captured"] else 0.0
    remaining_lost_revenue = opportunity_value if opportunity["is_at_risk"] else 0.0
    return {
        "job_value": value,
        "lead_probability": probability,
        "opportunity_value": opportunity_value,
        "avoided_lost_revenue": avoided_lost_revenue,
        "remaining_lost_revenue": remaining_lost_revenue,
        "revenue_model_category": category or "default",
    }
```

**Test:** Add `test_estimate_row_revenue_uses_category_overrides`.

Expected:

- Category override changes value/probability.
- Captured row contributes avoided lost revenue.
- At-risk unresolved row contributes remaining lost revenue.
- Non-lead contributes zero.

---

## Phase 3 — Upgrade email report with proof-of-value section

### Task 5: Enrich `build_email_report()` rows with opportunity and revenue fields

**Objective:** Make the existing email report compute lost revenue and ROI totals.

**Files:**
- Modify: `app.py:1797-1877`
- Test: `tests/test_email_reports.py`

Inside `build_email_report()`:

1. Load revenue model once:

```python
revenue_model = load_revenue_model()
```

2. After existing counts/summary/lead-like detection, call:

```python
opportunity = classify_opportunity(fields, summary)
revenue = estimate_row_revenue(fields, opportunity, revenue_model)
rows.append({**fields, **counts, **opportunity, **revenue, ...})
```

3. Compute totals:

```python
captured_value = sum(item["opportunity_value"] for item in rows if item["is_captured"])
avoided_lost_revenue = sum(item["avoided_lost_revenue"] for item in rows)
remaining_lost_revenue = sum(item["remaining_lost_revenue"] for item in rows)
assistant_cost = assistant_cost_for_window(revenue_model, hours, assistant_count=len(selected))
net_value = avoided_lost_revenue - assistant_cost
roi_multiple = avoided_lost_revenue / assistant_cost if assistant_cost > 0 else None
at_risk_count = sum(1 for item in rows if item["is_at_risk"])
captured_lead_count = sum(1 for item in rows if item["is_captured"])
```

4. Add these fields to returned JSON:

```python
"captured_opportunity_value": captured_value,
"avoided_lost_revenue": avoided_lost_revenue,
"remaining_lost_revenue": remaining_lost_revenue,
"assistant_cost_estimate": assistant_cost,
"net_value_estimate": net_value,
"roi_multiple": roi_multiple,
"at_risk_count": at_risk_count,
"captured_lead_count": captured_lead_count,
```

**Test:** Extend `test_email_report_preview_filters_multiple_assistants_and_flags_sms` or add a new test.

Expected:

- Response JSON contains `avoided_lost_revenue`, `remaining_lost_revenue`, `net_value_estimate`, `roi_multiple`.
- Values are numeric.
- Markdown contains `Estimated assistant value`, `Avoided lost revenue`, and `Remaining lost revenue at risk`.

---

### Task 6: Add an “Estimated assistant value” section to markdown report

**Objective:** Make the customer-facing report sell the assistant’s worth clearly.

**Files:**
- Modify: `app.py:1826-1877`
- Test: `tests/test_email_reports.py`

Insert after report header and before the generic metric table:

```markdown
## Estimated assistant value

| Metric | Value |
|---|---:|
| Captured lead/opportunity value | $X |
| Avoided lost revenue | $Y |
| Remaining lost revenue at risk | $Z |
| Estimated assistant cost for this window | $C |
| Net value estimate | $N |
| ROI multiple | 12.3x |
```

Add a short plain-English explanation:

```markdown
These are estimates based on the configured average job value, lead probability, and missed-without-AI probability. They are directional proof-of-value numbers, not booked revenue.
```

Add a section:

```markdown
## Highest-risk lost revenue opportunities
```

Table columns:

- Time
- Assistant
- Caller
- Est. value at risk
- Risk reason
- Summary

Limit to top 25, sorted by `remaining_lost_revenue DESC`.

**Test:** Assert markdown includes:

- `## Estimated assistant value`
- `Avoided lost revenue`
- `ROI multiple`
- `## Highest-risk lost revenue opportunities`

---

### Task 7: Improve subject line with value proof

**Objective:** Make emailed reports actionable from the inbox.

**Files:**
- Modify: `app.py:1824`
- Test: `tests/test_email_reports.py`

Change subject to include avoided lost revenue:

```python
subject = f"AI Insights Report — {total} calls, {lead_count} leads, {money(avoided_lost_revenue)} protected"
```

If there are at-risk leads:

```python
subject += f", {money(remaining_lost_revenue)} at risk"
```

**Test:** Assert subject starts with `AI Insights Report` and includes `$`.

---

## Phase 4 — Add dashboard-level proof metrics

### Task 8: Add revenue fields to `get_insight_stats()`

**Objective:** Show value metrics on the admin dashboard, not only emails.

**Files:**
- Modify: `app.py:1615-1703`
- Test: create/extend `tests/test_admin_ui.py` or `tests/test_email_reports.py`

Inside `get_insight_stats()`:

- Load revenue model.
- For each record, run same classification/revenue helper.
- Aggregate:
  - `captured_lead_count`
  - `at_risk_count`
  - `captured_opportunity_value`
  - `avoided_lost_revenue`
  - `remaining_lost_revenue`
- Add formatted fields:
  - `avoided_lost_revenue_display`
  - `remaining_lost_revenue_display`
  - `captured_opportunity_value_display`

**Test:** Seed insights and assert `/admin/api/stats` contains these fields.

---

### Task 9: Add dashboard KPI cards

**Objective:** Make the dashboard communicate value immediately.

**Files:**
- Modify: `templates/admin_dashboard.html`
- Test: `tests/test_admin_ui.py`

Add KPI cards near the top:

- `Protected revenue` → `stats.avoided_lost_revenue_display`
- `Revenue still at risk` → `stats.remaining_lost_revenue_display`
- `Captured leads` → `stats.captured_lead_count`

Suggested copy:

```html
<div class="kpi-card accent">
  <div class="kpi-label">Protected revenue</div>
  <div class="kpi-value">{{ stats.avoided_lost_revenue_display }}</div>
  <div class="kpi-foot">estimated avoided lost revenue</div>
</div>
<a class="kpi-card danger" href="/admin/insights?q=at+risk" style="text-decoration:none; color:inherit;">
  <div class="kpi-label">Revenue still at risk</div>
  <div class="kpi-value">{{ stats.remaining_lost_revenue_display }}</div>
  <div class="kpi-foot">needs follow-up →</div>
</a>
```

**Test:** Login and GET `/admin`; assert `Protected revenue` and `Revenue still at risk` are present.

---

## Phase 5 — Customer-configurable assumptions

### Task 10: Add report settings inputs for revenue assumptions

**Objective:** Let an operator preview reports using customer-specific assumptions without redeploying.

**Files:**
- Modify: `templates/admin_email_report.html`
- Modify: admin JS if report builder JS lives in `admin_base.html` or static script area
- Modify: `app.py` preview/send endpoint parsing
- Test: `tests/test_email_reports.py`

Add fields to report settings:

- Average job value
- Lead close probability
- Missed-without-AI probability
- Monthly assistant cost

Pass them in JSON payload as:

```json
{
  "revenue_model": {
    "default_job_value": 750,
    "default_lead_probability": 0.30,
    "missed_without_ai_probability": 0.60,
    "monthly_assistant_cost": 95
  }
}
```

Update `build_email_report()` signature:

```python
def build_email_report(..., revenue_model_override: dict[str, Any] | None = None)
```

Merge override on top of `load_revenue_model()` after validation.

**Test:** `test_email_report_preview_accepts_revenue_model_override`:

- Same seeded insight.
- Preview once with default value and once with higher value.
- Assert higher override increases `avoided_lost_revenue`.

---

### Task 11: Add optional per-assistant/customer config file

**Objective:** Support durable customer-specific defaults in production.

**Files:**
- Modify: `docker-compose.yml` if present
- Modify: README/deployment docs if present
- Optional create: `/data/revenue-model.json` example in docs, not committed if environment-specific

Config example:

```json
{
  "default_job_value": 500,
  "default_lead_probability": 0.25,
  "missed_without_ai_probability": 0.60,
  "monthly_assistant_cost": 95,
  "category_values": {
    "Catering Lead": 1500,
    "Event Inquiry": 1200,
    "Reservation": 300
  },
  "category_probabilities": {
    "Catering Lead": 0.35,
    "Event Inquiry": 0.30,
    "Reservation": 0.50
  },
  "assistant_overrides": {
    "assistant-example": {
      "default_job_value": 900,
      "monthly_assistant_cost": 150
    }
  }
}
```

**Validation:** Run full tests and confirm app boots with missing config file.

---

## Phase 6 — Lost-revenue evidence and follow-up workflows

### Task 12: Add review labels for revenue-risk rows

**Objective:** Make at-risk revenue actionable, not just reported.

**Files:**
- Modify: `app.py` and/or report generation only
- Optional Modify: `templates/admin_insights.html`
- Test: `tests/test_reviews.py` or report tests

Initial no-schema approach:

- Include `risk_reasons` in report rows.
- For query/search, ensure `list_insight_summaries()` includes `is_at_risk`, `remaining_lost_revenue`, and `risk_reasons` so `/admin/api/insights` can expose it.

Later optional schema:

- Add a `revenue_reviews` or extend `insight_reviews` labels with `lost-revenue-risk`, `followed-up`, `won`, `lost`.

**Test:** API insights response includes `risk_reasons` for at-risk seeded call.

---

### Task 13: Add “won/lost after follow-up” manual feedback loop

**Objective:** Improve proof quality over time by letting humans mark actual outcomes.

**Files:**
- Modify: `insight_reviews` usage or create new table
- Modify: `templates/admin_insight_detail.html`
- Test: review tests

Minimal implementation:

- Add labels to existing review UI: `won`, `lost`, `followed-up`, `bad-lead`, `booked`.
- Report actual outcome counts separately from estimates:
  - `Booked/won leads marked by staff`
  - `Estimated value from won labels`

Do this after Phase 1-5; do not block initial value reporting.

---

## Phase 7 — CallRail-like customer portal roadmap

### Task 14: Add customer-facing report API shape

**Objective:** Prepare for a white-labeled customer portal while reusing admin calculations.

**Files:**
- Modify: `app.py`
- Test: new API test

Add endpoint:

```text
POST /admin/api/performance-report/preview
```

Return structured JSON:

```json
{
  "window": {"hours": 168},
  "summary": {
    "total_conversations": 42,
    "captured_leads": 12,
    "at_risk_leads": 3,
    "avoided_lost_revenue": 4500,
    "remaining_lost_revenue": 900,
    "roi_multiple": 18.4
  },
  "top_intents": [],
  "risk_rows": [],
  "lead_rows": [],
  "tool_qa": []
}
```

Do not build public auth/customer tenancy until the internal API shape is stable.

---

## Validation checklist

Run after implementation:

```bash
cd /home/hermes/telnyx-webhook-server
pytest -q
```

Manual smoke test:

1. Start locally with temp DB and test secret.
2. Seed at least three insights:
   - captured/resolved lead
   - unresolved quote/event lead
   - general FAQ/non-lead
3. Login to admin.
4. Open `/admin` and confirm protected/at-risk revenue cards.
5. Open `/admin/reports/email`.
6. Preview report for selected assistant.
7. Confirm markdown includes:
   - estimated assistant value
   - avoided lost revenue
   - remaining lost revenue at risk
   - highest-risk lost revenue opportunities
   - existing callback and SMS/tool QA sections
8. Send test email with monkeypatched SMTP in tests; do not send production email during unit tests.

## Risks and tradeoffs

- **Estimates can look fake if overconfident.** Always label values as estimates and expose assumptions.
- **Bad defaults can mislead customers.** Start conservative; let each customer configure job value and close probability.
- **Insight quality determines report quality.** Add/maintain Telnyx insight fields for call category, resolution status, caller intent, summary, and outcome.
- **Lost revenue is not booked revenue.** Separate estimated opportunity from actual won/lost feedback.
- **CallRail-style attribution is a later phase.** Do not block AI assistant proof-of-value on Google Ads/Meta/website source integrations.

## Definition of done

- Email preview/send reports include lost-revenue and assistant-value sections.
- Dashboard shows protected revenue and revenue-at-risk KPIs.
- Revenue assumptions are configurable by env/file and preview override.
- Tests cover revenue model loading, row estimation, report JSON fields, markdown output, and dashboard rendering.
- Full `pytest -q` passes.
- Customer-facing wording clearly says estimates are directional proof-of-value numbers, not guaranteed booked revenue.
