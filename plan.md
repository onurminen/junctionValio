# Trusted Trade --- Implementation Plan

## 1. Project goal

Build a working prototype of an open, permission-aware procurement
interface for autonomous purchasing agents, using Valio Aimo's synthetic
mock APIs.

The demo must complete an end-to-end flow:

1.  A buyer describes a purchasing need in natural language.
2.  The agent converts it into a structured purchasing request.
3.  The system searches the catalog and checks price and availability.
4.  A recommendation component selects a suitable purchasing plan.
5.  A deterministic policy engine decides whether the agent may proceed,
    needs human approval, or must be rejected.
6.  The system places the order through the mock backend when
    authorized.
7.  The system records an auditable history of the request, checks,
    approvals, and order result.

**Core principle:** Gemini may interpret, compare, recommend, and
explain. It must never be the authority that grants permission or
directly commits an order. Deterministic backend code validates every
action.

## 2. Success criteria

### Must work for the demo

-   [ ] Backend can authenticate with and call the Valio Aimo mock API.
-   [ ] Product search returns real mock-catalog results.
-   [ ] Price and availability can be checked for selected products and
    requested quantities/delivery dates.
-   [ ] A natural-language request is converted into validated
    structured data.
-   [ ] Candidate products are ranked against explicit buyer
    requirements.
-   [ ] An order can be validated and submitted to the mock backend.
-   [ ] Agent permissions and spending limits are enforced server-side.
-   [ ] An order can be held for human approval.
-   [ ] Approval is bound to the exact order contents and
    expires/becomes invalid if those contents change.
-   [ ] Important actions are persisted in an audit log.
-   [ ] The UI clearly shows the request, recommendation, policy
    outcome, approval status, and order result.
-   [ ] The project can be run and demonstrated reliably using
    documented setup instructions.

### Strong additions

-   [ ] MCP server exposes the same core procurement capabilities as the
    REST API.
-   [ ] OpenAPI specification documents the external interface.
-   [ ] Idempotency prevents accidental duplicate order submissions.
-   [ ] Partial availability and safe substitution flow is demonstrated.
-   [ ] Backend is deployed on Modal.
-   [ ] Aiven PostgreSQL is used for persistent application data and
    audit records.
-   [ ] Gemini is used meaningfully for request interpretation,
    constrained tool use, and explanations.

### Stretch goals only after the core is stable

-   Signed order receipts or signed audit exports.
-   More advanced agent identity and credential verification.
-   Kafka/event-driven architecture.
-   Recipe-level purchasing, sustainability optimization, or
    carbon-footprint comparison.
-   Complex multi-agent workflows.

## 3. First task: inspect the supplied mock environment

Do this before designing assumptions into the application.

1.  Open the Junction Valio Aimo resource portal and read its API
    documentation.
2.  Identify the base URL, authentication method, available endpoints,
    request/response schemas, and rate limits.
3.  Test the relevant endpoints with a small script or API client.
4.  Determine how customer/account identity is represented.
5.  Determine how products, pack sizes, units, prices, stock, delivery
    dates, and order status are represented.
6.  Confirm whether the mock backend supports order creation, order
    retrieval, and any order validation or availability reservation
    operation.
7.  Save sanitized example requests and responses as fixtures for local
    development and tests.
8.  Record gaps or ambiguous semantics; ask the challenge mentors rather
    than inventing behavior.

Do not assume that example fields in this plan match the real API. Adapt
the internal domain model to the actual mock API and isolate external
API details in an integration adapter.

**Credential hygiene:** Never commit the challenge API key or other
secrets. Use environment variables locally and Modal secrets in
deployment. The key included in the shared challenge description should
be treated as exposed; ask the organizers to rotate it before relying on
it. Do not put credentials in browser code, screenshots, logs, fixtures,
or documentation.

## 4. Proposed architecture

