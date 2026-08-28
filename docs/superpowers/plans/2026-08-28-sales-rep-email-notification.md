# Sales-Rep Email Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Email a fixed list of sales reps after the gateway successfully creates sale order(s) in Odoo, one email per ingested file.

**Architecture:** The Celery worker task `sync_pending_order_to_odoo` calls a new best-effort notifier (`app/services/email_notifier.py`, stdlib `smtplib`) right after marking the staging row `integrated`. Recipients and SMTP credentials come from `.env` (already injected into containers via `env_file`), so rollout is a config change plus `docker compose up -d` — never a rebuild.

**Tech Stack:** Python 3.11, stdlib `smtplib`/`email.mime` (no new runtime deps), pytest (dev only), Celery worker, pydantic-settings.

**Spec:** `docs/superpowers/specs/2026-08-28-sales-rep-email-notification-design.md`

## Global Constraints

- No new **runtime** dependencies: only `smtplib` and `email.mime` (stdlib). pytest is the only new dependency and goes in `requirements-dev.txt`.
- `notify_sales_reps` must **never raise**: the sale orders already exist in Odoo when it runs; an email failure must not mark the staging row `failed` (a Celery retry would duplicate the orders).
- One email per staging file, even if the file contains several sale orders.
- Feature is off (no-op with info log) when `SALES_NOTIFY_EMAILS` is empty.
- All test commands run from `integration_gateway/` using `.venv/bin/pytest`.
- Commit messages follow the repo's existing conventional style (`feat:`, `test:`, `docs:`).

---

### Task 1: Email notifier module (settings + unit tests)

**Files:**
- Create: `integration_gateway/requirements-dev.txt`
- Create: `integration_gateway/conftest.py`
- Modify: `integration_gateway/Makefile` (append a `test` target)
- Modify: `integration_gateway/app/config.py` (append 7 settings fields to `Settings`)
- Modify: `integration_gateway/.env.example` (append the notification block)
- Create: `integration_gateway/tests/test_email_notifier.py`
- Create: `integration_gateway/app/services/email_notifier.py`

**Interfaces:**
- Consumes: `app.config.settings` (pydantic `Settings` singleton, already exists).
- Produces (used by Task 2):
  - `parse_recipients(raw: str) -> list[str]`
  - `notify_sales_reps(file_name: str, created_order_names: list[str]) -> None` — never raises.

- [ ] **Step 1: Create the dev-requirements file and install pytest**

Create `integration_gateway/requirements-dev.txt`:

```
pytest>=8.0
```

Run:

```bash
cd integration_gateway
.venv/bin/pip install -r requirements-dev.txt
```

- [ ] **Step 2: Create test scaffolding (conftest.py + Makefile target)**

Create `integration_gateway/conftest.py`:

```python
# Empty conftest at the project root: pytest inserts this directory into
# sys.path so that `import app` resolves when running tests from here.
```

Append to `integration_gateway/Makefile`:

```make
# Run the test suite
.PHONY: test
test:
	.venv/bin/pytest tests -v
```

(Note: the recipe line must be indented with a TAB.)

- [ ] **Step 3: Write the failing unit tests**

Create `integration_gateway/tests/test_email_notifier.py`:

