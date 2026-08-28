# Sales-Rep Email Notification on Order Ingestion — Design

- **Date:** 2026-08-28
- **Status:** Approved (design discussed in chat; this document records it)
- **Scope:** `integration_gateway/`

## Context

The current ingestion pipeline is:

1. `fluidefy_watcher.sh` (inotify, host) detects a file in Fluidefy's
   `input/sisre2fluide/` directory and POSTs it (base64) to
   `POST /api/v1/orders/notify`.
2. The API returns `202` and enqueues `tasks.process_inbound_xml` (Celery),
   which dedupes by MD5 and persists the file to the `staging_orders` table,
   then enqueues `tasks.sync_pending_order_to_odoo`.
3. `sync_pending_order_to_odoo` decodes the XML, parses it into xsdata
   dataclasses, resolves partners/products, and creates one `sale.order` per
   `<ORDER>` element via `odoo_client.API.create_('sale.order', ...)`. A
   single file may contain **multiple** sale orders. On success the staging
   row is marked `integrated`; on failure `failed` with the error logged in
   `error_log`.

Sales reps currently have no signal that an order arrived; they must watch
Odoo. This feature adds an email notification to a **fixed list** of
recipients whenever the gateway successfully ingests order(s) into Odoo.

## Goal

After `sync_pending_order_to_odoo` creates sale order(s) in Odoo, send one
email per ingested file to a configured fixed list of sales reps, summarizing
what was created.

## Non-goals

- Alerts on **failed** ingestions (may be a follow-up feature).
- Digest/batched emails (one email per file, sent immediately).
- Posting to the Odoo chatter.
- Per-order or per-content recipient routing (recipients are a fixed list).

## Decision: gateway-side notification (Strategy B)

The email is sent by the Celery worker itself, not by Odoo. Rationale:

- Fires **exactly** on gateway ingestion events. An Odoo-side automation
  would fire on any `sale.order` creation and would need tag-based filtering.
- The recipient list and SMTP credentials live in `.env`, which the containers
  already receive via `env_file` — changing reps requires only
  `docker compose up -d` (recreate), never a rebuild.
- All integration logic stays in one version-controlled codebase with tests.
- No dependency on Odoo's outgoing mail server being configured.

Rejected alternative (Strategy A, Odoo-native automation) remains viable if
the requirement ever changes to "notify on *any* order creation".

## Architecture

### New module: `app/services/email_notifier.py`

Standard library only (`smtplib`, `email.mime.text`) — **no new runtime
dependencies**.

Public interface:

```python
def parse_recipients(raw: str) -> list[str]:
    """Split a comma-separated address list, stripping whitespace and
    dropping empty entries. "a@x.com, b@y.com,, " -> ["a@x.com", "b@y.com"]"""

def notify_sales_reps(file_name: str, created_order_names: list[str]) -> None:
    """Send one plain-text notification email to the configured recipients.
    No-op when SALES_NOTIFY_EMAILS is empty. Never raises: SMTP failures are
    logged (logger.exception) and swallowed."""
```

Behavior:

- `parse_recipients` is pure and independently testable.
- `notify_sales_reps` resolves recipients from `settings.SALES_NOTIFY_EMAILS`;
  if the list is empty it logs info and returns (feature disabled).
- Connection: `smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT,
  timeout=10)`; `starttls()` when `SMTP_USE_TLS` is true; `login()` only when
  `SMTP_USER` is non-empty; `sendmail(SMTP_FROM, recipients, msg.as_string())`.

### Hook: `app/workers/tasks.py` (`sync_pending_order_to_odoo`)

Immediately after the staging row is marked `integrated` and committed, and
before `return True`:

1. For each created Odoo id, fetch the human-readable reference via
   `api.read_object("sale.order", odoo_order_id)` (returns a list of dicts
   from `search_read`); use `result[0]["name"]` (e.g. `"SO01234"`), falling
   back to the numeric id string on any per-id error.
2. Call `notify_sales_reps(order_record.file_name, order_names)` inside a
   try/except that logs and continues.

The hook is **best-effort by design**: the sale orders already exist in Odoo
at this point. Letting an email failure propagate would mark the staging row
`failed` and a Celery retry would create duplicate orders. Email failure
therefore must never alter the task outcome.

### Configuration (`app/config.py`, pydantic Settings)

| Setting | Default | Meaning |
|---|---|---|
| `SALES_NOTIFY_EMAILS` | `""` | Comma-separated recipient list; empty disables the feature |
| `SMTP_HOST` | `""` | SMTP relay hostname |
| `SMTP_PORT` | `587` | SMTP relay port |
| `SMTP_USER` | `""` | Login (no AUTH when empty) |
| `SMTP_PASSWORD` | `""` | Password |
| `SMTP_FROM` | `"fluidefy-gateway@localhost"` | Envelope/header From |
| `SMTP_USE_TLS` | `true` | Issue STARTTLS before AUTH/SEND |

`.env.example` documents all of them; real values go in the untracked `.env`.

## Email content

- **From:** `SMTP_FROM`
- **To:** all recipients (single To header, comma-joined)
- **Subject:** `[Fluidefy] New sale order(s) ingested from {file_name}`
- **Body:** plain text, UTF-8 — timestamp (UTC), source file name, one bullet
  per created sale order reference, and the count of orders created.
- **Granularity:** one email per staging file even when the file contains
  several sale orders (all references listed), to avoid email storms on
  multi-order files or 20-file bursts.

## Error handling

| Failure | Behavior |
|---|---|
| Empty `SALES_NOTIFY_EMAILS` | Info log, no-op |
| SMTP connect/auth/send error | `logger.exception`, swallowed; task still returns `True`; staging row stays `integrated` |
| `read_object` fails for one id | That entry falls back to `str(odoo_order_id)`; other ids unaffected |
| SMTP host unreachable (timeout) | 10 s socket timeout caps the delay; swallowed as above |

## Testing strategy

pytest (dev dependency only, in `requirements-dev.txt`), run from
`integration_gateway/`:

- **Unit (Task 1)** — `tests/test_email_notifier.py` with `smtplib.SMTP`
  monkeypatched: recipient parsing; no-op on empty list; From/To/subject
  correctness; STARTTLS + login when configured; no AUTH when `SMTP_USER`
  empty; SMTP exception swallowed.
- **Integration (Task 2)** — `tests/test_sync_notification.py`: the real
  worker task `sync_pending_order_to_odoo` runs against a fake DB session and
  fake `odoo_client.API`, with the committed real-world fixture
  `tests/fixtures/sample_order.xml` (1 order, Fluidefy number 551463).
  Asserts: task returns `True`, staging row marked `integrated`, notifier
  called once with the file name and the fetched SO reference; and that a
  notifier raising does not change the task outcome.

## Deployment & prerequisites

- **Prerequisite:** an SMTP relay reachable from the worker container
  (company relay or transactional provider). Credentials go in `.env`.
- Rollout: merge → add the variables to `.env` → `docker compose up -d`
  (containers read `.env` via `env_file`; **no image rebuild required**).
- Disabling: set `SALES_NOTIFY_EMAILS=` (empty) and `docker compose up -d`.