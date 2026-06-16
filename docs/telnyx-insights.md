# Telnyx Insight Group webhooks

This document describes the webhook payloads Telnyx sends to this server when an **Insight Group** finishes analyzing a conversation, how we store them, and how to read the results.

## When Telnyx sends a webhook

Telnyx posts to your Insight Group webhook URL when insight analysis completes for a conversation (phone call or SMS chat):

```text
POST https://webhook.<your-domain>/telnyx/insights
```

Typical trigger: a conversation ends (or reaches the point configured in Mission Control), and every insight in the group has finished running.

The server must respond quickly with `200` and:

```json
{"accepted": true, "id": "<server-generated-id>"}
```

If Telnyx cannot verify the request or gets a non-2xx response, it may retry delivery.

## Request authentication

Production deliveries are signed by Telnyx when `TELNYX_PUBLIC_KEY` is configured.

| Header | Purpose |
|--------|---------|
| `telnyx-signature-ed25519` | Base64 Ed25519 signature over `{timestamp}\|{raw_body}` |
| `telnyx-timestamp` | Unix timestamp used in the signed payload |

For manual testing, you can use the shared secret instead:

```bash
curl -s "https://webhook.miswitch.cloud/telnyx/insights?secret=<secret>" \
  -H 'content-type: application/json' \
  -d @sample-payload.json
```

## Two layers: Telnyx body vs stored record

Telnyx sends a JSON **event body**. This server wraps it in a **stored record** before writing to SQLite.

### What Telnyx sends (HTTP body)

```json
{
  "event_type": "conversation_insight_result",
  "record_type": "event",
  "payload": {
    "conversation_id": "b922b88a-401e-4ae0-92cc-1b91ee74f867",
    "insight_group_id": "e58ece8c-f50b-47ed-86d9-8ec6483439c1",
    "insight_id": null,
    "insights_instructions": null,
    "request_id": "b922b88a-401e-4ae0-92cc-1b91ee74f867",
    "status": "completed",
    "user_id": "e543faba-8ea2-49ab-adc8-b690e53fc659",
    "metadata": { "...": "..." },
    "results": [ "..."]
  }
}
```

### What this server stores (SQLite row)

Each delivery is saved as one JSON object with server metadata plus the original Telnyx body under `payload`:

```json
{
  "id": "f02fa1d3aa944dafbb4896baf0b88cbf",
  "received_at": "2026-06-09T16:56:18.820619+00:00",
  "path": "/telnyx/insights",
  "telnyx_signature_present": true,
  "telnyx_signature_verified": true,
  "shared_secret_verified": false,
  "telnyx_timestamp": "1781024177",
  "payload": { "... Telnyx event body ..." }
}
```

| Field | Source | Meaning |
|-------|--------|---------|
| `id` | This server | Unique ID for this received webhook |
| `received_at` | This server | UTC ISO timestamp when we accepted the POST |
| `path` | This server | Always `/telnyx/insights` today |
| `telnyx_signature_*` | This server | Whether Telnyx signing headers were present and valid |
| `shared_secret_verified` | This server | Whether the request used `?secret=` or `x-webhook-secret` |
| `payload` | Telnyx | Unmodified event JSON from the POST body |

## Telnyx payload structure

All observed production events use:

```text
event_type = conversation_insight_result
record_type  = event
payload.status = completed
```

### Top-level event fields

| Field | Type | Description |
|-------|------|-------------|
| `event_type` | string | Always `conversation_insight_result` for Insight Groups |
| `record_type` | string | Always `event` |
| `payload.conversation_id` | UUID | Telnyx conversation identifier |
| `payload.insight_group_id` | UUID | Insight Group that produced this webhook |
| `payload.insight_id` | null \| UUID | Usually `null` when the whole group completes at once |
| `payload.request_id` | UUID | Correlates with the insight run request |
| `payload.status` | string | `completed` when results are ready |
| `payload.user_id` | UUID | Telnyx account/user scope |
| `payload.metadata` | object | Channel, assistant, and call context |
| `payload.results` | array | One entry per insight in the group |

### `metadata` by channel

`metadata.telnyx_conversation_channel` tells you whether the conversation was a voice call or SMS thread.

**Phone call** — includes call control and tool usage:

```json
{
  "assistant_id": "assistant-40db34c1-9f0c-4a98-ae25-b6981d5b3e3f",
  "assistant_version_id": "20260609T133047378262",
  "telnyx_conversation_channel": "phone_call",
  "telnyx_agent_target": "+12489295958",
  "telnyx_end_user_target": "+12489201080",
  "telnyx_end_user_target_verified": true,
  "call_control_id": "v3:9qUjEIuFGKb4cj4ojSIK39DNV-ykeixv43ERYcZYj1zJjeilqieYLw",
  "call_leg_id": "9aa2d05a-6427-11f1-90f1-02420aef98a1",
  "call_session_id": "9aa2c66e-6427-11f1-be2d-02420aef98a1",
  "from": "+12489201080",
  "to": "+12489295958",
  "called_tools": ["send_sms", "transfer"],
  "telnyx_shaken_stir_attestation": "a"
}
```

**SMS chat** — lighter metadata, no call IDs:

```json
{
  "assistant_id": "assistant-3c2dea60-17c9-4a08-87be-d3d84a2f734e",
  "assistant_version_id": "20260529T182100983477",
  "telnyx_conversation_channel": "sms_chat",
  "telnyx_agent_target": "+12486093031",
  "telnyx_end_user_target": "+12488237676"
}
```

Common metadata keys:

| Key | Channels | Description |
|-----|----------|-------------|
| `assistant_id` | both | AI Assistant that handled the conversation |
| `assistant_version_id` | both | Published assistant version at conversation time |
| `telnyx_agent_target` | both | Your Telnyx number / agent endpoint |
| `telnyx_end_user_target` | both | Caller or texter number |
| `call_control_id` | phone | Telnyx Call Control ID for the leg |
| `call_leg_id`, `call_session_id` | phone | Internal call identifiers |
| `from`, `to` | phone | E.164 numbers for the call direction |
| `called_tools` | phone | Assistant tools invoked during the call |
| `telnyx_end_user_target_verified` | phone | STIR/SHAKEN or verification flag when present |
| `telnyx_shaken_stir_attestation` | phone | Attestation level (`a`, `b`, `c`) when present |

## Insight Group: MySwitch

This is the Insight Group currently wired to this webhook server. Every production payload in our database with `insight_group_id` **`e58ece8c-f50b-47ed-86d9-8ec6483439c1`** comes from this group.

| Property | Value |
|----------|-------|
| **Name** | MySwitch |
| **ID** | `e58ece8c-f50b-47ed-86d9-8ec6483439c1` |
| **Webhook URL** | `https://webhook.miswitch.cloud/telnyx/insights` |
| **Insights count** | 5 |
| **Created** | 22 Oct 2025 12:34 PM |

When a conversation finishes, Telnyx runs all five insights and POSTs one webhook. The `payload.results` array always contains **five entries** — one per insight below. Use the `insight_id` column to look up which result is which.

| # | Insight name | `insight_id` | Type | Typical `result` format |
|---|--------------|--------------|------|-------------------------|
| 1 | Caller Identity | `78ae8f13-50fb-4bb0-afea-be087458d493` | Custom | JSON string |
| 2 | Sentiment Confidence V2 | `73145dab-78a8-4ef8-bc8d-3ec132089f8b` | Custom | JSON string (schema below) |
| 3 | Customer Intent / Call Category | `b5182c7c-1ec3-46ed-bb6e-e43c33d2fbb0` | Custom | JSON string or markdown |
| 4 | Call Resolution Status | `e0398bdc-55c1-4a32-a430-1bd3b625afb2` | Custom | JSON string |
| 5 | Summary | `cfcc865c-d3d4-4823-8a4b-f0df57d9f56f` | Managed by Telnyx | Plain-text paragraph |

### 1. Caller Identity

**Type:** Custom

**Instructions:** Extract the caller's name as they stated it during the conversation. If the caller provided their name at any point, return it. If no name was given, return empty strings.

**Expected output** (JSON string — parse with `json.loads`):

```json
{
  "caller_first_name": "Jessica",
  "caller_last_name": "Miller"
}
```

When no name was collected:

```json
{
  "caller_first_name": "",
  "caller_last_name": ""
}
```

### 2. Sentiment Confidence V2

**Type:** Custom

**Instructions:** Analyze the **caller's** speech or message (not the assistant's responses). Produce structured sentiment and intent:

