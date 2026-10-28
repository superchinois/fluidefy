import html

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from ..schemas.order import OrderNotification, TaskAcceptedResponse
from ..workers.tasks import celery_app, sync_pending_order_to_odoo
from ..database import SessionLocal
from ..models.staging import StagingOrder

router = APIRouter(prefix="/api/v1")

# Arrival times are stored as naive UTC; display them in Reunion Island time.
_REUNION_TZ = ZoneInfo("Indian/Reunion")


def _format_arrival(dt) -> str:
    """Format a naive-UTC arrival datetime as a Reunion Island local time string."""
    if dt is None:
        return ""
    local = dt.replace(tzinfo=timezone.utc).astimezone(_REUNION_TZ)
    return local.strftime("%Y-%m-%d %H:%M:%S")


def _now_local() -> str:
    """Current Reunion Island local time, stamped into retry notes."""
    return datetime.now(timezone.utc).astimezone(_REUNION_TZ).strftime("%Y-%m-%d %H:%M:%S")


# CSS shared by the monitor page.
_STATUS_COLORS = {
    "pending": "#f0ad4e",    # amber
    "integrated": "#5cb85c", # green
    "failed": "#d9534f",     # red
}


def _status_badge(status: str) -> str:
    color = _STATUS_COLORS.get(status, "#777")
    escaped = status.replace("&", "&amp;").replace("<", "&lt;")
    return f'<span style="color:{color};font-weight:bold;">{escaped}</span>'


def _retry_cell(row) -> str:
    """Render the row's action cell: a retry button only for failed orders."""
    if row.processing_status != "failed":
        return "&mdash;"
    action = router.url_path_for("retry_order", order_id=row.id)
    return (
        f'<form method="post" action="{action}"'
        f' onsubmit="return confirm(\'Retry order {row.id}?\');">'
        f'<button type="submit">Retry</button></form>'
    )


def _render_table(rows) -> str:
    if not rows:
        body = '<tr><td colspan="6" style="text-align:center;">No order files processed yet.</td></tr>'
    else:
        body = "\n".join(
            f"<tr><td>{r.id}</td><td>{r.file_name}</td>"
            f"<td>{_format_arrival(r.date_of_arrival)}</td><td>{_status_badge(r.processing_status)}</td>"
            f"<td class=\"error\">{r.error_log or ''}</td><td>{_retry_cell(r)}</td></tr>"
            for r in rows
        )
    return body


def _monitor_page(rows, refresh_url: str) -> HTMLResponse:
    """Render the monitor page.

    ``refresh_url`` is the path the page was requested on, so the Refresh link
    always targets the real route (which lives under the API prefix) instead of
    a hardcoded path that may not exist.
    """
    body = _render_table(rows)
    refresh_href = html.escape(refresh_url, quote=True)
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Fluidefy → Odoo Task Monitor</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; }}
    h1 {{ font-size: 1.4rem; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ text-align: left; padding: 8px 12px; border: 1px solid #ddd; }}
    th {{ background: #f5f5f5; }}
    tr:nth-child(even) {{ background: #fafafa; }}
    .error {{ color: #d9534f; font-size: 0.85rem; }}
  </style>
</head>
<body>
  <h1>Fluidefy → Odoo Task Monitor</h1>
  <p><a href="{refresh_href}">Refresh</a> · Times shown in <strong>Indian/Reunion</strong> (UTC+4)</p>
  <table>
    <thead>
      <tr><th>ID</th><th>File name</th><th>Arrival time</th><th>Status</th><th>Error log</th><th>Action</th></tr>
    </thead>
    <tbody>
{body}
    </tbody>
  </table>
</body>
</html>"""
    return HTMLResponse(content=page)


@router.get(
    "/monitor",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def monitor_orders(request: Request):
    """Render a simple HTML page listing the state of all staged orders."""
    db = SessionLocal()
    try:
        rows = (
            db.query(StagingOrder)
            .order_by(StagingOrder.date_of_arrival.desc())
            .all()
        )
        return _monitor_page(rows, request.url.path)
    finally:
        db.close()


@router.post(
    "/orders/{order_id}/retry",
    include_in_schema=False,
)
def retry_order(order_id: int):
    """Re-queue one failed order and hand it to the Odoo sync task again.

    Manual and human-triggered only: nothing retries automatically. The payload
    stored in the staging row is reused, so no file re-delivery is needed, and
    the sync task skips any sale.order already present in Odoo, which keeps
    repeated clicks from duplicating orders.
    """
    db = SessionLocal()
    try:
        order = db.query(StagingOrder).filter(StagingOrder.id == order_id).first()
        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Staged order {order_id} not found.",
            )
        if order.processing_status != "failed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Order {order_id} is '{order.processing_status}'; "
                    "only failed orders can be retried."
                ),
            )
        if not order.file_base64:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Order {order_id} has no stored payload to retry.",
            )

        # Keep the failure reason for the audit trail, prefixed with this retry.
        previous_error = order.error_log or ""
        order.processing_status = "pending"
        order.error_log = f"[retry requested {_now_local()}] {previous_error}".strip()
        db.commit()

        try:
            sync_pending_order_to_odoo.delay(order_id)
        except Exception as exc:
            # Never leave the row 'pending' without a task actually queued.
            order.processing_status = "failed"
            order.error_log = f"Retry dispatch to the Celery broker failed: {exc}"
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Could not queue the retry: {exc}",
            )

        return RedirectResponse(
            router.url_path_for("monitor_orders"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    finally:
        db.close()


@router.post(
    "/orders/notify",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TaskAcceptedResponse,
)
async def accept_order_notification(payload: OrderNotification):
    try:
        # Push heavy work asynchronously over to celery architecture safely
        task = celery_app.send_task(
            "tasks.process_inbound_xml",
            args=[payload.file_name, payload.file_content_base64],
        )
        return TaskAcceptedResponse(
            status="accepted",
            message="File delivery parsing request handed over successfully to Celery Worker pipeline.",
            task_id=task.id,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit background workers task successfully to message brokers: {str(e)}",
        )
