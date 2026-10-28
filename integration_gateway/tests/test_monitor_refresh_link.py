"""Regression tests for the monitor page "Refresh" link.

The monitor route is mounted on the router that carries the ``/api/v1``
prefix, so its real path is ``/api/v1/monitor``. The page used to hardcode
``href="/monitor"``, which 404s because no route exists at that path.

These tests read the real registered path off the router (so they keep
working if the prefix is ever changed) and assert the rendered page links
back to that exact path.
"""
import unittest
from unittest.mock import patch

from starlette.requests import Request

from app.api import routes


def registered_monitor_path() -> str:
    """Return the path of the single GET route serving the monitor page."""
    matches = [
        route.path
        for route in routes.router.routes
        if "GET" in getattr(route, "methods", set()) and route.path.endswith("/monitor")
    ]
    assert len(matches) == 1, f"expected exactly one monitor route, found: {matches}"
    return matches[0]


def request_for(path: str) -> Request:
    """Build a minimal ASGI request for ``path`` (no app or database needed)."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("testclient", 123),
        }
    )


class _FakeQuery:
    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return []


class _FakeSession:
    def query(self, *_args, **_kwargs):
        return _FakeQuery()

    def close(self):
        pass


class MonitorRefreshLinkTests(unittest.TestCase):
    def test_monitor_route_is_mounted_under_the_api_prefix(self):
        """Guards the assumption behind the bug: the page is not at /monitor."""
        path = registered_monitor_path()
        self.assertEqual(path, f"{routes.router.prefix}/monitor")

    def test_refresh_link_points_at_the_registered_monitor_path(self):
        """The Refresh link must target the path the page is served from."""
        path = registered_monitor_path()
        html = routes._monitor_page([], path).body.decode()
        self.assertIn(f'href="{path}"', html)

    def test_refresh_link_is_not_the_old_hardcoded_path(self):
        """Regression: /monitor has no route and used to 404."""
        path = registered_monitor_path()
        html = routes._monitor_page([], path).body.decode()
        self.assertNotIn('href="/monitor"', html)

    def test_handler_derives_refresh_link_from_the_incoming_request(self):
        """End of the chain: the route must feed the request path to the page."""
        path = registered_monitor_path()
        with patch.object(routes, "SessionLocal", lambda: _FakeSession()):
            html = routes.monitor_orders(request_for(path)).body.decode()
        self.assertIn(f'href="{path}"', html)


if __name__ == "__main__":
    unittest.main()