``` text
Buyer / Demo UI
      |
      v
FastAPI application
      |
      +---- Request interpretation (Gemini)
      |
      +---- Product discovery service
      |          |
      |          v
      |     Valio mock API adapter
      |
      +---- Recommendation service
      |
      +---- Policy / authorization engine
      |
      +---- Approval service
      |
      +---- Order service --------> Valio mock API adapter
      |
      +---- Audit service
      |
      +---- PostgreSQL (Aiven)
      |
      +---- MCP adapter (optional, after REST core)
```

### Suggested stack

-   Python 3.12+ (or the version supported by the chosen deployment
    environment).
-   FastAPI for the backend.
-   Pydantic for request/response validation.
-   Gemini API through the official Google Gen AI SDK.
-   PostgreSQL on Aiven for persistence.
-   SQLAlchemy or SQLModel for database access; choose one and keep it
    consistent.
-   Next.js/React for the demo UI, unless the team chooses a simpler
    frontend.
-   OpenAPI generated by FastAPI as the primary API contract.
-   MCP SDK for a thin adapter over existing backend service functions.
-   Modal for deployment once the local end-to-end flow works.
-   Pytest for tests.

Avoid microservices for the hackathon. Use a modular monolith with clear
service boundaries.

## 5. Core domain model

Define internal Pydantic models based on the actual mock API. Keep
external API payloads separate from internal models.

Likely concepts:

-   `PurchaseRequest`: buyer's intent, requested items/categories,
    quantities, units, delivery deadline, constraints, and preferences.
-   `Product`: product identifier, name, description, category, pack
    size, sales unit, allergens/certifications where provided.
-   `Offer`: product, unit price, currency, customer context, price
    timestamp/validity if provided.
-   `Availability`: requested and available quantities, unit, delivery
    date, status, source and checked-at timestamp.
-   `PurchasePlan`: selected line items, quantities, estimated total,
    delivery details, alternatives, and reasons.
-   `AgentIdentity`: agent identifier and associated customer/account.
-   `AgentPolicy`: allowed actions/categories, maximum order value,
    quantity limits, substitution permissions, and approval rules.
-   `OrderValidation`: validation outcome, policy version,
    price/availability snapshot, and reasons.
-   `Approval`: pending/approved/rejected state, approver, timestamp,
    and immutable snapshot/hash of the approved order.
-   `Order`: external order identifier, submitted contents, status, and
    backend response.
-   `AuditEvent`: actor, action, timestamp, correlation ID, relevant
    entity IDs, outcome, and structured metadata.

Use real fields and units from the mock API wherever possible. Do not
invent allergen, origin, certification, stock, or sustainability facts.

## 6. Main workflows

### Workflow A: interpret a buyer request

1.  UI sends natural-language text to the backend.
2.  Gemini converts it into a structured `PurchaseRequest`.
3.  Validate the output with Pydantic.
4.  Ask a clarification question if an essential field is missing or
    ambiguous (for example, quantity unit or delivery date).
5.  Persist the request and an audit event.
6.  Return the structured interpretation to the UI for visibility.

Gemini should return structured data, not prose that the backend must
parse. Treat all model output as untrusted input.

### Workflow B: discover and compare products

1.  Search the mock catalog using the relevant terms/category/filters.
2.  Retrieve details for plausible candidates.
3.  Check price and availability for the correct customer/account and
    requested delivery date.
4.  Normalize units and pack sizes before comparing prices.
5.  Exclude candidates that violate hard constraints.
6.  Rank remaining candidates using transparent deterministic scoring or
    lexicographic rules.
7.  Return a shortlist with evidence and reasons.

Separate hard constraints from preferences: - Hard constraints: must be
satisfied (e.g. explicit allergen exclusion, required delivery deadline,
allowed category). - Preferences: optimize where possible (e.g. lower
price, sustainability label, preferred brand).

Never let Gemini invent missing product attributes. If structured data
is absent, mark the attribute unknown and do not claim the product
satisfies the requirement.

### Workflow C: validate and place an order

