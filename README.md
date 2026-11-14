# Trusted Trade

Trusted Trade is a minimal FastAPI prototype for the Junction Valio challenge. It follows the repository plan for a safe procurement flow: natural-language buyer request → catalog search → validation → policy evaluation → approval snapshot → order submission only when the backend is configured to reach the mock supplier.

## Features

- Product catalog search against the static Valio challenge dataset
- Request parsing for simple buyer text
- Deterministic policy engine with typed outcomes
- Approval snapshot hashing to prevent unauthorized mutation
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

3. Configure environment variables:

   cp .env.example .env
   # then set VALIO_BASE_URL and VALIO_API_KEY before talking to the live mock API

4. Launch the app:

   uvicorn app.main:app --reload

5. Open the Swagger docs:

   http://127.0.0.1:8000/docs

## Example requests

- Parse a request:

  curl -X POST http://127.0.0.1:8000/api/requests/parse \
    -H 'Content-Type: application/json' \
    -d '{"text":"I need 12 bars of chocolate for customer C-42 by 2026-11-16","customer_id":"C-42","agent_id":"demo-agent"}'

- Search products:

  curl 'http://127.0.0.1:8000/api/catalog/search?query=chocolate&limit=3'

## Notes

- The application intentionally fails closed: without `VALIO_BASE_URL` and `VALIO_API_KEY`, it does not pretend that an order was placed.
- The live Valio Aimo API is configured through environment variables and never committed to source control.
- The static JSON dataset in `data/` is used for deterministic local development and tests.