1. **Sentiment** — label (`Positive`, `Neutral`, `Negative`, `Mixed`), score (`-1.0` to `+1.0`), confidence (`0.0` to `1.0`)
2. **Intent** — most likely intent name (e.g. `complaint`, `request_info`, `cancellation`, `greeting`) with confidence (`0.0` to `1.0`)
3. **Explanation** — 1–3 sentences referencing tone, words, and phrasing
4. **Key factors** — 2–5 words/phrases that most influenced the result

**JSON schema** (required top-level keys: `sentiment`, `intent`, `explanation`, `key_factors`):

```json
{
  "sentiment": {
    "label": "Neutral",
    "score": 0.0,
    "confidence": 0.8
  },
  "intent": {
    "name": "transfer_request",
    "confidence": 0.9
  },
  "explanation": "The user requested a transfer to the event coordinator.",
  "key_factors": ["event coordinator", "transfer"]
}
```

Observed in production: `label` is usually `Neutral`; `name` varies (`transfer_request`, `request_info`, `event_inquiry`, etc.).

### 3. Customer Intent / Call Category

**Type:** Custom

**Instructions:** Identify the primary reason for the customer's call. Categorize into: Account/Billing Question, Technical Support, Product Information, Service Issue/Complaint, Order/Purchase Inquiry, General Information, Other. Provide a brief description of the specific issue or question.

**Expected output** — usually a JSON string:

```json
{
  "primary_category": "Transfer",
  "issue_description": "The customer requested a transfer to the event coordinator"
}
```

Some deliveries return **markdown** instead (same insight, different formatting pass):

```markdown
**Primary Reason for Call:** Other
**Specific Issue/Question:** Caller was transferred and needs to speak with an event coordinator.
**Action:** Transfer to Event Coordinator.
```

Try JSON parse first; if it fails, treat as markdown.

### 4. Call Resolution Status

**Type:** Custom

**Instructions:** Determine if the customer's issue was resolved during this call:

| Status | Meaning |
|--------|---------|
| **Resolved** | Issue fully addressed by the agent |
| **Transferred** | Escalated to another team/specialist |
| **Partially Resolved** | Some help provided but transfer still needed |
| **Unresolved** | Customer ended call without resolution |

Include the primary reason if transferred or unresolved.

**Expected output** (JSON string):

```json
{
  "transfer_reason": "requested transfer to event coordinator",
  "resolution_status": "Transferred",
  "resolved_by_agent": false
}
```

`resolution_status` maps to the statuses above. When transferred, `transfer_reason` explains why.

### 5. Summary

**Type:** Managed by Telnyx (read-only instructions)

**Instructions:** Summarize the conversation for use as future context. Include key facts, decisions, preferences, or goals. Avoid unnecessary details or pleasantries. Format as a short paragraph (3–5 sentences max).

**Expected output** — plain-text string (not JSON):

```text
A user contacted Legacy Events and requested to be transferred to the event coordinator.
The transfer completed and a confirmation SMS was sent. The caller's name was not collected.
```

### Extracting MySwitch results from a stored record

Given a stored record at `.insights[0]` from `GET /telnyx/insights`:

```bash
# List insight_id → first 80 chars of each result
curl -s "https://webhook.miswitch.cloud/telnyx/insights?secret=$SECRET" \
  | jq '.insights[-1].payload.payload.results[] | {insight_id, preview: .result[0:80]}'
```

Lookup helper (conceptual — map IDs to names):

```python
MYSWITCH_INSIGHTS = {
    "78ae8f13-50fb-4bb0-afea-be087458d493": "caller_identity",
    "73145dab-78a8-4ef8-bc8d-3ec132089f8b": "sentiment_v2",
    "b5182c7c-1ec3-46ed-bb6e-e43c33d2fbb0": "call_category",
    "e0398bdc-55c1-4a32-a430-1bd3b625afb2": "resolution_status",
    "cfcc865c-d3d4-4823-8a4b-f0df57d9f56f": "summary",
}

def parse_results(results: list[dict]) -> dict:
    parsed = {}
    for item in results:
        name = MYSWITCH_INSIGHTS.get(item["insight_id"], item["insight_id"])
        raw = item["result"]
        try:
            parsed[name] = json.loads(raw)
        except json.JSONDecodeError:
            parsed[name] = raw
    return parsed
```

## The `results` array

Each item is one insight definition from your Insight Group:

```json
{
  "insight_id": "73145dab-78a8-4ef8-bc8d-3ec132089f8b",
  "result": "..."
}
```