```python
"""Unit tests for the ingestion email notifier (smtplib is faked)."""
import pytest

from app.config import settings
from app.services import email_notifier
from app.services.email_notifier import notify_sales_reps, parse_recipients


class FakeSMTP:
    """Records smtplib calls; raises on construction when host == 'unreachable'."""

    instances = []

    def __init__(self, host, port, timeout=None):
        if host == "unreachable":
            raise ConnectionRefusedError("refused")
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tls = False
        self.logged_in = None
        self.sent = None
        FakeSMTP.instances.append(self)

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def sendmail(self, from_addr, to_addrs, msg):
        self.sent = (from_addr, to_addrs, msg)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def fake_smtp_and_config(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(email_notifier.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(settings, "SALES_NOTIFY_EMAILS", "rep1@x.com, rep2@y.com")
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(settings, "SMTP_PORT", 587)
    monkeypatch.setattr(settings, "SMTP_USER", "user")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(settings, "SMTP_FROM", "gw@x.com")
    monkeypatch.setattr(settings, "SMTP_USE_TLS", True)


def test_parse_recipients_splits_strips_and_drops_empty():
    assert parse_recipients("a@x.com, b@y.com,, ") == ["a@x.com", "b@y.com"]
    assert parse_recipients("") == []


def test_noop_when_no_recipients(monkeypatch):
    monkeypatch.setattr(settings, "SALES_NOTIFY_EMAILS", "")
    notify_sales_reps("orders.xml", ["SO0001"])
    assert FakeSMTP.instances == []


def test_sends_single_email_to_all_recipients():
    notify_sales_reps("orders.xml", ["SO0001", "SO0002"])

    assert len(FakeSMTP.instances) == 1
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.test", 587, 10)
    from_addr, to_addrs, msg = smtp.sent
    assert from_addr == "gw@x.com"
    assert to_addrs == ["rep1@x.com", "rep2@y.com"]
    assert "Subject: [Fluidefy] New sale order(s) ingested from orders.xml" in msg
    assert "SO0001" in msg and "SO0002" in msg


def test_starttls_and_login_when_configured():
    notify_sales_reps("orders.xml", ["SO0001"])
    smtp = FakeSMTP.instances[0]
    assert smtp.tls is True
    assert smtp.logged_in == ("user", "secret")


def test_no_auth_and_no_tls_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_USER", "")
    monkeypatch.setattr(settings, "SMTP_USE_TLS", False)
    notify_sales_reps("orders.xml", ["SO0001"])
    smtp = FakeSMTP.instances[0]
    assert smtp.tls is False
    assert smtp.logged_in is None


def test_smtp_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "unreachable")
    notify_sales_reps("orders.xml", ["SO0001"])  # must not raise
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `cd integration_gateway && .venv/bin/pytest tests/test_email_notifier.py -v`
Expected: FAIL / ERROR — `ImportError: cannot import name 'notify_sales_reps'` (module does not exist yet).

- [ ] **Step 5: Add the settings fields**

In `integration_gateway/app/config.py`, append inside `class Settings` (after `ODOO_SO_USER_ID`):

```python
    # --- Sales-rep email notification (sent by the worker after ingestion) ---
    # Comma-separated recipient list; empty disables the feature.
    SALES_NOTIFY_EMAILS: str = ""
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "fluidefy-gateway@localhost"
    SMTP_USE_TLS: bool = True
```

- [ ] **Step 6: Implement the notifier**

Create `integration_gateway/app/services/email_notifier.py`:

```python
"""Email notification to a fixed sales-rep list after order ingestion.

Design doc: docs/superpowers/specs/2026-08-28-sales-rep-email-notification-design.md

Best-effort by contract: notify_sales_reps never raises. The sale orders
already exist in Odoo when it is called; an email failure must never mark the
staging row as failed (a Celery retry would duplicate the orders).
"""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText

from ..config import settings

logger = logging.getLogger(__name__)


