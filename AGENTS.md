# AGENTS.md --- Trusted Trade

Instructions for AI coding agents working in this repository.

## 1. Mission

Build **Trusted Trade**, a hackathon prototype for safe, open
procurement by external AI purchasing agents, integrated with Valio
Aimo's synthetic mock APIs.

The primary objective is a reliable end-to-end demo:

`buyer request → structured intent → catalog search → price/availability → purchase plan → policy evaluation → approval or order → audit trail`

Read `plan.md` before making architectural or implementation decisions.
This file defines the working rules for coding agents.

## 2. Non-negotiable engineering principles

1.  **Backend owns authority.** The LLM may propose actions but cannot
    authorize purchases.
2.  **Never fake transactions.** Report an order as placed only after
    the mock backend confirms it.
3.  **Ground product claims in API data.** Never invent stock, prices,
    delivery dates, allergens, ingredients, origins, certifications, or
    sustainability attributes.
4.  **Enforce permissions server-side.** Frontend checks are not
    security controls.
5.  **Approval is order-specific.** Bind approval to an immutable
    snapshot of products, quantities, units, prices, currency,
    customer/account and delivery date. Any material change invalidates
    approval.
6.  **Keep supplier credentials secret.** Never expose them to the
    browser, commit them, print them in logs, or include them in
    fixtures.
7.  **Prefer simple, testable architecture.** Use a modular monolith; do
    not introduce microservices or infrastructure without a concrete
    need.
8.  **Use open contracts.** Document the app's REST API with OpenAPI and
    use JSON-compatible schemas. MCP, if implemented, must call the same
    service layer.
9.  **Fail closed for purchasing.** If authorization, price,
    availability, customer context, or required validation cannot be
    established, do not submit an order.
10. **Optimize for the working demo.** Complete the core transaction and
    approval flow before optional features.

## 3. Start every task by inspecting the repository

Before editing: - Inspect the existing directory structure, README,
dependency files, tests, and current implementation. - Check Git status.
Do not overwrite or discard user changes. - Read the relevant parts of
`plan.md`. - Identify existing conventions and reuse them. - If API
documentation or example payloads are present, inspect them before
writing integration code. - Do not assume an endpoint, field, unit,
authentication scheme, or response format exists. Confirm it from the
supplied docs or a successful API response.

If the task is underspecified, choose the smallest safe implementation
consistent with the plan. Ask a concise question only when a missing
decision blocks correct implementation.

## 4. Valio mock API integration

-   Put all supplier-specific HTTP calls in a dedicated adapter/client
    module.
-   Keep supplier request/response models separate from internal domain
    models.
-   Configure base URL and API key through environment variables or the
    deployment secret manager.
-   Set sensible timeouts and handle HTTP errors, invalid JSON, network
    errors and rate limits.
-   Do not log authorization headers, API keys, or other secrets.
-   Preserve source identifiers and relevant timestamps from supplier
    responses.
-   Validate and normalize data at the adapter boundary.
-   Do not silently convert units. Make conversions explicit and test
    them.
-   Do not assume a price is per litre, kilogram, item, case, or package
    until the API documentation establishes the meaning.
-   Do not assume availability is a reservation. Treat it as a snapshot
    unless the API explicitly guarantees otherwise.
-   Recheck availability and price immediately before order commitment
    if the mock API supports it.
-   Keep a sanitized fixture for tests; never include credentials or
    confidential data.

If the mock API differs from `plan.md` examples, follow the actual API
and update the plan/documentation accordingly.

## 5. Gemini and LLM usage

Use Gemini for tasks that benefit from language understanding: -
Extracting structured requirements from buyer text. - Mapping ambiguous
natural-language product descriptions to catalog search terms. -
Choosing which read-only discovery tools to call. - Explaining a
recommendation using supplied evidence.

