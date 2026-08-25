"""Resolve Odoo ``product.product`` ids from the product identifiers present
in a Fluidefy order line (``DLIN`` record).

Instead of calling ``odoo_client.API.extract_from_odoo`` once per identifier
per line, this module uses the ``odoo_client.Cache`` object, which preloads
all products from Odoo in a single bulk call and only refreshes it when it
becomes stale (default 1 hour). A shared, module-level resolver is reused
across Celery task invocations so the bulk load happens at most once per hour
per worker process.

A single Fluidefy line may carry several product codes:
  * ``DLIN_CODE_PRODUIT``            -> :attr:`OrderLine.product_code`
  * ``DLIN_CODE_INTERNE_FOURNISSEUR`` -> :attr:`OrderLine.supplier_code`
  * ``DLIN_CODE_INTERNE_ACHETEUR``    -> :attr:`OrderLine.buyer_code`

These may correspond to either an Odoo internal reference (``default_code``)
or a GTIN/EAN barcode (``barcode``), so both fields are tried for every code,
in priority order, stopping at the first match. If no product matches, a
:class:`ProductNotFoundError` is raised (unless ``create_if_missing``).
"""
from __future__ import annotations

from odoo_client import Cache

from .fluidefy_mapper import OrderLine

# Product fields to look up, in priority order. ``default_code`` (internal
# reference) is preferred over ``barcode`` for short supplier codes, but a
# GTAN (e.g. 2040514138552) is most reliably stored in ``barcode``.
_FIELDS = ("default_code", "barcode")


class ProductNotFoundError(Exception):
    """Raised when an order line's product cannot be found in Odoo."""


def _product_code(line: OrderLine) -> str:
    """Best available identifier to persist on a newly created product."""
    return line.product_code or line.supplier_code or line.buyer_code or ""


class ProductResolver:
    """Resolves Odoo product ids backed by an ``odoo_client.Cache``."""

    def __init__(self, api):
        self._api = api
        self._cache = Cache(api)
        self._index: dict[tuple[str, str], int] = {}
        self._indexed_at = None

    def _variant_id(self, item: dict) -> int:
        """Return a usable ``product.product`` id for a cached template item."""
        variants = item.get("product_variant_ids") or []
        if variants:
            return variants[0]
        return item["id"]

    def _build_index(self) -> None:
        """Index cached products by (field, code) -> product id."""
        index: dict[tuple[str, str], int] = {}
        for item in self._cache.items.values():
            product_id = self._variant_id(item)
            for field in _FIELDS:
                code = item.get(field)
                if code:
                    index[(field, code)] = product_id
        self._index = index

    def _ensure_fresh(self) -> None:
        """Refresh the cache (no-op if fresh) and rebuild the index if needed."""
        self._cache.reset_cache()
        if self._indexed_at is None or self._indexed_at < self._cache._last_updated:
            self._build_index()
            self._indexed_at = self._cache._last_updated

    def resolve(self, line: OrderLine, create_if_missing: bool = False) -> int:
        """Return the Odoo ``product.product`` id matching ``line``.

        Args:
            line: A parsed order line.
            create_if_missing: Whether to create the product when not found.

        Returns:
            The Odoo ``product.product`` id.

        Raises:
            ProductNotFoundError: If no matching product is found and
                ``create_if_missing`` is False.
        """
        self._ensure_fresh()

        for code in (line.product_code, line.supplier_code, line.buyer_code):
            if not code:
                continue
            for field in _FIELDS:
                product_id = self._index.get((field, code))
                if product_id:
                    return product_id

        if create_if_missing:
            return self._api.create_(
                "product.product",
                {
                    "default_code": _product_code(line),
                    "name": line.product_name or _product_code(line),
                    "type": "consu",
                },
            )

        raise ProductNotFoundError(
            f"Product not found in Odoo for line {line.line_no}: "
            f"code={line.product_code!r}, supplier_code={line.supplier_code!r}, "
            f"buyer_code={line.buyer_code!r} ({line.product_name})"
        )

    def resolve_lines(self, parsed) -> None:
        """Resolve product ids for every line of a parsed order, in place.

        Raises ProductNotFoundError on the first line whose product does not
        exist in Odoo (propagated by the caller to fail the whole order).
        """
        for line in parsed.lines:
            line.product_id = self.resolve(line)


# Shared resolver reused across Celery task invocations so the Cache bulk load
# happens only once per worker process (and refreshes at most hourly).
_resolver: ProductResolver | None = None


def get_resolver(api) -> ProductResolver:
    """Return the module-level shared resolver, creating it on first use."""
    global _resolver
    if _resolver is None:
        _resolver = ProductResolver(api)
    return _resolver


def resolve_order_line_product_ids(api, parsed) -> None:
    """Resolve product ids for every line of a parsed order, in place.

    Uses the shared Cache-backed resolver.
    """
    get_resolver(api).resolve_lines(parsed)
