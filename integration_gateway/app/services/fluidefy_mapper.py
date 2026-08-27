"""Map parsed Fluidefy Orders XML into Odoo ``sale.order`` payloads.

The generated xsdata dataclasses wrap every leaf element in a class exposing a
``.value`` attribute (e.g. ``dlin.dlin_code_produit.value``). This module
flattens those into plain Python structures and produces the dict payload that
``odoo_client.API.create_('sale.order', ...)`` expects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..fluidefy_models.orders.fluide_orders import Dlin, Orders


@dataclass
class OrderLine:
    """A single order line extracted from a DLIN record."""

    line_no: str
    # Supplier product code (DLIN_CODE_PRODUIT)
    product_code: str
    # Buyer's internal product code (DLIN_CODE_INTERNE_ACHETEUR)
    buyer_code: str
    # Supplier's internal product code (DLIN_CODE_INTERNE_FOURNISSEUR)
    supplier_code: str
    product_name: str
    quantity: float
    uom: str
    price_unit: float
    # Resolved Odoo product.product id (set by product_resolver at sync time)
    product_id: Optional[int] = None


@dataclass
class ParsedOrder:
    """Flattened, business-friendly representation of one ORDER."""

    order_number: str
    buyer_code: str
    buyer_name: str
    buyer_address: str
    buyer_city: str
    buyer_zip: str
    buyer_country: str
    # Earliest requested delivery date (HDDT_DATE_HEURE_LIVRAISON_PLUS_TOT),
    # formatted as YYYY-MM-DD. Used in the trailing order note line.
    delivery_date: str = ""
    lines: list[OrderLine] = field(default_factory=list)


def _val(obj) -> Optional[str]:
    """Return the ``.value`` of a generated leaf class, or None."""
    return obj.value if obj is not None else None


def _to_float(value: Optional[str], default: float = 0.0) -> float:
    if not value:
        return default
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return default


def _format_delivery_date(raw: Optional[str]) -> str:
    """Convert ``HDDT_DATE_HEURE_LIVRAISON_PLUS_TOT`` (YYYYMMDDHHMM) to YYYY-MM-DD."""
    if not raw:
        return ""
    date_part = raw[:8]
    if len(date_part) != 8 or not date_part.isdigit():
        return raw  # unexpected format: fall back to the raw value
    return f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]}"


def _extract_line(dlin: Dlin) -> OrderLine:
    return OrderLine(
        line_no=_val(dlin.dlin_no_ligne) or "",
        product_code=_val(dlin.dlin_code_produit) or "",
        buyer_code=_val(dlin.dlin_code_interne_acheteur) or "",
        supplier_code=_val(dlin.dlin_code_interne_fournisseur) or "",
        product_name=_val(dlin.dlin_libelle_produit_1)
        or _val(dlin.dlin_libelle_produit_2)
        or "",
        quantity=_to_float(_val(dlin.dlin_qte_commandee)),
        uom=_val(dlin.dlin_um_qte_cdee) or "Unit(s)",
        price_unit=_to_float(_val(dlin.dlin_prix_net)),
    )


def parse_orders(orders: Orders) -> list[ParsedOrder]:
    """Convert a parsed ``ORDERS`` document into a list of ParsedOrder."""
    result: list[ParsedOrder] = []
    for order in orders.order:
        entete = order.entete
        hnby = entete.hnby
        parsed = ParsedOrder(
            order_number=_val(entete.hgen.hgen_numero_message) or "",
            buyer_code=_val(hnby.hnby_code_acheteur) or "",
            buyer_name=_val(hnby.hnby_raison_sociale_1_acheteur) or "",
            buyer_address=" ".join(
                filter(
                    None,
                    [
                        _val(hnby.hnby_adresse_1_acheteur),
                        _val(hnby.hnby_adresse_2_acheteur),
                        _val(hnby.hnby_adresse_3_acheteur),
                        _val(hnby.hnby_adresse_4_acheteur),
                    ],
                )
            ),
            buyer_city=_val(hnby.hnby_ville_acheteur) or "",
            buyer_zip=_val(hnby.hnby_code_postal_acheteur) or "",
            buyer_country=_val(hnby.hnby_pays_acheteur) or "",
            delivery_date=_format_delivery_date(
                _val(entete.hddt.hddt_date_heure_livraison_plus_tot)
            ),
        )
        parsed.lines = [_extract_line(ligne.dlin) for ligne in order.lignes.ligne]
        result.append(parsed)
    return result


def to_odoo_sale_order(parsed: ParsedOrder) -> dict:
    """Build the Odoo ``sale.order`` create payload for a ParsedOrder.

    ``partner_id`` is left as a sentinel (``None``) so the caller can resolve or
    create the partner via the Odoo API before creating the order.
    """
    order_line = [
        {
            # Resolved Odoo product id if available, otherwise the raw code
            # (caller is expected to resolve via product_resolver beforehand).
            "product_id": line.product_id or line.product_code,
            "name": line.product_name,
            "product_uom_qty": line.quantity,
        }
        for line in parsed.lines
    ]

    # Append a note line as the LAST line, carrying the order number and the
    # earliest requested delivery date.
    order_line.append(
        {
            "display_type": "line_note",
            "name": f"N. CDE {parsed.order_number} - LIVRAISON LE {parsed.delivery_date}",
        }
    )

    return {
        "partner_id": None,  # resolved by caller
        "client_order_ref": parsed.order_number,
        "origin": "Fluidefy Integration Pipeline",
        "order_line": [(0, 0, line) for line in order_line],
    }