1.  The agent submits a proposed purchase plan.
2.  Backend rechecks price and availability immediately before
    commitment if the mock API supports it.
3.  Backend validates customer/account, product IDs, quantities, units,
    delivery date, order limits, and policy.
4.  Policy engine returns exactly one of:
    -   `AUTO_APPROVED`: may proceed without human intervention.
    -   `PENDING_APPROVAL`: hold the order; do not submit it.
    -   `REJECTED`: hard policy violation; do not submit it.
5.  If auto-approved, submit using the mock order endpoint.
6.  If approval is required, persist a pending approval and show it in
    the UI.
7.  After a human approves, verify that the approved order snapshot
    still matches the order being submitted.
8.  Submit the order and persist the external order ID/status.
9.  Record every transition in the audit log.

Do not treat a successful local validation as proof that the external
order was accepted. Only report an order as placed after the mock API
confirms it.

### Workflow D: human approval

Approval must be specific to an immutable order snapshot. Include at
least: - Agent and customer/account. - Product IDs, quantities and
units. - Delivery date. - Price/total and currency. - Relevant
constraints and policy outcome. - Snapshot hash or equivalent immutable
version identifier. - Approver identity and decision timestamp.

If any material order field changes after approval, require a new
approval. A rejected approval must not be silently resubmitted.

### Workflow E: audit trail

Record important events: - Request received and interpreted. - Product
search performed. - Availability/price checked. - Recommendation
created. - Order validation result and policy version. - Approval
requested, approved, or rejected. - Order submission attempted and
external result received.

Each event should include a timestamp, correlation ID, actor, action,
outcome, and relevant IDs. Avoid storing API secrets or unnecessary
personal data. Preserve the source response or a safe normalized
snapshot when useful for explaining the decision.

## 7. Policy engine design

Implement the policy engine as ordinary deterministic Python code with
unit tests. Do not ask Gemini whether an order is authorized.

Example policy configuration (adapt to the real product model):

``` json
{
  "agent_id": "hotel-procurement-agent",
  "can_search_products": true,
  "can_check_availability": true,
  "can_place_orders": true,
  "max_order_value": 2000,
  "currency": "EUR",
  "allowed_categories": ["dairy", "vegetables", "meat"],
  "max_quantity_per_line": 1500,
  "allow_substitutions": false,
  "approval_required_for_unknown_price": true
}
```

Rules to implement and test:

-   Missing ordering permission -\> reject.
-   Disallowed product category -\> reject.
-   Order exceeds a hard quantity limit -\> reject or route to approval,
    according to the configured policy.
-   Order exceeds spending authority -\> pending approval.
-   Substitution not explicitly allowed -\> pending approval or reject
    according to policy.
-   Price/availability cannot be verified -\> do not auto-place; request
    intervention.
-   Product or delivery violates a hard buyer constraint -\> reject
    candidate/order.
-   Policy version and evaluated facts are recorded.

Use integer minor currency units (e.g. cents) or `Decimal` for money.
Never use binary floating point for financial calculations.

## 8. REST API surface

Design the app's own API independently of the supplier's mock API.
FastAPI should publish an OpenAPI document.

Suggested endpoints (adjust names and schemas to fit implementation):

``` http
POST /api/v1/requests/interpret
POST /api/v1/products/search
POST /api/v1/availability/check
POST /api/v1/purchase-plans
POST /api/v1/orders/validate
POST /api/v1/orders
GET  /api/v1/orders/{order_id}
GET  /api/v1/approvals
GET  /api/v1/approvals/{approval_id}
POST /api/v1/approvals/{approval_id}/decision
GET  /api/v1/audit-events
```

Requirements: - Validate all input with Pydantic. - Return explicit
error codes and actionable messages. - Include correlation/request
IDs. - Use idempotency keys for order submission if possible. - Keep
supplier API credentials server-side. - Do not expose internal supplier
credentials or unrestricted backend access to the agent. - Document
authentication assumptions. For a hackathon demo, a seeded demo identity
may be acceptable, but clearly label it as a prototype and keep
authorization checks active.

