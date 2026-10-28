"""Tests for the manual retry feature on the monitor page.

Retry is human-triggered only: no Celery autoretry, no periodic sweeper. A
failed row is put back to ``pending`` and the existing sync task is dispatched
again. Because the sync task creates one Odoo ``sale.order`` per incoming order,
a retry must first check whether that order already exists in Odoo -- otherwise
a failure mid-loop would duplicate what was created before the error.
"""
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.api import routes
from app.services.fluidefy_mapper import OrderLine, ParsedOrder
from app.workers import tasks


class FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row


class FakeSession:
    def __init__(self, row):
        self.row = row
        self.commits = 0

    def query(self, *_args, **_kwargs):
        return FakeQuery(self.row)

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def failed_row(**overrides):
    row = SimpleNamespace(
        id=1,
        file_name="order.xml",
        date_of_arrival=datetime(2026, 1, 2, 3, 4, 5),
        processing_status="failed",
        error_log="API Synchronization exception: boom",
        file_base64="PFRFU1Q+",
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class FakeApi:
    """Records calls so tests can assert what Odoo was asked to do."""

    def __init__(self, existing=None):
        self.existing = existing or []
        self.lookups = []
        self.created = []

    def extract_from_odoo(self, model, domain, fields, more_context=None):
        self.lookups.append((model, domain, fields))
        return self.existing

    def create_(self, model, data):
        self.created.append((model, data))
        return 42


def parsed_order(order_number="CDE-2026-0001"):
    return ParsedOrder(
        order_number=order_number,
        buyer_code="3760045227820",
        buyer_name="Buyer",
        buyer_address="1 rue",
        buyer_city="Saint-Denis",
        buyer_zip="97400",
        buyer_country="RE",
        delivery_date="2026-01-10",
        lines=[
            OrderLine(
                line_no="1",
                product_code="PROD-1",
                buyer_code="BUY-1",
                supplier_code="SUP-1",
                product_name="Product 1",
                quantity=2.0,
                uom="PCE",
                price_unit=10.0,
            )
        ],
    )


class RetryEndpointTests(unittest.TestCase):
    def test_failed_order_is_requeued_and_dispatched_once(self):
        row = failed_row()
        session = FakeSession(row)
        task = MagicMock()
        with patch.object(routes, "SessionLocal", lambda: session), patch.object(
            routes, "sync_pending_order_to_odoo", task
        ):
            response = routes.retry_order(1)

        self.assertEqual(row.processing_status, "pending")
        self.assertEqual(session.commits, 1)
        task.delay.assert_called_once_with(1)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], routes.router.url_path_for("monitor_orders"))

    def test_retry_keeps_the_previous_error_as_history(self):
        row = failed_row()
        with patch.object(routes, "SessionLocal", lambda: FakeSession(row)), patch.object(
            routes, "sync_pending_order_to_odoo", MagicMock()
        ):
            routes.retry_order(1)

        self.assertIn("[retry requested", row.error_log)
        self.assertIn("boom", row.error_log)

    def test_unknown_order_returns_404(self):
        with patch.object(routes, "SessionLocal", lambda: FakeSession(None)), patch.object(
            routes, "sync_pending_order_to_odoo", MagicMock()
        ):
            with self.assertRaises(HTTPException) as caught:
                routes.retry_order(999)
        self.assertEqual(caught.exception.status_code, 404)

    def test_only_failed_orders_can_be_retried(self):
        for status in ("pending", "integrated"):
            with self.subTest(status=status):
                row = failed_row(processing_status=status)
                session = FakeSession(row)
                task = MagicMock()
                with patch.object(routes, "SessionLocal", lambda: session), patch.object(
                    routes, "sync_pending_order_to_odoo", task
                ):
                    with self.assertRaises(HTTPException) as caught:
                        routes.retry_order(1)

                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(row.processing_status, status)
                self.assertEqual(session.commits, 0)
                task.delay.assert_not_called()

    def test_order_without_payload_is_refused(self):
        row = failed_row(file_base64="")
        with patch.object(routes, "SessionLocal", lambda: FakeSession(row)), patch.object(
            routes, "sync_pending_order_to_odoo", MagicMock()
        ):
            with self.assertRaises(HTTPException) as caught:
                routes.retry_order(1)
        self.assertEqual(caught.exception.status_code, 409)

    def test_dispatch_failure_rolls_the_status_back_to_failed(self):
        row = failed_row()
        session = FakeSession(row)
        task = MagicMock()
        task.delay.side_effect = RuntimeError("broker down")
        with patch.object(routes, "SessionLocal", lambda: session), patch.object(
            routes, "sync_pending_order_to_odoo", task
        ):
            with self.assertRaises(HTTPException) as caught:
                routes.retry_order(1)

        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(row.processing_status, "failed")
        self.assertIn("broker down", row.error_log)


