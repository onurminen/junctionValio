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
6.  The system persists a proposal for human approval. The challenge API
    has no order-submission endpoint, so this demo must not claim or
    simulate a placed order.
7.  The system records an auditable history of the request, checks,
    approval decision, and proposal status.

**Core principle:** Gemini may interpret, compare, recommend, and
explain. It must never be the authority that grants permission or
directly commits an order. Deterministic backend code validates every
proposal and approval action.

**Confirmed challenge constraints:** mentors confirmed there is no order
API; producing an order proposal for human review is the expected
commitment step. Availability `>= 999999` means quantity is unknown, an
empty availability schedule means unavailable, `0.01` lot size is to be
treated as `1`, and `closingTime` is the cutoff for its associated
`availableDate`. Respect HTTP 429 by waiting at least 30 seconds before
another supplier call. Supplier policy does not enforce buyer approval
or spend limits, so the prototype requires human approval by default.

## 2. Success criteria

### Must work for the demo

-   [x] Backend authenticates to Valio's price/availability API; a live
    smoke check has succeeded. Product discovery itself uses the supplied
    catalog dataset because the challenge API has no search endpoint.
-   [x] Product search returns records from the supplied catalog.
-   [x] Price and availability are checked for selected products,
    quantities, and delivery dates.
-   [x] Request interpretation validates explicit values and asks for
    missing or ambiguous fields. It intentionally supports one product
    line at a time; it does not guess unit conversions.
-   [x] Candidates are filtered by exact category/allergen source codes
    when provided and ranked by eligible EUR catalog price per matching
    sales unit. Allergen names are not translated to codes, and
    unsupported free-form hard constraints are rejected.
-   [x] A validated local proposal can be persisted for human approval.
    No supplier order submission is possible with the challenge API.
-   [x] Agent/customer identity is bound to server-authenticated tokens.
    Configured category, line-quantity, and order-value limits are
    enforced; spending/category limits are not invented when unset.
-   [x] Complete proposals default to human approval before they can be
    approved or rejected.
-   [x] Approval is bound to the exact snapshot hash; changes invalidate
    the decision, and duplicate/contradictory decisions are rejected.
    Approvals do not currently expire on a timer.
-   [x] Proposal, policy, approval, and audit events are persisted.
    Approval and audit reads require a configured approver token.
-   [x] The UI shows price/availability evidence, policy outcome, the
    saved approval snapshot, and explicitly states that no order is sent.
-   [x] Local setup and demo steps are documented; automated tests cover
    the core flow and failure paths.

### Strong additions

-   [x] FastAPI publishes the REST contract as OpenAPI at `/openapi.json`
    and Swagger UI at `/docs`.
-   [deferred] MCP adapter; add only if an external MCP client is needed.
-   [deferred] Supplier order adapter/idempotency; there is no order
    endpoint to call or retry.
-   [deferred] Automated substitution; the demo presents alternatives
    for human review and never silently substitutes.
-   [deferred] Modal deployment and Aiven PostgreSQL; local SQLite is
    sufficient for the current challenge demo.
-   [deferred] Gemini integration; deterministic interpretation is
    currently safer and no model key/benefit is required for the core
    flow.

The deferred items are optional extensions, not blockers to the
challenge's no-order-API demo. The next implementation scope should be
driven by a concrete deployment or judging requirement rather than by
the original speculative stack.

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

**Confirmed:** the published OpenAPI documents price, availability, and
file-download operations, but no order creation, order lookup, or
reservation operation. Do not add a supplier order client or display a
supplier-confirmed order state unless the challenge publishes such an
endpoint.

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
      +---- Structured request interpretation (deterministic; optional Gemini)
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
      +---- Approval service (proposal only; no supplier order endpoint)
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
-   Optional Gemini API through the official Google Gen AI SDK, only if
    available and useful for the demo.
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
-   `OrderProposal`: selected lines, price/availability snapshot,
    approval status, and immutable snapshot/hash. This is not a supplier
    order and has no external order ID.
-   `AuditEvent`: actor, action, timestamp, correlation ID, relevant
    entity IDs, outcome, and structured metadata.

Use real fields and units from the mock API wherever possible. Do not
invent allergen, origin, certification, stock, or sustainability facts.

## 6. Main workflows

### Workflow A: interpret a buyer request

1.  UI sends natural-language text to the backend.
2.  A deterministic interpreter extracts explicit values into a
    structured request. Gemini may be added as an optional assist, but
    is not required for the core demo.
3.  Validate extracted values with Pydantic; model output, if used, is
    untrusted input.
4.  Ask a clarification question if an essential field is missing or
    ambiguous (for example, quantity unit or delivery date).
