"""Resolve Odoo ``res.partner`` ids from the buyer identifiers present in a
Fluidefy order (``HNBY`` record).

Uses the ``odoo_client.Cache`` object, which preloads all partners from Odoo
in a single bulk call (including the ``barcode`` field) and only refreshes
when stale. A shared, module-level resolver is reused across Celery task
invocations so the bulk load happens at most once per hour per worker process.

The buyer is matched by the ``barcode`` field of ``res.partner``, which
carries the GLN (e.g. ``3760045227820``) from the Fluidefy
``HNBY_CODE_ACHETEUR``.
"""
from __future__ import annotations

from odoo_client import Cache


class PartnerNotFoundError(Exception):
    """Raised when a buyer partner cannot be found in Odoo."""


class PartnerResolver:
    """Resolves Odoo partner ids backed by an ``odoo_client.Cache``."""

    def __init__(self, api):
        self._api = api
        self._cache = Cache(api)
        self._index: dict[str, int] = {}
        self._indexed_at = None

    def _build_index(self) -> None:
        """Index cached partners by ``barcode`` -> partner id."""
        index: dict[str, int] = {}
        for partner in self._cache.partners.values():
            barcode = partner.get("barcode")
            if barcode:
                index[barcode] = partner["id"]
        self._index = index

    def _ensure_fresh(self) -> None:
        """Refresh the cache (no-op if fresh) and rebuild the index if needed."""
        self._cache.reset_cache()
        if self._indexed_at is None or self._indexed_at < self._cache._last_updated:
            self._build_index()
            self._indexed_at = self._cache._last_updated

    def resolve(self, buyer_code: str) -> int:
        """Return the Odoo ``res.partner`` id whose barcode matches ``buyer_code``.

        Args:
            buyer_code: The buyer GLN / barcode from the Fluidefy HNBY record.

        Returns:
            The Odoo ``res.partner`` id.

        Raises:
            PartnerNotFoundError: If no partner with a matching barcode exists.
        """
        self._ensure_fresh()
        partner_id = self._index.get(buyer_code)
        if partner_id is None:
            raise PartnerNotFoundError(
                f"Customer not found in Odoo by barcode: {buyer_code!r}"
            )
        return partner_id


# Shared resolver reused across Celery task invocations so the Cache bulk load
# happens only once per worker process (and refreshes at most hourly).
_resolver: PartnerResolver | None = None


def get_partner_resolver(api) -> PartnerResolver:
    """Return the module-level shared resolver, creating it on first use."""
    global _resolver
    if _resolver is None:
        _resolver = PartnerResolver(api)
    return _resolver


def resolve_partner_id(api, buyer_code: str) -> int:
    """Return the Odoo ``res.partner`` id for a buyer GLN/barcode.

    Uses the shared Cache-backed resolver.
    """
    return get_partner_resolver(api).resolve(buyer_code)
