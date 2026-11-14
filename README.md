# Trusted Trade

Trusted Trade is a minimal FastAPI prototype for the Junction Valio challenge. It follows the repository plan for a safe procurement flow: natural-language buyer request → catalog search → price/availability checks → policy evaluation → approval snapshot. The published challenge API documents price and availability lookups but no order-submission endpoint, so this prototype does not submit orders.

## Features

- Product catalog search against the static Valio challenge dataset
- Request parsing for simple buyer text
- Deterministic policy engine with typed outcomes
- SQL-backed purchase plan, approval, and audit records (SQLite by default)
- Approval snapshot hashing, server-side snapshot lookup, and separate approver token
- Small browser demo at `/`
- OpenAPI documentation from FastAPI
- Safe fail-closed behavior when supplier credentials are absent

## Run locally

1. Create and activate a virtual environment.
2. Install dependencies:

   python -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt

   If you prefer a direct install, use the project metadata:

   pip install -e .

3. Configure environment variables in `.env`:

   test -f .env || cp .env.example .env
   # set VALIO_API_KEY to the key from the Junction platform
   # VALIO_BASE_URL defaults to https://junction26.valioaimo.fi
   # set TRUSTED_TRADE_ALLOWED_CATEGORIES to buyer-approved category codes
   # configure a private token for each approver before enabling decisions

The demo policy defaults to `demo-agent` associated only with `customer-001`
and sends every complete proposed order for human approval. The mock API does
not enforce buyer-specific category or spending limits, so the app has no such
limits unless you explicitly configure `TRUSTED_TRADE_ALLOWED_CATEGORIES`,
`TRUSTED_TRADE_MAX_LINE_QUANTITY`, or `TRUSTED_TRADE_MAX_ORDER_VALUE`. For
multiple agent/customer associations, use comma-separated
`agent:customer|customer` entries. Approver identities are configured with
`TRUSTED_TRADE_APPROVERS`.

4. Launch the app:

   uvicorn app.main:app --reload

5. Open the Swagger docs:

   http://127.0.0.1:8000/docs

   The visual demo is at `http://127.0.0.1:8000/`.

## Example requests

- Parse a request:

  curl -X POST http://127.0.0.1:8000/api/requests/parse \
    -H 'Content-Type: application/json' \
    -d '{"text":"I need 12 bars of chocolate for customer C-42 by 2026-11-16","customer_id":"C-42","agent_id":"demo-agent"}'

- Search products:

  curl 'http://127.0.0.1:8000/api/catalog/search?query=chocolate&limit=3'

- Compare the shortlist and build a purchase plan:

  curl -X POST http://127.0.0.1:8000/api/purchase-plan/build \
    -H 'Content-Type: application/json' \
    -d '{"agent_id":"demo-agent","customer_id":"customer-001","requested_date":"2026-11-16","line_items":[{"query":"chocolate","quantity":"1","unit":"KI"}]}'

## Notes

- `.env` is loaded automatically and excluded from Git. Keep the platform API key private.
- The challenge API has no order endpoint; the app produces a structured proposal and holds it for human approval without transmitting it.
- Plans, approval snapshots, and audit events are persisted to the configured database. The audit trail is not tamper-proof.
- Approval decisions require a per-approver token from `TRUSTED_TRADE_APPROVER_TOKENS`, sent in the `X-Approver-Token` header. Configure entries as `approver-id:secret` (comma-separated for multiple approvers). The approver must also be on the allowlist and must differ from the agent.
- The default policy does not invent category or spending restrictions; configure them only when buyer-specific rules are known.
- The planner checks a small static-catalog shortlist with batched price/availability API calls and exposes alternatives. Availability values `>= 999999` are reported as unknown, not as stock, and are sent for human review without applying a quantity cap from that sentinel. `closingTime` is checked against the API's `timeNow` for the associated `availableDate`.
- A `429` from Valio is returned as `429` with `Retry-After` (at least 30 seconds); the client suppresses further calls during that cooldown rather than retrying.
- The live Valio Aimo API is configured through environment variables and never committed to source control.
- The static JSON dataset in `data/` is used for deterministic local development and tests.
