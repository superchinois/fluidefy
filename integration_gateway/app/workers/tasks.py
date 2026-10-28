import hashlib
import base64
from celery import Celery
from sqlalchemy.orm import Session
from xsdata.formats.dataclass.parsers import XmlParser
from xsdata.formats.dataclass.parsers.config import ParserConfig
from ..config import settings
from ..database import engine, SessionLocal
from ..models.staging import StagingOrder
from ..services.fluidefy_mapper import parse_orders, to_odoo_sale_order
from ..services.product_resolver import resolve_order_line_product_ids
from ..services.partner_resolver import resolve_partner_id
from ..fluidefy_models.orders.fluide_orders import Orders
import odoo_client

celery_app = Celery("tasks", broker=settings.REDIS_URL, backend=settings.REDIS_URL)


def find_existing_sale_order(api, order_number: str):
    """Return the Odoo ``sale.order`` already created for a Fluidefy order number.

    Retries re-run the sync for a payload that may have been partially applied,
    so an order created before an unrelated line failed has to be detected here
    instead of being created a second time. ``client_order_ref`` carries the
    Fluidefy order number (see ``fluidefy_mapper.to_odoo_sale_order``).

    Returns the matching ``{"id": ..., "name": ...}`` dict, or None.
    """
    if not order_number:
        return None

    matches = api.extract_from_odoo(
        "sale.order",
        [("client_order_ref", "=", order_number)],
        ["name"],
    )
    return matches[0] if matches else None


def create_sale_order(api, parsed):
    """Ensure one parsed order exists in Odoo, exactly once.

    Returns ``(created_id, existing_order)`` where exactly one of the two is
    None: either the order was just created, or it was already in Odoo and was
    left untouched (which is what makes a retry safe).
    """
    existing = find_existing_sale_order(api, parsed.order_number)
    if existing:
        return None, existing

    # Resolve the buyer partner (HNBY) in Odoo by its barcode (GLN);
    # fail the order if it does not exist rather than creating it silently.
    partner_id = resolve_partner_id(api, parsed.buyer_code)

    # Resolve each order line's Odoo product.product id from the
    # identifiers carried in the XML (DLIN codes / barcode).
    resolve_order_line_product_ids(api, parsed)

    # Build the sale.order payload from the parsed order
    sale_order_payload = to_odoo_sale_order(parsed)
    sale_order_payload['partner_id'] = partner_id
    # Flag the order with the configured tag and assign the responsible
    # user (from ODOO_SO_TAGS_ID / ODOO_SO_USER_ID env vars).
    if settings.ODOO_SO_TAGS_ID:
        sale_order_payload['tag_ids'] = [(6, 0, [settings.ODOO_SO_TAGS_ID])]
    if settings.ODOO_SO_USER_ID:
        sale_order_payload['user_id'] = settings.ODOO_SO_USER_ID

    return api.create_('sale.order', sale_order_payload), None


def sync_summary(created_ids, skipped) -> str:
    """Build the operator-facing result message for one sync run."""
    parts = []
    if created_ids:
        parts.append(f"Successfully instantiated Odoo order object(s) ID: {created_ids}")
    if skipped:
        parts.append(f"Already present in Odoo, not created again: {skipped}")
    return " | ".join(parts) or "Nothing to synchronise"


@celery_app.task(name="tasks.process_inbound_xml")
def process_inbound_xml(file_name: str, file_content_base64: str) -> bool:
    """
    Handle an incoming XML order delivered by the watcher.

    The watcher sends the file contents base64-encoded (content delivery), so
    this task does not need access to the file on disk: it decodes the content,
    computes a hash, and persists it to the PostgreSQL staging table.
    """
    try:
        # Decode the delivered base64 payload back into raw file bytes
        file_bytes = base64.b64decode(file_content_base64)

        file_hash = hashlib.md5(file_bytes).hexdigest()

        db: Session = SessionLocal()
        try:
            existing = db.query(StagingOrder).filter(StagingOrder.file_hash == file_hash).first()
            if existing:
                print(f"Duplicate file contents identified. MD5 reference: {file_hash}")
                return False

            new_order = StagingOrder(
                file_name=file_name,
                file_hash=file_hash,
                file_base64=file_content_base64,
                processing_status="pending"
            )
            db.add(new_order)
            db.commit()
            
            # TRIGGER SYNC CHAIN:
            # Once saved to the database, trigger the Odoo sync task asynchronously 
            # to process this specific newly staged order.
            sync_pending_order_to_odoo.delay(new_order.id)
            
            print(f"Successfully recorded order payload row into database. Sync triggered for ID: {new_order.id}")
            return True
        except Exception as db_err:
            db.rollback()
            print(f"Database persist exception block encountered: {str(db_err)}")
            raise db_err
        finally:
            db.close()

    except Exception as exc:
        print(f"Fatal worker pipeline execution fault occurred tracking {file_name}: {str(exc)}")
        raise exc


@celery_app.task(name="tasks.sync_pending_order_to_odoo")
def sync_pending_order_to_odoo(staging_order_id: int) -> bool:
    """
    Fetches a specific pending order record from PostgreSQL, extracts the Base64 XML data,
    authenticates via Odoo External API, invokes Odoo business models to instantiate 
    a sale.order, and commits the execution status.
    """
    db: Session = SessionLocal()
    order_record = db.query(StagingOrder).filter(
        StagingOrder.id == staging_order_id,
        StagingOrder.processing_status == "pending"
    ).first()

    if not order_record:
        print(f"Order entry with ID {staging_order_id} not found or already processed.")
        db.close()
        return False

    created_ids = []
    skipped = []

    try:
        # 1. Decode the XML data payload out of base64 storage string
        raw_xml_bytes = base64.b64decode(order_record.file_base64)
        xml_content_string = raw_xml_bytes.decode("utf-8")

        # 2. Parse the XML into the generated xsdata dataclasses.
        #    Real Fluidefy files may contain elements not present in the XSD
        #    (e.g. HGEN_ICR), so ignore unknown properties instead of failing.
        parser = XmlParser(config=ParserConfig(fail_on_unknown_properties=False))
        orders = parser.from_string(xml_content_string, Orders)
        parsed_orders = parse_orders(orders)

        # 3. Establish connection to Odoo via odoo_client.API (JSON-RPC, lazy authentication)
        api = odoo_client.API(settings)

        for parsed in parsed_orders:
            # Create the order unless it is already in Odoo (makes retries safe)
            created_id, existing = create_sale_order(api, parsed)
            if created_id is not None:
                created_ids.append(created_id)
            else:
                skipped.append(existing.get("name") or existing.get("id"))

        # 4. Successful Execution Lifecycle: Update record parameters
        order_record.processing_status = "integrated"
        order_record.error_log = sync_summary(created_ids, skipped)
        db.commit()
        print(f"Sync complete: Database entry ID {staging_order_id} integrated to Odoo sale.order ID(s) {created_ids} (skipped: {skipped})")
        return True

    except Exception as exc:
        db.rollback()
        # 5. Fault Isolation Lifecycle: Safely track stack frames and log failure context to the row
        error_msg = f"API Synchronization exception: {str(exc)}"
        if created_ids or skipped:
            error_msg += f" (created before the failure: {created_ids}, already in Odoo: {skipped})"
        print(f"ERROR: {error_msg}")
        
        order_record.processing_status = "failed"
        order_record.error_log = error_msg
        db.commit()
        return False
        
    finally:
        db.close()