## 9. MCP interoperability

Only implement MCP after the REST services and end-to-end flow work.

Expose a thin MCP server with tools that call the same application
service functions: - `search_products` - `get_product_details` -
`check_availability` - `compare_products` - `validate_order` -
`place_order` (must enforce the same policy and approval rules) -
`get_order_status`

Do not duplicate business logic inside MCP tool handlers. MCP must not
bypass authorization. Document tool inputs and outputs, and demonstrate
that an external MCP-compatible client can discover and call at least
the read-only tools. If feasible, demonstrate an authorized order or
approval-required response.

## 10. UI requirements

Keep the UI focused on the judging flow. Three views are sufficient:

### Purchasing

-   Natural-language request input.
-   Visible structured interpretation.
-   Product shortlist with price, pack size, availability and delivery
    fit.
-   Selected purchase plan and total.
-   Policy outcome and order status.

### Approvals

-   Pending approval queue.
-   Exact order contents and total.
-   Why approval is required.
-   Agent permissions relevant to the decision.
-   Approve/reject actions.
-   Confirmation after decision.

### Audit

-   Chronological event timeline.
-   Search/filter by request, order, agent, or outcome.
-   Clear explanations of product selection and policy decisions.
-   External order reference where available.

Use explicit loading, empty, error, pending, rejected, and success
states. Never show a success state before the backend confirms it.

## 11. Side-challenge integration

### Gemini

Use Gemini for: - Natural-language request extraction into a validated
schema. - Tool selection / orchestration where useful. - Explaining a
recommendation based on supplied structured evidence.

The model must not: - Invent price, stock, delivery, allergens,
certifications, or product facts. - Decide whether it has permission to
order. - Bypass the policy engine. - Directly call the supplier's order
endpoint with credentials.

### Aiven

Use Aiven PostgreSQL for: - Agent/demo identity and policies. - Purchase
requests and plans. - Approvals. - Orders and order items. - Audit
events.

Use migrations and environment-based connection settings. If database
setup takes too long, use a local PostgreSQL instance with the same
schema and switch to Aiven for the deployed demo.

### Modal

Deploy the FastAPI app or agent endpoint on Modal after local
functionality works. Store secrets using Modal Secrets. Add a health
endpoint. Test cold-start behavior and ensure the demo can tolerate
startup latency. Do not make Modal deployment a blocker for the local
end-to-end implementation.

## 12. Testing strategy

Minimum automated tests:

### Unit tests

-   Request schema validation.
-   Unit/pack-size conversions.
-   Price and total calculations.
-   Product filtering and ranking.
-   Policy engine outcomes.
-   Approval snapshot matching.
-   Idempotency behavior.

### Integration tests

-   Catalog search against the mock API.
-   Price and availability lookup.
-   Order validation.
-   Order creation and retrieval.
-   Database persistence.

### End-to-end demo tests

1.  Within-limit order is placed automatically.
2.  Over-limit order is held for approval, then placed after approval.
3.  Rejected approval prevents order placement.
4.  Modified order after approval requires a new approval.
5.  Partial availability results in alternatives or a clear exception.
6.  Supplier API failure does not produce a false success.
7.  Retried order submission does not create a duplicate if idempotency
    is supported.

Use mock fixtures for repeatable tests, but verify the final demo
against the actual supplied mock backend.

## 13. 48-hour execution schedule

### Hours 0--6: API discovery and foundation

-   Read portal documentation.
-   Probe mock API and save sanitized examples.
-   Confirm order workflow and data model.
-   Create repository, backend, frontend and environment setup.
-   Agree on internal API contracts.
-   Establish a basic health check and database connection.

**Milestone:** Search and availability work against the mock API.

### Hours 6--18: Core purchasing

