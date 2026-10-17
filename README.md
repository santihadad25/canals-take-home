# Orders API

Places an order: it picks the closest warehouse that has **every** requested product, reserves the
stock, and then authorizes and captures the payment with an external provider. A retry with the
same `Idempotency-Key` never creates or charges twice. A background **reconciler** finishes the
orders that were left halfway (a capture that timed out, a payment that never got authorized).

Flask + gunicorn, SQLAlchemy over PostgreSQL. The geocoder and the payment provider are mocks that
run in their own containers.

**Please read [`orders-service-summary.pdf`](orders-service-summary.pdf)**: it explains the design
decisions (order states, when stock is reserved and released, failure analysis) and the two mocks.

## Run it

Needs Docker with Compose. From this folder:

```bash
cp .env.example .env           # the settings: ports, credentials, URLs (skip it if .env exists)
docker compose up -d --build
```

That starts everything, in order (the ports are the ones on your machine, set in `.env`):

| Service | What it is | Port | Variable |
|---|---|---|---|
| `db` | PostgreSQL. **The first time it starts** it creates the tables and loads the demo warehouses with their stock (`db/init/`) | 5432 | `DB_PORT` |
| `geocoding-mock` | Turns an address into coordinates | 8101 | `GEOCODING_MOCK_PORT` |
| `payments-mock` | Card payments provider (authorize, capture, cancel) | 8102 | `PAYMENTS_MOCK_PORT` |
| `api` | The orders API | **8000** | `API_PORT` |
| `reconciler` | Finishes the orders left halfway, every 10 s | - | - |

```bash
curl localhost:8000/health        # {"status":"ok", ...}
docker compose logs -f api reconciler
docker compose down               # stop, keeping the data
docker compose down -v            # stop and throw the data away: the next `up` starts from scratch
```

The scripts in `db/init/` only run when the database is created, so after changing them (or if you
have a volume from an older version) run `docker compose down -v` first.

### Configuration (`.env`)

Nothing is written by hand in `docker-compose.yml`: the credentials, the database names, the
published ports, the provider URLs and the number of API workers are read from the `.env` file next
to it. `.env.example` is the template with the default values; `.env` itself is not meant to be
committed. Edit a value and run `docker compose up -d` again (a variable exported in your shell
wins over the file).

| Variable | Default | What it sets |
|---|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD` | `canals`, `canals` | Database credentials, used by the services that connect to it |
| `POSTGRES_DB`, `POSTGRES_TEST_DB` | `canals`, `canals_test` | The application database, and the one the tests use |
| `DB_PORT`, `API_PORT`, `GEOCODING_MOCK_PORT`, `PAYMENTS_MOCK_PORT` | `5432`, `8000`, `8101`, `8102` | Ports published on your machine |
| `WEB_CONCURRENCY` | `4` | Gunicorn worker processes of the API |
| `GEOCODING_URL`, `PAYMENTS_URL` | `http://geocoding-mock:8000`, `http://payments-mock:8000` | Where the application finds the two providers |

The user, password and database names are applied when the database is first created, so to change
them later run `docker compose down -v` (it deletes the data). The examples below use the default
values; if you change them, use yours.

## The demo data

Warehouses and stock (`db/init/02-demo-data.sql`):

| Product | Price | Buenos Aires | Córdoba | Rosario | Mendoza |
|---|---|---|---|---|---|
| 1 Notebook 14" | 1299.00 | 10 | 5 | 3 | - |
| 2 Wireless mouse | 25.00 | 50 | 30 | 20 | 10 |
| 3 Mechanical keyboard | 89.00 | 20 | 10 | 5 | 10 |
| 4 27" monitor | 249.00 | 5 | 8 | 2 | - |
| 5 HD webcam | 59.00 | - | 15 | - | 4 |

The geocoder mock knows these addresses: `Av. Corrientes 1234, Buenos Aires`,
`Bv. San Juan 450, Córdoba`, `Bv. Oroño 1000, Rosario`, `Av. San Martín 800, Mendoza` and
`Av. Colón 1500, Mar del Plata`. Any other address is "not found".

