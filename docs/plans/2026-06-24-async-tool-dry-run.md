# Async Tool Dry-Run Implementation

Promoted from `/home/hermes/daily-builds/2026-06-23-telnyx-async-tool-lab` into `telnyx-webhook-server`.

## Goal

Support Telnyx AI Assistant async webhook tool prototypes safely:

1. Acknowledge tool requests quickly.
2. Run backend work after the ACK.
3. Persist the job lifecycle in SQLite.
4. Prepare the Telnyx Add Messages API payload that would inject a `system` message into the live conversation.
5. Do **not** send external API writes yet.

## Implemented Routes

| Route | Purpose |
|-------|---------|
| `POST /telnyx/tools/async/{tool_name}` | Shared-secret protected async tool receiver. Requires `x-telnyx-call-control-id`. |
| `GET /admin/tools/async-jobs` | Admin UI table of dry-run jobs. |
| `GET /admin/api/async-jobs` | JSON list of jobs for inspection/testing. |

## Current Tool Behavior

`order-status` uses a dry-run local mock:

- Extracts `order_id`, `orderId`, or `case_id`.
- Redacts caller phone when possible.
- Stores a result like `ready_for_pickup`.
- Prepares this Add Messages shape:

```json
{
  "call_control_id": "...",
  "messages": [
    {
      "role": "system",
      "content": "Background lookup complete for order TEST-42: status=ready_for_pickup, eta=0 minutes."
    }
  ]
}
```

Any other `{tool_name}` gets a generic dry-run result.

## Next Step Before Production Sends

Replace the dry-run mock with one of:

- n8n webhook call
- Tripleseat availability lookup
- Zoho ticket/customer lookup
- NetSapiens call context enrichment

Only after testing on a separate assistant/number should the server send `add_messages_dry_run` to the real Telnyx Add Messages API.