5.  Record an audit event with safe interpretation metadata. The current
    demo does not store a separate raw-request record.
6.  Return the structured interpretation to the UI for visibility.

Do not silently default a missing product, quantity, sales unit, or
delivery date. Return clarification questions rather than guessing.

### Workflow B: discover and compare products

1.  Search the mock catalog using the relevant terms/category/filters.
2.  Retrieve details for plausible candidates.
3.  Check price and availability for the correct customer/account and
    requested delivery date.
4.  Compare only offers with the same requested sales unit. Do not
    convert units or infer pack-size equivalence unless the API defines
    the conversion.
5.  Exclude candidates that violate hard constraints.
6.  Rank remaining candidates using transparent deterministic scoring or
    lexicographic rules.
7.  Return a shortlist with evidence and reasons.

The challenge API defines `catalogPrice` as price per sales unit and
`comparisonPrice` as a normalized price per KG or L. Rank only eligible
offers in the same requested sales unit and currency by their catalog
price; show the comparison price separately and never compare KG against
L or treat the comparison price as an order price.
When a buyer supplies an exact catalog category code on a request line,
filter candidates by that code before price and availability evaluation.
For allergen exclusions, accept exact source IDs only; require an
unambiguous `FREE_FROM` classification and reject contains, may-contain,
undeclared, conflicting, or missing data. Do not infer allergen IDs from
natural-language names because the supplied files provide no ID-to-name
dictionary.
Reject free-form hard constraints unless they are represented in a
supported structured field; storing unparsed text is not evidence that a
constraint was satisfied.

Separate hard constraints from preferences: - Hard constraints: must be
satisfied (e.g. explicit allergen exclusion, required delivery deadline,
allowed category). - Preferences: optimize where possible (e.g. lower
price, sustainability label, preferred brand).

Never let Gemini invent missing product attributes. If structured data
is absent, mark the attribute unknown and do not claim the product
satisfies the requirement.

### Workflow C: validate and create an order proposal

1.  The agent submits a proposed purchase plan.
2.  Backend checks price and availability for the requested customer
    and delivery date. The challenge API does not expose a reservation
    or order-commit operation.
3.  Backend validates customer/account, product IDs, quantities, units,
    delivery date, order limits, and policy.
4.  Policy engine returns exactly one of:
    -   `AUTO_APPROVED`: valid under the configured policy, but still
        only a proposal because there is no supplier order endpoint.
    -   `PENDING_APPROVAL`: persist the proposal and request human review.
    -   `REJECTED`: hard policy violation; do not approve or submit.
5.  Persist eligible proposals and their policy evidence. Human
    approval is required by default for this demo.
6.  If a human approves, verify the approved snapshot still matches and
    record the decision. Do not transmit it to the supplier.
7.  Record every transition in the audit log.

Never report an order as submitted or confirmed. A human approval means
only that the proposal was approved in Trusted Trade.

### Workflow D: human approval

Approval must be specific to an immutable order snapshot. Include at
least: - Agent and customer/account. - Product IDs, quantities and
units. - Delivery date. - Price/total and currency. - Relevant
constraints and policy outcome. - Snapshot hash or equivalent immutable
version identifier. - Approver identity and decision timestamp.

If any material proposal field changes after approval, require a new
approval. A rejected proposal must not be silently resubmitted. Approval
does not submit an order in this challenge.

### Workflow E: audit trail