The payments mock decides what happens from the card number (`mocks/payments/scenarios.json`):

| Card | What happens |
|---|---|
| `4242424242424242` | Everything works |
| `4000000000000002` | Declined at authorization (`card_declined`) |
| `4000000000009995` | Declined at authorization (`insufficient_funds`) |
| `4000000000000507` | Authorizes, but the capture answers too late: the order stays `AUTHORIZED` and the reconciler captures it |
| `4000000000000309` | The authorization never answers in time: the order expires and its stock is released |
| `4000000000000606` | Authorizes, but the capture is rejected: the order fails and its stock is released |

## Try it

Every `POST /orders` needs an `Idempotency-Key` header with a UUID. The examples use fixed ones so
you can copy and paste them; **using the same key again returns the same order** (that is the
point), so change the UUID to place a new one.

### 1. Happy path

An order for Córdoba: 1 notebook and 2 mice. Córdoba is the closest warehouse and has all of it.

```bash
curl -i -X POST localhost:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 11111111-1111-4111-8111-111111111111" \
  -d '{
    "customer_id": "customer-1",
    "shipping_address": "Bv. San Juan 450, Córdoba",
    "items": [{"product_id": 1, "quantity": 1}, {"product_id": 2, "quantity": 2}],
    "payment": {"card_number": "4242424242424242"}
  }'
```

`201 Created`, with the order already `PAID` and shipped from warehouse 2 (Córdoba):

```json
{"id":"2389b9a8-...","status":"PAID","warehouse_id":2,"total_cents":134900,"payment_id":"pay_...",
 "items":[{"product_id":1,"quantity":1,"unit_price_cents":129900},
          {"product_id":2,"quantity":2,"unit_price_cents":2500}], ...}
```

Send **the same request again** (same key): `200 OK` with the same order, and nothing is charged
twice. Reusing the key with a different body is a `422 idempotency_key_reused`.

An order for Buenos Aires goes to warehouse 1 instead, because it is the closest one:

```bash
curl -i -X POST localhost:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 11111111-1111-4111-8111-222222222222" \
  -d '{
    "customer_id": "customer-1",
    "shipping_address": "Av. Corrientes 1234, Buenos Aires",
    "items": [{"product_id": 3, "quantity": 1}],
    "payment": {"card_number": "4242424242424242"}
  }'
```

### 2. No warehouse can fill the order

Nobody has 100 webcams:

```bash
curl -i -X POST localhost:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 22222222-2222-4222-8222-222222222222" \
  -d '{
    "customer_id": "customer-1",
    "shipping_address": "Bv. San Juan 450, Córdoba",
    "items": [{"product_id": 5, "quantity": 100}],
    "payment": {"card_number": "4242424242424242"}
  }'
```

`409 Conflict`. It fails before calling the geocoder or the payment provider, so nothing is
reserved or charged:

```json
{"error":{"code":"out_of_stock","message":"no warehouse has all the requested products in stock"}}
```

There is stock in total, but no **single** warehouse has everything (the order is never split).
There are 19 webcams in the system, but at most 15 in one place:

```bash
curl -i -X POST localhost:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 33333333-3333-4333-8333-333333333333" \
  -d '{
    "customer_id": "customer-1",
    "shipping_address": "Bv. San Juan 450, Córdoba",
    "items": [{"product_id": 1, "quantity": 1}, {"product_id": 5, "quantity": 16}],
    "payment": {"card_number": "4242424242424242"}
  }'
```

Same `409 out_of_stock`.

### 3. Other answers

A declined card (`402`, and the stock goes back to the warehouse):

```bash
curl -i -X POST localhost:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 66666666-6666-4666-8666-666666666666" \
  -d '{
    "customer_id": "customer-1",
    "shipping_address": "Av. Corrientes 1234, Buenos Aires",
    "items": [{"product_id": 2, "quantity": 1}],
    "payment": {"card_number": "4000000000000002"}
  }'
```