class MonitorRetryButtonTests(unittest.TestCase):
    def test_only_failed_rows_show_a_retry_button(self):
        failed = failed_row(id=7, processing_status="failed")
        integrated = failed_row(id=8, processing_status="integrated")
        html = routes._render_table([failed, integrated])

        self.assertIn(routes.router.url_path_for("retry_order", order_id=7), html)
        self.assertNotIn(routes.router.url_path_for("retry_order", order_id=8), html)
        self.assertEqual(html.count("<form"), 1)

    def test_retry_button_posts_to_the_retry_route(self):
        html = routes._render_table([failed_row(id=7)])
        retry_url = routes.router.url_path_for("retry_order", order_id=7)
        self.assertIn(f'action="{retry_url}"', html)
        self.assertIn('method="post"', html)


class OdooDeduplicationTests(unittest.TestCase):
    def test_no_lookup_without_an_order_number(self):
        api = FakeApi()
        self.assertIsNone(tasks.find_existing_sale_order(api, ""))
        self.assertEqual(api.lookups, [])

    def test_lookup_uses_the_fluidefy_order_number(self):
        api = FakeApi(existing=[{"id": 7, "name": "S0007"}])
        found = tasks.find_existing_sale_order(api, "CDE-2026-0001")

        self.assertEqual(found, {"id": 7, "name": "S0007"})
        model, domain, _fields = api.lookups[0]
        self.assertEqual(model, "sale.order")
        self.assertEqual(domain, [("client_order_ref", "=", "CDE-2026-0001")])

    def test_missing_order_returns_none(self):
        self.assertIsNone(tasks.find_existing_sale_order(FakeApi(), "CDE-2026-0001"))


class CreateSaleOrderTests(unittest.TestCase):
    def test_existing_order_is_skipped_and_not_created_again(self):
        api = FakeApi(existing=[{"id": 7, "name": "S0007"}])
        with patch.object(tasks, "resolve_partner_id", lambda _api, _code: 99):
            created_id, existing = tasks.create_sale_order(api, parsed_order())

        self.assertIsNone(created_id)
        self.assertEqual(existing, {"id": 7, "name": "S0007"})
        self.assertEqual(api.created, [])

    def test_new_order_is_created_with_the_fluidefy_reference(self):
        api = FakeApi()
        with patch.object(tasks, "resolve_partner_id", lambda _api, _code: 99), patch.object(
            tasks, "resolve_order_line_product_ids", lambda _api, _parsed: None
        ):
            created_id, existing = tasks.create_sale_order(api, parsed_order())

        self.assertEqual(created_id, 42)
        self.assertIsNone(existing)
        model, payload = api.created[0]
        self.assertEqual(model, "sale.order")
        self.assertEqual(payload["client_order_ref"], "CDE-2026-0001")
        self.assertEqual(payload["partner_id"], 99)


class SyncSummaryTests(unittest.TestCase):
    def test_summary_reports_created_and_skipped_orders(self):
        message = tasks.sync_summary([12, 13], ["S0007"])
        self.assertIn("[12, 13]", message)
        self.assertIn("S0007", message)

    def test_summary_handles_an_all_skipped_retry(self):
        message = tasks.sync_summary([], ["S0007"])
        self.assertIn("S0007", message)
        self.assertNotIn("none", message.lower())


if __name__ == "__main__":
    unittest.main()
