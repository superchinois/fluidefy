# Fluidefy → Odoo ERP Integration Gateway

A decoupled, asynchronous pipeline that ingests inbound XML order files dropped by the
**Fluidefy** system into a Linux directory, stages them in a PostgreSQL audit table, and
automatically creates **Sales Orders** in an **Odoo ERP** instance via XML-RPC.

---

## Architecture Overview

```
  Fluidefy XML file
        │
        ▼
┌──────────────────────┐   HTTP POST   ┌─────────────────────┐   Celery task   ┌─────────────────────┐
│  Bash inotify watcher│ ────────────► │  FastAPI Gateway    │ ──────────────► │  Celery Worker       │
│  (systemd service)   │   /notify     │  (uvicorn :8000)    │   (Redis broker)│                      │
└──────────────────────┘               └─────────────────────┘                 └─────────────────────┘
        │                                                                               │
        │ moves file to                                                                 │ reads/writes
        │ processed/ or error/                                                          ▼
        ▼                                                                     ┌─────────────────────┐
┌──────────────────────┐                                                     │  PostgreSQL         │
│  /var/lib/fluidefy/   │                                                     │  staging_orders     │
│  orders/inbound       │  ◄── at-least-once delivery & error isolation        └─────────────────────┘
└──────────────────────┘                                                          │
                                                                                  │ Celery task
                                                                                  ▼
                                                                       ┌─────────────────────┐
                                                                       │  Odoo ERP (XML-RPC) │
                                                                       │  sale.order.create  │
                                                                       └─────────────────────┘
```

### Data Flow

1. **File arrival** – Fluidefy writes an XML order file to
   `/var/lib/fluidefy/orders/inbound/`.
2. **Watch & notify** – A `systemd`-managed bash script using `inotifywait` detects the
   `close_write` event, base64-encodes the file contents, and POSTs them (with the file
   name) as JSON to the FastAPI gateway (content delivery — the API never touches the
   file on disk). On `202 Accepted` the file is moved to `processed/`; on any other
   status it is moved to `error/`.
3. **API gateway** – FastAPI validates the payload with Pydantic and immediately hands
   the heavy work to a Celery worker (`tasks.process_inbound_xml`), returning `202`
   without blocking.
4. **Staging** – The Celery worker decodes the delivered base64 content, computes an
   **MD5 hash** (for
   idempotency / deduplication), and persists the base64 content in a row of the
   PostgreSQL `staging_orders` table with status `pending`.
5. **Odoo sync** – A second Celery task (`tasks.sync_pending_order_to_odoo`) is
   triggered. It decodes the XML, authenticates against the Odoo XML-RPC API, resolves
   the buyer customer **by its barcode (GLN)** and each product, and calls
   `sale.order.create`. The staging record is then updated to `integrated` (or `failed`
   with an error log on exception). Each order is flagged with a configured tag and
   assigned a responsible user (`ODOO_SO_TAGS_ID` / `ODOO_SO_USER_ID`).
6. **Monitor** – A `GET /api/v1/monitor` HTML page lists the state of every staged
   order (file name, arrival time, status, error log) straight from `staging_orders`.

---

## Project Structure

```
.
├── fluidefy-watcher.service      # systemd unit for the inotify bash watcher
├── fluidefy_watcher.sh            # bash watcher script (inotify + curl webhook)
├── XSD/                           # Fluidefy EDI message schemas (Orders, Invoic, ...)
└── integration_gateway/           # FastAPI + Celery ingestion microservice
    ├── app/                       # application package
    │   ├── main.py                # FastAPI entrypoint (includes API router)
    │   ├── config.py              # pydantic-settings (DB URL, Redis URL, Odoo)
    │   ├── database.py            # SQLAlchemy engine & session factory
    │   ├── api/                   # HTTP layer
    │   │   └── routes.py          # POST /orders/notify + GET /monitor
    │   ├── models/                # ORM models
    │   │   └── staging.py         # StagingOrder model
    │   ├── schemas/               # Pydantic request/response schemas
    │   │   └── order.py
    │   ├── workers/               # Celery app & tasks
    │   │   └── tasks.py           # ingest XML + sync to Odoo
    │   ├── services/              # business logic
    │   │   ├── fluidefy_mapper.py # XML → Odoo sale.order mapping
    │   │   ├── partner_resolver.py# resolve buyer res.partner by barcode (GLN)
    │   │   └── product_resolver.py# resolve product.product ids via Cache
    │   └── fluidefy_models/       # generated xsdata models (regenerated via `make models`)
    ├── scripts/
    │   └── generate_models.py     # xsdata codegen from the XSD schemas
    ├── odoo_module/
    │   └── fluidefy_sync.py       # optional Odoo-side cron module (psycopg2)
    ├── tests/                     # test package
    ├── Dockerfile                 # image for api + worker
    ├── docker-compose.yml         # redis, postgres, api, worker
    ├── Makefile                   # targets: models, api, worker, up
    ├── requirements.txt           # Python dependencies
    ├── .env.example               # env var template (copy to .env)
    └── implementation.md          # design document & deployment guide
```

---

## Key Design Principles

