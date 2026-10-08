"""Shipmozo shipping aggregator client (manual push from admin)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from app.common import format_variant_info_label, serialize_shipment, utcnow
from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Order, Shipment
from app.shipping import service as sr

logger = logging.getLogger("shipping.shipmozo")


def shipmozo_configured() -> bool:
    return bool(
        (settings.SHIPMOZO_PUBLIC_KEY or "").strip().strip("\"'")
        and (settings.SHIPMOZO_PRIVATE_KEY or "").strip().strip("\"'")
        and (settings.SHIPMOZO_WAREHOUSE_ID or "").strip().strip("\"'")
    )


def _clean(value: str | None) -> str:
    return (value or "").strip().strip("\"'")


def _headers() -> dict[str, str]:
    return {
        "public-key": _clean(settings.SHIPMOZO_PUBLIC_KEY),
        "private-key": _clean(settings.SHIPMOZO_PRIVATE_KEY),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _dig(data: Any, *keys: str) -> Any:
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", 0, "0", False, []):
            return value
    return None


def _error_detail(body: dict, resp: httpx.Response) -> str:
    data = body.get("data")
    nested_error = None
    if isinstance(data, dict):
        nested_error = data.get("error") or data.get("message")
        if isinstance(nested_error, (list, dict)):
            nested_error = str(nested_error)
    detail = (
        nested_error
        or body.get("message")
        or body.get("error")
        or body.get("msg")
        or (body.get("errors") and str(body.get("errors")))
        or (data if isinstance(data, str) else None)
        or (resp.text or "").strip()
        or "Shipmozo request failed"
    )
    if isinstance(detail, (list, dict)):
        detail = str(detail)
    detail = str(detail).strip() or "Shipmozo request failed"
    # Prefer nested validation text over generic "Error"
    if detail.lower() == "error" and nested_error:
        detail = str(nested_error).strip()
    result = body.get("result")
    if result is not None and str(result) not in detail:
        return f"Shipmozo: {detail} (result={result})"
    if not detail.lower().startswith("shipmozo"):
        return f"Shipmozo: {detail}"
    return detail


async def _api(method: str, path: str, **kwargs) -> dict:
    if not shipmozo_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Shipmozo is not configured. Set SHIPMOZO_PUBLIC_KEY, "
                "SHIPMOZO_PRIVATE_KEY, and SHIPMOZO_WAREHOUSE_ID."
            ),
        )

    base = _clean(settings.SHIPMOZO_BASE_URL) or "https://shipping-api.com/app/api/v1"
    async with httpx.AsyncClient(base_url=base.rstrip("/"), timeout=45) as client:
        resp = await client.request(method, path, headers=_headers(), **kwargs)

    body: dict = {}
    if resp.content:
        try:
            parsed = resp.json()
            body = parsed if isinstance(parsed, dict) else {"data": parsed}
        except Exception:
            body = {"raw": resp.text}

    result_flag = body.get("result")
    success = body.get("success")
    # Shipmozo often returns HTTP 200 with result="0" on auth/validation errors
    failed = (
        resp.status_code >= 400
        or result_flag in (0, "0", False, "false")
        or success in (False, "false", 0, "0")
    )
    if failed:
        logger.error(
            "Shipmozo error %s %s HTTP %s body=%s",
            method,
            path,
            resp.status_code,
            body,
        )
        raise HTTPException(status_code=502, detail=_error_detail(body, resp))

    return body


def _dims_cm(order: Order) -> tuple[float, float, float]:
    length = float(settings.SHIPMOZO_DEFAULT_LENGTH or settings.SHIPROCKET_DEFAULT_LENGTH)
    breadth = float(settings.SHIPMOZO_DEFAULT_BREADTH or settings.SHIPROCKET_DEFAULT_BREADTH)
    height = float(settings.SHIPMOZO_DEFAULT_HEIGHT or settings.SHIPROCKET_DEFAULT_HEIGHT)
    for item in order.items or []:
        info = item.variant_info if isinstance(item.variant_info, dict) else {}
        try:
            length = max(length, float(info.get("length_cm") or 0) or length)
            breadth = max(breadth, float(info.get("breadth_cm") or 0) or breadth)
            height = max(height, float(info.get("height_cm") or 0) or height)
        except (TypeError, ValueError):
            pass
    return length, breadth, height


def _build_push_payload(order: Order, weight_kg: float) -> dict:
    """Build Shipmozo push-order body per official API docs."""
    length, breadth, height = _dims_cm(order)
    weight_grams = max(int(round(float(weight_kg) * 1000)), 500)
    phone_digits = "".join(c for c in (order.customer_phone or "") if c.isdigit())[-10:]
    if len(phone_digits) != 10:
        raise HTTPException(
            status_code=400,
            detail=f"Order {order.order_id} needs a valid 10-digit phone for Shipmozo.",
        )
    phone = int(phone_digits)

    landmark = getattr(order, "address_landmark", None)
    is_cod = (order.payment_method or "").lower() == "cod"
    payment_type = "COD" if is_cod else "PREPAID"
    order_amount = float(order.total or order.subtotal or 0)

    pin_digits = "".join(c for c in str(order.address_pincode or "") if c.isdigit())[:6]
    if len(pin_digits) != 6:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Order {order.order_id} has no valid 6-digit pincode "
                f"(got {order.address_pincode!r}). Update the order address first."
            ),
        )
    pin = int(pin_digits)

    line1 = (order.address_line1 or order.address_city or "Address")[:190]
    line2 = (order.address_line2 or landmark or "")[:190]
    name = (order.customer_name or "Customer")[:100]
    city = order.address_city or ""
    state = order.address_state or ""
    if not city or not state:
        raise HTTPException(
            status_code=400,
            detail=f"Order {order.order_id} is missing city/state for Shipmozo.",
        )
    email = order.customer_email or f"order-{order.order_id.lower()}@chairdekho.in"

    products = []
    for item in order.items or []:
        label = format_variant_info_label(item.variant_info)
        base = item.name or "Item"
        products.append(
            {
                "name": (f"{base} ({label})" if label else base)[:200],
                "sku_number": str(item.product_id or item.id),
                "quantity": int(item.quantity),
                "discount": "",
                "hsn": "",
                "unit_price": float(item.price),
                "product_category": "Other",
            }
        )
    if not products:
        raise HTTPException(status_code=400, detail="Order has no items")

    return {
        "order_id": order.order_id,
        "order_date": (order.created_at or utcnow()).strftime("%Y-%m-%d"),
        "order_type": "ESSENTIALS",
        "consignee_name": name,
        "consignee_phone": phone,
        "consignee_email": email,
        "consignee_address_line_one": line1,
        "consignee_address_line_two": line2,
        "consignee_pin_code": pin,
        "consignee_city": city,
        "consignee_state": state,
        "product_detail": products,
        "payment_type": payment_type,
        "cod_amount": str(int(round(order_amount))) if is_cod else "",
        "weight": weight_grams,
        "length": int(round(length)) or 10,
        "width": int(round(breadth)) or 10,
        "height": int(round(height)) or 10,
        "warehouse_id": _clean(settings.SHIPMOZO_WAREHOUSE_ID),
        "gst_ewaybill_number": "",
        "gstin_number": "",
    }


def _extract_reference_id(api_result: dict) -> str | None:
    data = api_result.get("data") if isinstance(api_result.get("data"), dict) else {}
    ref = _first(
        api_result.get("reference_id"),
        api_result.get("ref_id"),
        data.get("reference_id") if isinstance(data, dict) else None,
        data.get("ref_id") if isinstance(data, dict) else None,
        _dig(api_result, "data", "order", "reference_id"),
    )
    return str(ref) if ref is not None else None


def _extract_awb_courier(api_result: dict) -> tuple[str | None, str | None]:
    data = api_result.get("data") if isinstance(api_result.get("data"), dict) else {}
    awb = _first(
        api_result.get("awb"),
        api_result.get("awb_number"),
        api_result.get("awb_code"),
        data.get("awb") if isinstance(data, dict) else None,
        data.get("awb_number") if isinstance(data, dict) else None,
        data.get("awb_code") if isinstance(data, dict) else None,
    )
    courier = _first(
        api_result.get("courier"),
        api_result.get("courier_name"),
        api_result.get("courier_company"),
        data.get("courier") if isinstance(data, dict) else None,
        data.get("courier_name") if isinstance(data, dict) else None,
        data.get("courier_company") if isinstance(data, dict) else None,
        data.get("courier_company_service") if isinstance(data, dict) else None,
    )
    return (
        str(awb) if awb is not None else None,
        str(courier) if courier is not None else None,
    )


async def push_order_to_shipmozo(order_id: str) -> dict:
    """Push order to Shipmozo, then auto-assign courier when enabled.

    Flow per Shipmozo docs:
      POST /push-order  → save reference_id
      POST /auto-assign-order  { order_id }  → AWB + courier (if panel auto-assign is set)
    """
    if not shipmozo_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Shipmozo is not configured. Set SHIPMOZO_PUBLIC_KEY, "
                "SHIPMOZO_PRIVATE_KEY, and SHIPMOZO_WAREHOUSE_ID."
            ),
        )

    async with AsyncSessionLocal() as db:
        order = await sr._load_order(db, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if not order.items:
            raise HTTPException(status_code=400, detail="Order has no items")

        result = await db.execute(
            select(Shipment).where(Shipment.order_id == order.order_id)
        )
        existing = result.scalar_one_or_none()

        reference_id = (
            existing.shipmozo_reference_id
            if existing and existing.shipmozo_reference_id
            else None
        )

        if not reference_id:
            weight_kg = await sr._order_weight_kg(order)
            payload = _build_push_payload(order, weight_kg)
            logger.info(
                "Shipmozo push payload for %s warehouse=%s pin=%s phone=%s city=%s",
                order.order_id,
                payload.get("warehouse_id"),
                payload.get("consignee_pin_code"),
                payload.get("consignee_phone"),
                payload.get("consignee_city"),
            )

            push_result = await _api("POST", "/push-order", json=payload)
            logger.info(
                "Shipmozo push response for %s: %s", order.order_id, push_result
            )

            reference_id = _extract_reference_id(push_result) or order.order_id
            if not existing:
                existing = Shipment(order_db_id=order.id, order_id=order.order_id)
                db.add(existing)
                await db.flush()

            existing.shipmozo_reference_id = str(reference_id)
            existing.status = "created"
            existing.updated_at = utcnow()
            awb, courier = _extract_awb_courier(push_result)
        else:
            if not existing:
                existing = Shipment(order_db_id=order.id, order_id=order.order_id)
                db.add(existing)
                await db.flush()
            awb, courier = existing.awb_code, existing.courier_name

        # Docs: auto-assign body is only { "order_id": "<website order id>" }
        if settings.SHIPMOZO_AUTO_ASSIGN and not awb:
            try:
                assign_result = await _api(
                    "POST",
                    "/auto-assign-order",
                    json={"order_id": order.order_id},
                )
                logger.info(
                    "Shipmozo auto-assign for %s: %s", order.order_id, assign_result
                )
                a2, c2 = _extract_awb_courier(assign_result)
                awb = awb or a2
                courier = courier or c2
                if awb:
                    existing.status = "awb_assigned"
            except HTTPException as exc:
                logger.warning(
                    "Shipmozo auto-assign skipped for %s: %s",
                    order.order_id,
                    exc.detail,
                )

        if awb:
            existing.awb_code = str(awb)
            if (
                not existing.tracking_url
                or "shiprocket" in (existing.tracking_url or "").lower()
            ):
                existing.tracking_url = (
                    f"https://shipping-api.com/app/api/v1/track-order"
                    f"?awb_number={awb}"
                )
            if not existing.status or existing.status == "created":
                existing.status = "awb_assigned"
        if courier:
            existing.courier_name = str(courier)

        existing.shipmozo_reference_id = str(reference_id)
        existing.updated_at = utcnow()
        await db.commit()
        await db.refresh(existing)
        logger.info(
            "Shipmozo order created for %s → ref %s / AWB %s",
            order.order_id,
            existing.shipmozo_reference_id,
            existing.awb_code,
        )
        return serialize_shipment(existing)