Record important events: - Request received and interpreted. - Product
search performed. - Availability/price checked. - Recommendation
created. - Proposal validation result and policy version. - Approval
requested, approved, or rejected. In this challenge there is no order
submission attempt or external order result.

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
  "can_create_purchase_proposals": true,
  "max_order_value": 2000,
  "currency": "EUR",
  "allowed_categories": ["dairy", "vegetables", "meat"],
  "max_quantity_per_line": 1500,
  "allow_substitutions": false,
  "approval_required_for_unknown_price": true
}
```

Rules to implement and test:

-   Missing permission to create a proposal -\> reject.
-   Disallowed product category -\> reject.
-   Order exceeds a hard quantity limit -\> reject or route to approval,
    according to the configured policy.
-   Order exceeds spending authority -\> pending approval.
-   Substitution not explicitly allowed -\> pending approval or reject
    according to policy.
-   Price/availability cannot be verified -\> reject the proposal or
    require human intervention; never claim it is ready to submit.
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

The order endpoints above are future-facing examples only. The current
challenge API does not provide order submission or lookup, so Trusted
Trade must not expose fake `/orders` endpoints or report supplier order
confirmation.

Requirements: - Validate all input with Pydantic. - Return explicit
error codes and actionable messages. - Include correlation/request
IDs. - Use idempotency keys for order submission if possible. - Keep
supplier API credentials server-side. - Do not expose internal supplier
credentials or unrestricted backend access to the agent. - Document
authentication assumptions. Agent tokens are matched to the requested
identity and bound to the owner of any plan submitted for approval. A
seeded demo identity is accepted only in development; production rejects
agent requests unless tokens are configured.

## 9. MCP interoperability

Only implement MCP after the REST services and end-to-end flow work.

Expose a thin MCP server with tools that call the same application
service functions: - `search_products` - `get_product_details` -
`check_availability` - `compare_products` - `validate_order` -
`request_approval` - `get_approval_status`

Do not duplicate business logic inside MCP tool handlers. MCP must not
bypass authorization. Document tool inputs and outputs, and demonstrate
that an external MCP-compatible client can discover and call at least
the read-only tools. Do not expose a `place_order` tool unless the
challenge later publishes a real order endpoint.

## 10. UI requirements

Keep the UI focused on the judging flow. Three views are sufficient:

### Purchasing

-   Natural-language request input.
-   Visible structured interpretation.
-   Product shortlist with price, pack size, availability and delivery
    fit.
-   Selected purchase plan and total.
-   Policy outcome and proposal/approval status; never imply an order
    was submitted.

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

The automated suite uses deterministic catalog data and mocked supplier
responses. It covers request parsing/clarification, category and
allergen filtering, price ranking, availability and policy outcomes,
429 cooldown behavior, approval authentication and snapshot integrity,
audit persistence, and UI/API failure paths. There are deliberately no
unit-conversion tests because the demo does not perform implicit
conversions. A live mock API smoke check has also been performed; repeat
it manually when supplier credentials or challenge data change.

The test boundary must continue to verify that supplier failures,
unknown availability, unauthorized calls, and incomplete proposals
cannot be presented as confirmed orders. The challenge has no endpoint
that can place or confirm one.

## 13. Original 48-hour execution schedule

This schedule is retained as hackathon planning history, not as a list of
remaining implementation work. The core local demo milestones are
complete; deployment and optional integrations are deferred above.

### Hours 0--6: API discovery and foundation

-   Read portal documentation.
-   Probe mock API and save sanitized examples.
-   Confirm order workflow and data model.
-   Create repository, backend, frontend and environment setup.
-   Agree on internal API contracts.
-   Establish a basic health check and database connection.

**Milestone:** Search and availability work against the mock API.

### Hours 6--18: Core purchasing

-   Implement structured request interpretation with explicit
    clarification for missing or ambiguous values. Add Gemini only if a
    key and meaningful benefit are available.
-   Implement catalog search and product details.
-   Implement price/availability checks.
-   Implement unit normalization and candidate ranking.
-   Implement purchase plan response.

**Milestone:** Natural-language request produces a grounded, explainable
purchase proposal without inventing quantity, date, unit, or product.

### Hours 18--30: Trust and transaction flow

-   Implement policy engine and tests.
-   Implement proposal validation and policy evaluation.
-   Implement approval state machine and UI.
-   Persist proposals, approvals and audit events.

**Milestone:** A proposal can be reviewed and approved end-to-end, with
no false claim that a supplier order was placed.

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
2.  Show the structured interpretation and any clarification needed.
3.  Show catalog candidates and live mock price/availability results.
4.  Show the lowest eligible catalog price per matching sales unit and
    the separate kg/litre comparison-price evidence.
5.  Request human approval and show the exact saved proposal.
6.  Show an exception (for example unknown terminal availability or a
    passed closing time) requiring review or rejection.
7.  Approve or reject a proposal as the configured human approver.
8.  State clearly that approval does not submit an order.
9.  Open the audit timeline and trace the proposal and decision.
10. Optionally show an MCP client discovering the same procurement
    tools.

## 15. Judging alignment

  -----------------------------------------------------------------------
  Judging criterion                   Evidence in the demo
  ----------------------------------- -----------------------------------
  Working end-to-end demo (30%)       Real mock API search, availability,
                                      validated proposal and approval

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

  No order endpoint in challenge       Do not create a fake supplier
                                      order state or claim confirmation

  Approval can be reused after edits  Bind approval to immutable order
                                      snapshot

  Price/stock changes before          Treat supplier data as a snapshot;
  submission                          require a fresh check for any future
                                      commit workflow

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
-   Review and decide on an exact, snapshot-bound proposal.
-   See that approval is local and no order was transmitted.
-   Inspect an audit timeline explaining what happened.
-   Read the documented API contract and understand how an external
    agent would integrate.

Anything beyond this is secondary.