| Principle              | How it's achieved                                                                 |
|------------------------|-----------------------------------------------------------------------------------|
| **At-least-once delivery** | Files stay in `inbound/` until a `202` is received, then move to `processed/` or `error/`. |
| **Idempotency**        | MD5 file hash + unique DB constraint prevents duplicate processing.              |
| **Decoupled async**    | The API returns `202` immediately; hashing, DB writes, and Odoo calls run in Celery workers. |
| **Audit trail**        | Every file is persisted (base64 + hash + status + error log) in PostgreSQL.     |
| **Fault isolation**    | Failed syncs set `processing_status = 'failed'` with a full error message; the watcher's error dir isolates notification failures. |

---

## Prerequisites

### System packages (watcher host)

```bash
sudo apt-get install inotify-tools jq curl
```

### Infrastructure services

| Service      | Default URL / Port              |
|--------------|---------------------------------|
| Redis        | `redis://localhost:6373/0`     |
| PostgreSQL   | `postgresql://user:password@localhost:5432/fluidefy_staging` |
| Odoo ERP     | `http://localhost:8069`         |

### Environment variables

```bash
DATABASE_URL=postgresql://user:password@localhost:5432/fluidefy_staging
REDIS_URL=redis://localhost:6373/0

ODOO_URL=http://localhost:8069
ODOO_DB=my_odoo_database
ODOO_USERNAME=admin
ODOO_APIKEY=your_odoo_password_or_api_key

# Tag id used to flag sale orders created from Fluidefy (crm.tag)
ODOO_SO_TAGS_ID=0
# User id responsible for sale orders created from Fluidefy (res.users)
ODOO_SO_USER_ID=0
```

---

## Deployment

### 1. Directory layout on the watcher host

```bash
sudo mkdir -p /var/lib/fluidefy/orders/{inbound,processed,error}
sudo cp fluidefy_watcher.sh /usr/local/bin/fluidefy_watcher.sh
sudo chmod +x /usr/local/bin/fluidefy_watcher.sh
sudo cp fluidefy-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fluidefy-watcher
```

### 2. API gateway & workers

The recommended path is to run the whole stack with Docker Compose from
`integration_gateway/`:

```bash
cd integration_gateway
cp .env.example .env        # then fill in real Odoo credentials

docker compose build
docker compose up -d        # starts redis, postgres, api, worker
```

> **PostgreSQL 18+ volume note** – the Postgres 18 image requires the volume to be
> mounted at `/var/lib/postgresql` (not `/var/lib/postgresql/data`). If upgrading from
> an older Postgres image, remove the old volume first (`docker compose down -v`) to
> avoid stale-data startup errors.

Alternatively, run the services directly:

```bash
cd integration_gateway
pip install -r requirements.txt

# Terminal 1 – FastAPI
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Terminal 2 – Celery worker
celery -A app.workers.tasks.celery_app worker --loglevel=info
```

### 3. Odoo-side (optional)

`fluidefy_sync.py` (under `odoo_module/`) is an alternative Odoo `TransientModel` cron job that queries the
`staging_orders` table directly via psycopg2 and can be used instead of (or alongside)
the Celery-based XML-RPC sync. Enable it as a scheduled action inside your Odoo custom
module if you prefer pull-based integration.

---

## Configuration

Edit the top of `fluidefy_watcher.sh` to change the watch directory or webhook URL:

```bash
WATCH_DIR="/var/lib/fluidefy/orders/inbound"
PROCESSED_DIR="/var/lib/fluidefy/orders/processed"
ERROR_DIR="/var/lib/fluidefy/orders/error"
WEBHOOK_URL="http://localhost:8000/api/v1/orders/notify"
```

Database and Redis connection strings are configured in `integration_gateway/app/config.py`
(or via a `.env` file / environment variables thanks to `pydantic-settings`).

Odoo connection parameters are read from environment variables in `integration_gateway/app/workers/tasks.py`
(`ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_APIKEY`). The tag and responsible-user ids
applied to every created sale order come from `ODOO_SO_TAGS_ID` and `ODOO_SO_USER_ID`
(defined in `integration_gateway/app/config.py`).

---

## API Reference

### `POST /api/v1/orders/notify`

Accepts a notification that a new XML file has arrived. Enqueues a Celery task for
async processing and returns immediately.

**Request body** — the watcher base64-encodes the raw file contents and sends
``file_name`` plus ``file_content_base64``:

```json
{
  "file_name": "order_12345.xml",
  "file_content_base64": "PD94bWwgdmVyc2lvbiD..."
}
```

**Response — `202 Accepted`**

```json
{
  "status": "accepted",
  "message": "File delivery parsing request handed over successfully to Celery Worker pipeline.",
  "task_id": "c1a2b3c4-d5e6-..."
}
```

### `GET /api/v1/monitor`

Renders a simple HTML page (no JS framework) listing every staged order read from the
`staging_orders` table, newest first: ID, file name, arrival time, status
(`pending` / `integrated` / `failed`, color-coded) and error log. Includes a manual
"Refresh" link. Open it directly in a browser:
`http://localhost:8000/api/v1/monitor`.

---

## License

Distributed under the [MIT License](LICENSE).