```json
{"error":{"code":"payment_failed","reason":"card_declined","order_status":"PAYMENT_FAILED", ...}}
```

An address the geocoder does not know (`422 invalid_address`, nothing is reserved):

```bash
curl -i -X POST localhost:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 77777777-7777-4777-8777-777777777777" \
  -d '{
    "customer_id": "customer-1",
    "shipping_address": "Calle Inexistente 999, Nowhere",
    "items": [{"product_id": 2, "quantity": 1}],
    "payment": {"card_number": "4242424242424242"}
  }'
```

Other errors: `422 invalid_request` for a bad body (for example `"quantity": 0`),
`422 product_not_found`, `400 invalid_idempotency_key` when the header is missing or not a UUID.

### 4. The reconciler finishing an order

This card authorizes the payment but the capture answers too late. The API gives up waiting after
about 10 s and answers `202 Accepted`: the funds are held, the order is `AUTHORIZED`.

```bash
curl -i -X POST localhost:8000/orders \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: 44444444-4444-4444-8444-444444444444" \
  -d '{
    "customer_id": "customer-2",
    "shipping_address": "Av. Corrientes 1234, Buenos Aires",
    "items": [{"product_id": 2, "quantity": 1}],
    "payment": {"card_number": "4000000000000507"}
  }'
```

About 30 to 40 seconds later the reconciler captures it. Watch it happen:

```bash
docker compose logs -f reconciler        # "order ... -> PAID"
docker compose exec db psql -U canals -d canals -c \
  "SELECT status FROM orders WHERE idempotency_key = '44444444-4444-4444-8444-444444444444'"
```

An `AUTHORIZED` order never expires: its capture is retried until it goes through. The other case
is an order that **never got authorized** (card `4000000000000309`): it is `PAYMENT_UNKNOWN` and
holds its stock, and after 2 minutes (`ORDER_TTL_SECONDS`) the reconciler expires it and gives the
stock back. Sending the same request again then answers `409 order_expired`.

### 5. Look inside the database

The database is published on `localhost:5432` (`DB_PORT`), so you can connect with any client (DBeaver,
DataGrip, `psql`): database, user and password are `POSTGRES_DB`, `POSTGRES_USER` and
`POSTGRES_PASSWORD` from `.env` (all `canals` by default). Or use the one inside the container:

```bash
docker compose exec db psql -U canals -d canals
```

```sql
SELECT id, status, warehouse_id, total_cents FROM orders;
SELECT w.name, p.name AS product, s.quantity
  FROM stock s JOIN warehouses w ON w.id = s.warehouse_id JOIN products p ON p.id = s.product_id
  ORDER BY w.id, p.id;
SELECT order_id, from_status, to_status, detail FROM order_events ORDER BY id;  -- status history
```

The mocks can be called directly too: `curl "localhost:8101/geocode?address=Bv.%20San%20Juan%20450,%20C%C3%B3rdoba"`
and `curl localhost:8102/payments/<payment_id>`.

## Tests

They run against a real PostgreSQL (the database `POSTGRES_TEST_DB`, `canals_test` by default, that
`db/init` creates) and the mocks, in-process:

```bash
docker compose --profile test run --rm tests
```

* `tests/test_orders_api.py`: the API end to end: the happy path, the closest warehouse, an order
  nobody can fill, idempotent retries, rejected cards and invalid requests.
* `tests/test_reconciler.py`: expiring orders that never got authorized, capturing the authorized
  ones, and never releasing the stock twice.

## Layout

```
app/
  domain/        orders, statuses and transitions, money, distance (no I/O)
  application/   CreateOrder, OrderPaymentService, ReconcileOrders and the ports they use
  adapters/      api (Flask), persistence (SQLAlchemy), geocoding and payments (HTTP)
  jobs/          the reconciler loop
db/init/         schema, demo data and the test database, run by Postgres on first start
mocks/           the geocoding and payments providers
tests/
```