Rules: - Prefer structured output / function calling with explicit
schemas. - Validate every model response using Pydantic or equivalent. -
Treat model output as untrusted input. - Do not let the model choose its
own permissions or override policy. - Do not let the model directly
submit orders or access supplier credentials. - Do not use
model-generated product facts as evidence. - If a required field is
ambiguous, return a clarification requirement rather than guessing. -
Include only verified catalog and availability facts in explanations. -
Keep prompts concise and version them if prompt behavior is important to
the demo. - Handle model timeouts, rate limits, malformed output and
unavailable API keys gracefully.

The deterministic backend must independently validate every proposed
purchase plan.

## 6. Policy and purchasing rules

Implement authorization in deterministic code, with unit tests.

The policy engine should return a typed outcome, not an ambiguous
Boolean:

-   `AUTO_APPROVED`: backend may proceed to order submission.
-   `PENDING_APPROVAL`: persist a pending approval; do not submit.
-   `REJECTED`: hard violation; do not submit.

At minimum evaluate: - Agent identity and associated customer/account. -
Permission to place orders. - Allowed product categories. - Per-line
quantity limits. - Maximum order value and currency. - Buyer hard
constraints. - Delivery feasibility. - Substitution permissions. -
Whether price and availability are sufficiently verified.

Additional rules: - Use `Decimal` or integer minor units for money;
never binary floats. - Reject unknown or unsupported currencies rather
than comparing unlike amounts. - Keep hard constraints separate from
soft preferences. - Record the policy version and facts used in each
evaluation. - Do not treat a pending approval as an order. - Do not let
an agent approve its own order unless an explicit, separately authorized
role and policy allow it. Default to requiring a distinct human
approver.

## 7. Approval state machine

Use explicit states and valid transitions. A minimal model:

``` text
DRAFT
  └─ validate ─> AUTO_APPROVED ─> SUBMITTED ─> CONFIRMED / FAILED

DRAFT
  └─ validate ─> PENDING_APPROVAL
                    ├─ human approve ─> APPROVED ─> SUBMITTED ─> CONFIRMED / FAILED
                    ├─ human reject ─> REJECTED
                    └─ cancel/expire ─> CANCELLED / EXPIRED
```

Implementation requirements: - Persist the exact order snapshot at
approval request time. - Calculate a stable hash or immutable version
identifier for the snapshot. - On approval, verify the snapshot has not
changed. - Record who approved/rejected and when. - Prevent duplicate or
contradictory decisions. - Require a new approval if any material field
changes. - Do not submit an order before approval is complete. - Make
state transitions transactional where possible.

## 8. Orders, retries and idempotency

-   Keep order validation separate from order submission.
-   Generate and persist an idempotency key for each intended submission
    if supported by the supplier API.
-   If the supplier API does not support idempotency, persist submission
    attempts and avoid blindly retrying an ambiguous timeout. First
    determine whether the original request succeeded, using the
    available lookup endpoint if possible.
-   Store the supplier's order ID and status.
-   Distinguish local states (`PENDING_APPROVAL`, `READY_TO_SUBMIT`,
    `SUBMISSION_UNKNOWN`) from supplier-confirmed states.
-   Never display a successful order state based only on a local
    database write.

## 9. Audit and observability

Record meaningful events, including: - Request received/interpreted. -
Search and availability checks. - Recommendation generated. - Policy
evaluation. - Approval requested and decision recorded. - Order
submission attempt. - Supplier confirmation or failure.

Audit events should include: - UTC timestamp. - Correlation/request
ID. - Actor/agent identity. - Action and outcome. - Relevant
request/order/approval IDs. - Policy version where relevant. - Safe
structured metadata.

Do not log secrets, authorization tokens, or unnecessary personal data.
Do not claim the audit log is cryptographically immutable unless
tamper-evidence has actually been implemented.

## 10. API and MCP design

-   FastAPI routes should be thin: parse/validate input, call
    application services, return typed responses.
-   Keep business logic in service modules, not route handlers.
-   Use explicit response models and stable error shapes.
-   Include correlation IDs in logs and responses.
-   Publish OpenAPI documentation.
-   Do not expose supplier credentials or raw unrestricted supplier
    endpoints to external agents.