| Field | Description |
|-------|-------------|
| `insight_id` | Stable UUID for the insight prompt/template in Telnyx Mission Control |
| `result` | Model output — format depends on how that insight was configured |

For the **MySwitch** group, see the [Insight Group: MySwitch](#insight-group-myswitch) section above for the full `insight_id` → name map and expected output shapes.

### Result format variations

Insight outputs are **strings**. The model may return:

1. **JSON string** — parse with `json.loads(result)` after receiving the webhook
2. **Markdown text** — headings, bullet lists, bold labels
3. **Plain prose** — narrative paragraph(s)

Example JSON-string result (**MySwitch → Sentiment Confidence V2**):

```json
{
  "insight_id": "73145dab-78a8-4ef8-bc8d-3ec132089f8b",
  "result": "{\"intent\": {\"name\": \"transfer_request\", \"confidence\": 0.9}, \"sentiment\": {\"label\": \"Neutral\", \"score\": 0.0, \"confidence\": 0.8}, \"explanation\": \"The user requested a transfer to the event coordinator.\", \"key_factors\": [\"transferred over\", \"event coordinator\"]}"
}
```

Parsed:

```json
{
  "intent": {"name": "transfer_request", "confidence": 0.9},
  "sentiment": {"label": "Neutral", "score": 0.0, "confidence": 0.8},
  "explanation": "The user requested a transfer to the event coordinator.",
  "key_factors": ["transferred over", "event coordinator"]
}
```

Example structured JSON result (**MySwitch → Caller Identity**):

```json
{
  "insight_id": "78ae8f13-50fb-4bb0-afea-be087458d493",
  "result": "{\"caller_last_name\": \"\", \"caller_first_name\": \"\"}"
}
```

Example markdown result (**MySwitch → Customer Intent / Call Category**, alternate format):

```json
{
  "insight_id": "b5182c7c-1ec3-46ed-bb6e-e43c33d2fbb0",
  "result": "**Primary Reason for Call:** Transfer  \n**Specific Issue/Question:** Caller requested the event coordinator.  \n\n**Action:** Transfer to Event Coordinator."
}
```

Example plain-text result (**MySwitch → Summary**):

```json
{
  "insight_id": "cfcc865c-d3d4-4823-8a4b-f0df57d9f56f",
  "result": "A user contacted Legacy Events and requested a transfer to the event coordinator. The call was transferred and a confirmation SMS was sent. The caller's name was not collected."
}
```

## Full examples

### Example A — MySwitch phone call (completed transfer)

Telnyx POST body (abbreviated; all five MySwitch insights included):

```json
{
  "event_type": "conversation_insight_result",
  "record_type": "event",
  "payload": {
    "conversation_id": "e2d1d9c9-8418-4508-93ee-4ec419eaced3",
    "insight_group_id": "e58ece8c-f50b-47ed-86d9-8ec6483439c1",
    "insight_id": null,
    "insights_instructions": null,
    "request_id": "9f29524b-6335-416c-889f-2c65626772c6",
    "status": "completed",
    "user_id": "e543faba-8ea2-49ab-adc8-b690e53fc659",
    "metadata": {
      "assistant_id": "assistant-40db34c1-9f0c-4a98-ae25-b6981d5b3e3f",
      "assistant_version_id": "20260609T133047378262",
      "telnyx_conversation_channel": "phone_call",
      "telnyx_agent_target": "+12489295958",
      "telnyx_end_user_target": "+12489201080",
      "call_control_id": "v3:9qUjEIuFGKb4cj4ojSIK39DNV-ykeixv43ERYcZYj1zJjeilqieYLw",
      "called_tools": ["send_sms", "transfer"],
      "from": "+12489201080",
      "to": "+12489295958"
    },
    "results": [
      {
        "insight_id": "78ae8f13-50fb-4bb0-afea-be087458d493",
        "result": "{\"caller_last_name\": \"\", \"caller_first_name\": \"\"}"
      },
      {
        "insight_id": "73145dab-78a8-4ef8-bc8d-3ec132089f8b",
        "result": "{\"intent\": {\"name\": \"transfer_request\", \"confidence\": 0.9}, \"sentiment\": {\"label\": \"Neutral\", \"score\": 0.0, \"confidence\": 0.8}, \"explanation\": \"The user requested a transfer to the event coordinator.\", \"key_factors\": [\"event coordinator\", \"transfer\"]}"
      },
      {
        "insight_id": "b5182c7c-1ec3-46ed-bb6e-e43c33d2fbb0",
        "result": "{\"primary_category\": \"Transfer\", \"issue_description\": \"The customer requested a transfer to the event coordinator\"}"
      },
      {
        "insight_id": "e0398bdc-55c1-4a32-a430-1bd3b625afb2",
        "result": "{\"transfer_reason\": \"requested transfer to event coordinator\", \"resolution_status\": \"Transferred\", \"resolved_by_agent\": false}"
      },
      {
        "insight_id": "cfcc865c-d3d4-4823-8a4b-f0df57d9f56f",
        "result": "A user contacted Legacy Events and requested to be transferred to the event coordinator. The transfer completed and a confirmation SMS was sent."
      }
    ]
  }
}
```

### Example B — MySwitch SMS chat

Same Insight Group (`e58ece8c-f50b-47ed-86d9-8ec6483439c1`), but `metadata` shows `sms_chat` and omits call fields. All five insights still run; truncated to Summary only here:

```json
{
  "event_type": "conversation_insight_result",
  "record_type": "event",
  "payload": {
    "conversation_id": "b922b88a-401e-4ae0-92cc-1b91ee74f867",
    "insight_group_id": "e58ece8c-f50b-47ed-86d9-8ec6483439c1",
    "status": "completed",
    "metadata": {
      "assistant_id": "assistant-3c2dea60-17c9-4a08-87be-d3d84a2f734e",
      "assistant_version_id": "20260529T182100983477",
      "telnyx_conversation_channel": "sms_chat",
      "telnyx_agent_target": "+12486093031",
      "telnyx_end_user_target": "+12488237676"
    },
    "results": [
      {
        "insight_id": "cfcc865c-d3d4-4823-8a4b-f0df57d9f56f",
        "result": "Lead Summary: Caller transferred from Legacy Events. Requesting event coordinator. Follow-up needed for event type, date, and guest count."
      }
    ]
  }
}
```

## Inspecting stored insights

### HTTP API

```bash
curl -s "https://webhook.miswitch.cloud/telnyx/insights?secret=$(cat secrets/telnyx_webhook_secret)" \
  | jq '.count, .insights[-1]'
```

Response shape:

```json
{
  "count": 13,
  "insights": [ "... up to 50 most recent stored records ..." ]
}
```

Each element in `insights` is a full **stored record** (server envelope + Telnyx `payload`).

### SQLite

Database file: `data/webhook.db` (or `WEBHOOK_DB_PATH`).

```bash
sqlite3 data/webhook.db "SELECT id, received_at, json_extract(data, '$.payload.event_type') FROM insights ORDER BY received_at DESC LIMIT 5;"
```

Rows are JSON blobs in the `data` column (same structure as the HTTP API items).

### Retention

The server keeps the **500 most recent** insight records. Older rows are deleted automatically when new ones arrive.

## Processing tips

When building downstream automation (CRM, Slack, email digests):

1. **Key on `conversation_id`** to dedupe or correlate with Telnyx conversation APIs.
2. **Key on `insight_group_id`** if you run multiple Insight Groups to different webhook URLs later.
3. **Use the MySwitch `insight_id` map** (or build your own per group) — IDs are stable per insight definition.
4. **Try JSON parse first** on each `result` string; fall back to treating it as plain text or markdown.
5. **Check `metadata.telnyx_conversation_channel`** before assuming call-only fields exist.
6. **Prefer `telnyx_signature_verified: true`** in stored records when auditing that a payload came from Telnyx.

## Related configuration

| Setting | Where | Purpose |
|---------|-------|---------|
| Insight Group webhook URL | Telnyx Mission Control | Point to `https://webhook.<domain>/telnyx/insights` |
| `TELNYX_PUBLIC_KEY` | `.env` | Verify Ed25519 signatures on incoming POSTs |
| `WEBHOOK_SECRET` / secret file | `secrets/telnyx_webhook_secret` | Protect `GET /telnyx/insights` and curl testing |
| `WEBHOOK_DB_PATH` | Compose / env | SQLite file location (default `/data/webhook.db`) |

See [README.md](../README.md) for deployment and [Telnyx Insight Groups documentation](https://developers.telnyx.com/) for portal-side setup.