def parse_recipients(raw: str) -> list[str]:
    """Split a comma-separated address list, stripping whitespace and dropping
    empty entries: "a@x.com, b@y.com,, " -> ["a@x.com", "b@y.com"]"""
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def notify_sales_reps(file_name: str, created_order_names: list[str]) -> None:
    """Send one email per ingested file to the configured recipients.

    No-op when SALES_NOTIFY_EMAILS is empty. Never raises: SMTP failures are
    logged and swallowed so the ingestion outcome is unaffected.
    """
    recipients = parse_recipients(settings.SALES_NOTIFY_EMAILS)
    if not recipients:
        logger.info(
            "SALES_NOTIFY_EMAILS is empty; skipping ingestion notification for %s",
            file_name,
        )
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body_lines = [f"Ingested at {timestamp} from file: {file_name}", ""]
    body_lines += [f"  - {name}" for name in created_order_names]
    body_lines += ["", f"{len(created_order_names)} sale order(s) created in Odoo."]

    msg = MIMEText("\n".join(body_lines), "plain", "utf-8")
    msg["Subject"] = f"[Fluidefy] New sale order(s) ingested from {file_name}"
    msg["From"] = settings.SMTP_FROM
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
            if settings.SMTP_USE_TLS:
                server.starttls()
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(settings.SMTP_FROM, recipients, msg.as_string())
        logger.info("Ingestion notification sent for %s to %s", file_name, recipients)
    except Exception:
        logger.exception("Failed to send ingestion notification for %s", file_name)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd integration_gateway && .venv/bin/pytest tests/test_email_notifier.py -v`
Expected: 6 passed.

- [ ] **Step 8: Document the new variables in `.env.example`**

Append to `integration_gateway/.env.example`:

```
# --- Sales-rep email notification (worker sends after successful ingestion) ---
# Comma-separated list of recipients. Leave empty to disable notifications.
SALES_NOTIFY_EMAILS=
# SMTP relay used to send the notification emails.
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
# Envelope/header From address.
SMTP_FROM=fluidefy-gateway@localhost
# Issue STARTTLS before authentication/sending.
SMTP_USE_TLS=true
```

- [ ] **Step 9: Commit**

```bash
git add integration_gateway/requirements-dev.txt integration_gateway/conftest.py \
  integration_gateway/Makefile integration_gateway/app/config.py \
  integration_gateway/.env.example integration_gateway/app/services/email_notifier.py \
  integration_gateway/tests/test_email_notifier.py
git commit -m "feat(gateway): add best-effort email notifier for order ingestion"
```

---

### Task 2: Worker hook + integration tests

**Files:**
- Create: `integration_gateway/tests/fixtures/sample_order.xml` (copied from the real sample at the repo root)
- Create: `integration_gateway/tests/test_sync_notification.py`
- Modify: `integration_gateway/app/workers/tasks.py` (one import + hook after the success `db.commit()`)

**Interfaces:**
- Consumes (from Task 1): `notify_sales_reps(file_name: str, created_order_names: list[str]) -> None`, imported into `app.workers.tasks` as `notify_sales_reps`.
- Consumes (existing): `odoo_client.API.read_object(model, id_)` returns the raw `search_read` result — a **list of dicts**.
- Produces: no new public interface; the task's externally visible behavior is unchanged except for the email side effect.

- [ ] **Step 1: Add the real-world fixture**

Run from the repository root:

```bash
mkdir -p integration_gateway/tests/fixtures
cp ORDERS-136288813-1265376524.xml integration_gateway/tests/fixtures/sample_order.xml
```

This file contains exactly **1** `<ORDER>` (Fluidefy number `551463`, 8 lines), so the expected notification carries exactly one sale order reference.

- [ ] **Step 2: Write the failing integration tests**

Create `integration_gateway/tests/test_sync_notification.py`:

```python
"""Integration tests: the worker hook sends the notification email.

PostgreSQL (SessionLocal), the Odoo API and the partner/product resolvers are
faked; only the XML parsing and mapping code run for real, against the
committed fixture (1 order -> 1 created sale.order -> id 42 -> name SO0042).
"""
import base64
from pathlib import Path
from types import SimpleNamespace

import app.workers.tasks as tasks

FIXTURE = base64.b64encode(
    (Path(__file__).parent / "fixtures" / "sample_order.xml").read_bytes()
).decode()


class FakeAPI:
    """Stands in for odoo_client.API: create_ hands out ids; read_object mimics
    search_read returning [{'name': ...}, ...]."""

    def __init__(self):
        self.next_id = 41

    def create_(self, model, data):
        self.next_id += 1
        return self.next_id

    def read_object(self, model, id_):
        return [{"id": id_, "name": f"SO00{id_}"}]


class FakeDB:
    """Minimal stand-in for the SQLAlchemy session used by the task."""

    def __init__(self, record):
        self.record = record

    def query(self, model):
        return self

    def filter(self, *args):
        return self

    def first(self):
        return self.record

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def make_record():
    return SimpleNamespace(
        id=1,
        file_name="sample_order.xml",
        file_hash="x",
        file_base64=FIXTURE,
        processing_status="pending",
        error_log=None,
    )