-   Apply authentication/authorization to state-changing routes.
-   For a demo identity, make the prototype limitation explicit; never
    silently treat an arbitrary caller as an authorized buyer.

If implementing MCP: - Add it only after REST business functions work. -
MCP handlers must call the same service functions as REST. - Do not
duplicate or bypass policy logic. - Start with read-only tools, then add
validate/order tools with the same controls. - Make tool schemas
explicit and return structured results. - Never expose a direct
unguarded supplier order tool.

## 11. Database and migrations

-   Use Aiven PostgreSQL in deployment and a compatible local database
    for development.
-   Keep database credentials in environment variables or secret
    storage.
-   Use migrations for schema changes.
-   Use foreign keys and appropriate indexes for order, approval and
    audit lookups.
-   Store money as integer minor units or exact numeric types.
-   Store timestamps in UTC.
-   Use transactions for approval decisions and order state changes.
-   Avoid storing full raw supplier payloads unless needed; sanitize and
    minimize persisted data.

## 12. Frontend rules

-   The frontend is not an authority boundary.
-   Never put supplier API keys or privileged credentials in client-side
    code.
-   Display the difference between recommendation, validation, approval,
    submission and supplier confirmation.
-   Show price currency and unit basis clearly.
-   Show availability check time and delivery date where available.
-   Show unknown data as unknown, not as a positive claim.
-   Provide clear loading, empty, error, pending, rejected and confirmed
    states.
-   Disable repeated submit/approve actions while requests are in
    progress, but also enforce idempotency and state checks on the
    backend.
-   Explain why an order requires approval or was rejected.
-   Do not display invented ratings, certifications, product images or
    other catalog attributes.

## 13. Testing expectations

Add or update tests with every meaningful change.

Prioritize tests for: - Schema validation and malformed model output. -
Unit conversions and pack-size calculations. - Money calculations and
currency handling. - Product filtering/ranking against known fixtures. -
Every policy outcome. - Approval snapshot mismatch. - Duplicate approval
decisions. - Idempotent order submission and ambiguous timeout
handling. - Supplier API errors and malformed responses. - Audit events
for all important state transitions.

For purchasing-related code, include negative tests proving that
unauthorized or uncertain actions do not reach the supplier order
endpoint.

Do not remove or weaken tests simply to make a build pass.

## 14. Code quality and workflow

-   Prefer small, cohesive modules and clear names.
-   Add type annotations to public functions and service boundaries.
-   Use Pydantic models for external input and output.
-   Avoid unnecessary abstractions, generic frameworks, and premature
    optimization.
-   Do not add dependencies without a clear need.
-   Keep `.env` out of Git; provide `.env.example` with placeholder
    values only.
-   Update README with setup, run, test, and deployment instructions
    when relevant.
-   Run formatter, linter, type checker and relevant tests if
    configured.
-   Before finishing, inspect the diff and ensure no secrets or
    generated clutter were added.
-   Report what changed, commands/tests run, known limitations, and any
    follow-up required.

## 15. Hackathon prioritization

When time is limited, implement in this order:

1.  Real mock API connection.
2.  Product search and availability.
3.  Structured request interpretation.
4.  Grounded purchase plan and price calculations.
5.  Deterministic policy engine.
6.  Real order placement.
7.  Human approval flow.
8.  Persistent audit trail.
9.  Usable demo UI.
10. Modal deployment and Aiven integration.
11. MCP adapter.
12. Substitution and other stretch features.

Never sacrifice the core end-to-end flow to add a side-challenge
feature.

## 16. Definition of done for a coding task

Before declaring a task complete: - Implementation matches the actual
repository conventions. - Relevant tests pass. - Failure paths are
handled. - No secrets are exposed. - Authorization is enforced on the
backend. - Documentation is updated if behavior or setup changed. - The
final response summarizes files changed, tests run, and remaining
caveats.