-   Implement request interpretation with Gemini.
-   Implement catalog search and product details.
-   Implement price/availability checks.
-   Implement unit normalization and candidate ranking.
-   Implement purchase plan response.

**Milestone:** Natural-language request produces a grounded, explainable
purchase plan.

### Hours 18--30: Trust and transaction flow

-   Implement policy engine and tests.
-   Implement order validation and placement.
-   Implement approval state machine and UI.
-   Persist orders, approvals and audit events.

**Milestone:** At least one order can be placed end-to-end, with both
automatic and approval-required paths.

### Hours 30--38: Interoperability and deployment

-   Add MCP adapter if core is stable.
-   Deploy backend on Modal.
-   Configure Aiven PostgreSQL.
-   Test external API/MCP access and credentials.

**Milestone:** Deployed demo and documented agent interface.

### Hours 38--48: Exceptions, polish and presentation

-   Add partial availability/substitution path.
-   Improve UI and audit timeline.
-   Run end-to-end tests and fix failure states.
-   Prepare scripted demo and architecture explanation.
-   Freeze features before final presentation.

**Milestone:** Reliable demo with clear evidence for all judging
criteria.

## 14. Demo script

Use the actual catalog and supported delivery dates from the mock API.
Do not hardcode fictional product availability or pretend an order was
submitted.

1.  Submit a simple request that fits the agent's policy.
2.  Show Gemini's structured interpretation.
3.  Show catalog candidates and live mock price/availability results.
4.  Show the selected plan and deterministic reasoning.
5.  Place the order and show the mock backend confirmation.
6.  Submit a second request that exceeds the spending limit.
7.  Show the pending approval in the dashboard.
8.  Approve it as the buyer and show the resulting order confirmation.
9.  Open the audit timeline and trace both transactions.
10. Optionally show an MCP client discovering the same procurement
    tools.

## 15. Judging alignment

  -----------------------------------------------------------------------
  Judging criterion                   Evidence in the demo
  ----------------------------------- -----------------------------------
  Working end-to-end demo (30%)       Real mock API search, availability,
                                      validation and order creation

  Solving user need (20%)             Structured intent, correct unit
                                      handling, grounded product matching

  Autonomy and controls (20%)         Deterministic policies, approval
                                      state machine, exact-order approval

  Robustness (15%)                    Partial availability, stale prices,
                                      retries, API failures and
                                      substitutions

  Openness and production path (15%)  OpenAPI, JSON schemas, MCP adapter,
                                      documented security boundaries
  -----------------------------------------------------------------------

## 16. Risks and mitigations

  -----------------------------------------------------------------------
  Risk                                Mitigation
  ----------------------------------- -----------------------------------
  Mock API is incomplete or confusing Inspect it first; ask mentors;
                                      isolate it behind an adapter

  Product units/pack sizes are        Normalize units explicitly and test
  inconsistent                        conversions

  Gemini returns invalid or invented  Structured output, Pydantic
  data                                validation, backend evidence checks

  Duplicate orders from retries       Idempotency keys and persisted
                                      submission state

  Approval can be reused after edits  Bind approval to immutable order
                                      snapshot

  Price/stock changes before          Recheck immediately before order if
  submission                          supported

  Aiven/Modal setup consumes time     Local-first development; deploy
                                      after core works

  MCP consumes time without improving Treat MCP as a thin, late-stage
  demo                                adapter

  Demo depends on external services   Prepare sanitized fixtures and a
                                      clearly labeled fallback mode
  -----------------------------------------------------------------------

## 17. Definition of done

The project is ready when a judge can:

-   Enter a natural-language purchasing request.
-   See a structured interpretation and real catalog candidates.
-   Understand why a product was selected.
-   See a deterministic policy decision.
-   Complete an automatic order or a human-approved order.
-   See the confirmed result from the mock backend.
-   Inspect an audit timeline explaining what happened.
-   Read the documented API contract and understand how an external
    agent would integrate.

Anything beyond this is secondary.