def setup_task(monkeypatch, record):
    """Wire the task to fakes; returns the fake DB for assertions."""
    fake_db = FakeDB(record)
    monkeypatch.setattr(tasks, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(tasks.odoo_client, "API", lambda settings_: FakeAPI())
    monkeypatch.setattr(tasks, "resolve_partner_id", lambda api, code: 1)
    monkeypatch.setattr(
        tasks, "resolve_order_line_product_ids", lambda api, parsed: None
    )
    return fake_db


def test_success_sends_notification_and_marks_integrated(monkeypatch):
    record = make_record()
    setup_task(monkeypatch, record)
    calls = []
    monkeypatch.setattr(
        tasks, "notify_sales_reps", lambda file_name, names: calls.append((file_name, names))
    )

    result = tasks.sync_pending_order_to_odoo(1)

    assert result is True
    assert record.processing_status == "integrated"
    assert calls == [("sample_order.xml", ["SO0042"])]


def test_notification_failure_does_not_fail_task(monkeypatch):
    record = make_record()
    setup_task(monkeypatch, record)

    def boom(file_name, names):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(tasks, "notify_sales_reps", boom)

    result = tasks.sync_pending_order_to_odoo(1)

    assert result is True
    assert record.processing_status == "integrated"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd integration_gateway && .venv/bin/pytest tests/test_sync_notification.py -v`
Expected: FAIL — `AttributeError: <module 'app.workers.tasks'> does not have the attribute 'notify_sales_reps'` (monkeypatching the not-yet-imported name).

- [ ] **Step 4: Implement the hook**

In `integration_gateway/app/workers/tasks.py`, add to the imports (with the other service imports):

```python
from ..services.email_notifier import notify_sales_reps
```

Then, inside `sync_pending_order_to_odoo`, immediately after the success block (`order_record.processing_status = "integrated"` ... `db.commit()` and its `print`), and **before** `return True`, insert:

```python
        # 4b. Best-effort sales-rep notification (see design doc). The orders
        # already exist in Odoo: an email failure must never mark this row
        # as failed (a retry would duplicate the orders).
        order_names = []
        for odoo_order_id in created_ids:
            try:
                result = api.read_object("sale.order", odoo_order_id)
                name = result[0].get("name") if result else None
                order_names.append(name if name else str(odoo_order_id))
            except Exception as read_err:
                print(
                    f"WARNING: could not read sale.order {odoo_order_id} name: {str(read_err)}"
                )
                order_names.append(str(odoo_order_id))
        try:
            notify_sales_reps(order_record.file_name, order_names)
        except Exception as notify_err:
            # notify_sales_reps is contractually non-raising; belt and braces.
            print(
                f"WARNING: sales notification failed for staging order "
                f"{staging_order_id}: {str(notify_err)}"
            )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd integration_gateway && .venv/bin/pytest tests/test_sync_notification.py -v`
Expected: 2 passed.

- [ ] **Step 6: Run the full suite**

Run: `cd integration_gateway && make test`
Expected: 8 passed (6 unit + 2 integration).

- [ ] **Step 7: Commit**

```bash
git add integration_gateway/app/workers/tasks.py \
  integration_gateway/tests/test_sync_notification.py \
  integration_gateway/tests/fixtures/sample_order.xml
git commit -m "feat(gateway): notify sales reps by email after successful order sync"
```

---

## After execution (manual, needs real credentials — documented in the spec)

1. Add `SALES_NOTIFY_EMAILS`, `SMTP_HOST/PORT/USER/PASSWORD/FROM/USE_TLS` to the untracked `integration_gateway/.env`.
2. `docker compose up -d` — containers re-read `.env` via `env_file`; **no image rebuild**.
3. Drop a test XML into Fluidefy's `input/sisre2fluide/` and check the worker logs for `Ingestion notification sent`.