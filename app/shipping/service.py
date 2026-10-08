import logging

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.common import format_variant_info_label, serialize_shipment, utcnow
from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Order, Shipment

logger = logging.getLogger("shipping")

_token_cache: dict = {"token": None}


def shiprocket_configured() -> bool:
    return bool(settings.SHIPROCKET_EMAIL and settings.SHIPROCKET_PASSWORD)


async def _get_token(force: bool = False) -> str:
    if _token_cache["token"] and not force:
        return _token_cache["token"]
    if not shiprocket_configured():
        raise HTTPException(status_code=503, detail="Shiprocket is not configured")
    async with httpx.AsyncClient(
        base_url=settings.SHIPROCKET_BASE_URL, timeout=30
    ) as client:
        resp = await client.post(
            "/auth/login",
            json={
                "email": settings.SHIPROCKET_EMAIL,
                "password": settings.SHIPROCKET_PASSWORD,
            },
        )
    if resp.status_code != 200:
        logger.error("Shiprocket auth failed: %s", resp.text)
        raise HTTPException(status_code=502, detail="Shiprocket authentication failed")
    token = resp.json().get("token")
    if not token:
        raise HTTPException(status_code=502, detail="Shiprocket token missing")
    _token_cache["token"] = token
    return token


async def _api(method: str, path: str, retry_auth: bool = True, **kwargs) -> dict:
    token = await _get_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        base_url=settings.SHIPROCKET_BASE_URL, timeout=45
    ) as client:
        resp = await client.request(method, path, headers=headers, **kwargs)

    if resp.status_code == 401 and retry_auth:
        _token_cache["token"] = None
        return await _api(method, path, retry_auth=False, **kwargs)

    body: dict = {}
    if resp.content:
        try:
            parsed = resp.json()
            body = parsed if isinstance(parsed, dict) else {"data": parsed}
        except Exception:
            body = {"raw": resp.text}

    # Shiprocket often returns HTTP 200 with status_code=0 / errors in body
    status_code = body.get("status_code")
    if resp.status_code >= 400 or status_code in (0, "0", False):
        logger.error(
            "Shiprocket error %s %s HTTP %s body=%s",
            method,
            path,
            resp.status_code,
            body,
        )
        detail = (
            body.get("message")
            or body.get("error")
            or (body.get("errors") and str(body.get("errors")))
            or resp.text
            or "Shiprocket request failed"
        )
        if isinstance(detail, (list, dict)):
            detail = str(detail)
        raise HTTPException(status_code=502, detail=detail)

    return body


def _extract_shiprocket_ids(api_result: dict) -> tuple[str | None, str | None]:
    if not isinstance(api_result, dict):
        return None, None

    sr_order_id = api_result.get("order_id") or api_result.get("orderId")
    sr_shipment_id = api_result.get("shipment_id") or api_result.get("shipmentId")

    for key in ("payload", "data", "response"):
        nested = api_result.get(key)
        if isinstance(nested, dict):
            sr_order_id = sr_order_id or nested.get("order_id") or nested.get("orderId")
            sr_shipment_id = (
                sr_shipment_id
                or nested.get("shipment_id")
                or nested.get("shipmentId")
            )

    return (
        str(sr_order_id) if sr_order_id not in (None, "", 0, "0") else None,
        str(sr_shipment_id) if sr_shipment_id not in (None, "", 0, "0") else None,
    )


async def _load_order(db, order_id: str) -> Order | None:
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.order_id == order_id)
    )
    order = result.scalar_one_or_none()
    if order:
        return order
    if order_id.isdigit():
        result = await db.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == int(order_id))
        )
        return result.scalar_one_or_none()
    return None


async def _order_weight_kg(order: Order) -> float:
    total_grams = 0.0
    for item in order.items or []:
        vi = item.variant_info or {}
        grams = vi.get("weight_grams") or 0
        total_grams += float(grams) * item.quantity
    return max(0.5, total_grams / 1000) if total_grams else 0.5


def _split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "Customer").strip().split()
    if not parts:
        return "Customer", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _build_adhoc_payload(order: Order, weight_kg: float) -> dict:
    first, last = _split_name(order.customer_name)
    address = order.address_line1 or ""
    if order.address_line2:
        address = f"{address}, {order.address_line2}"
    if getattr(order, "address_landmark", None):
        address = f"{address}, {order.address_landmark}"

    # Prefer largest L/B/H from cart line items when present
    length = float(settings.SHIPROCKET_DEFAULT_LENGTH)
    breadth = float(settings.SHIPROCKET_DEFAULT_BREADTH)
    height = float(settings.SHIPROCKET_DEFAULT_HEIGHT)
    for i in order.items or []:
        info = i.variant_info if isinstance(i.variant_info, dict) else {}
        try:
            length = max(length, float(info.get("length_cm") or 0) or length)
            breadth = max(breadth, float(info.get("breadth_cm") or 0) or breadth)
            height = max(height, float(info.get("height_cm") or 0) or height)
        except (TypeError, ValueError):
            pass

    payload = {
        "order_id": order.order_id,
        "order_date": (order.created_at or utcnow()).strftime("%Y-%m-%d %H:%M"),
        "pickup_location": settings.SHIPROCKET_PICKUP_LOCATION or "work",
        "billing_customer_name": first,
        "billing_last_name": last,
        "billing_address": address[:190],
        "billing_address_2": (order.address_line2 or "")[:190],
        "billing_city": order.address_city,
        "billing_pincode": order.address_pincode,
        "billing_state": order.address_state,
        "billing_country": "India",
        "billing_email": order.customer_email
        or f"order-{order.order_id.lower()}@chairdekho.com",
        "billing_phone": "".join(c for c in (order.customer_phone or "") if c.isdigit())[
            -10:
        ],
        "shipping_is_billing": True,
        "order_items": [
            {
                "name": (
                    (
                        f"{i.name} ({format_variant_info_label(i.variant_info)})"
                        if format_variant_info_label(i.variant_info)
                        else (i.name or "Item")
                    )
                )[:200],
                "sku": str(i.product_id or i.id),
                "units": int(i.quantity),
                "selling_price": float(i.price),
            }
            for i in (order.items or [])
        ],
        "payment_method": "Prepaid"
        if (order.payment_method or "").lower() != "cod"
        else "COD",
        "sub_total": float(order.subtotal or 0),
        "length": length,
        "breadth": breadth,
        "height": height,
        "weight": max(round(float(weight_kg), 3), 0.5),
    }
    if settings.SHIPROCKET_CHANNEL_ID:
        payload["channel_id"] = settings.SHIPROCKET_CHANNEL_ID
    return payload


async def _maybe_assign_awb(shipment: Shipment) -> None:
    if not settings.SHIPROCKET_AUTO_AWB:
        return
    if not shipment.shiprocket_shipment_id:
        return
    try:
        body = {"shipment_id": int(shipment.shiprocket_shipment_id)}
        if settings.SHIPROCKET_COURIER_ID:
            body["courier_id"] = int(settings.SHIPROCKET_COURIER_ID)
        data = await _api("POST", "/courier/assign/awb", json=body)
        # Response shapes vary across Shiprocket versions
        response = data.get("response") if isinstance(data.get("response"), dict) else data
        awb = (
            response.get("awb_code")
            or response.get("awb")
            or data.get("awb_code")
            or data.get("awb")
        )
        courier = (
            response.get("courier_name")
            or response.get("courier_company_id")
            or data.get("courier_name")
        )
        if awb:
            shipment.awb_code = str(awb)
        if courier:
            shipment.courier_name = str(courier)
        if awb:
            shipment.tracking_url = f"https://shiprocket.co/tracking/{awb}"
            shipment.status = "awb_assigned"
    except Exception as exc:
        logger.warning(
            "Shiprocket AWB assign skipped for %s: %s", shipment.order_id, exc
        )


async def create_shipment(order_id: str) -> dict:
    if not shiprocket_configured():
        raise HTTPException(status_code=503, detail="Shiprocket is not configured")

    async with AsyncSessionLocal() as db:
        order = await _load_order(db, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if not order.items:
            raise HTTPException(status_code=400, detail="Order has no items")

        result = await db.execute(
            select(Shipment).where(Shipment.order_id == order.order_id)
        )
        existing = result.scalar_one_or_none()
        if (
            existing
            and existing.shiprocket_order_id
            and existing.status not in ("cancelled", "failed")
        ):
            return serialize_shipment(existing)

        weight_kg = await _order_weight_kg(order)
        payload = _build_adhoc_payload(order, weight_kg)
        logger.info(
            "Shiprocket create payload for %s pickup=%s phone=%s pincode=%s",
            order.order_id,
            payload.get("pickup_location"),
            payload.get("billing_phone"),
            payload.get("billing_pincode"),
        )

        api_result = await _api("POST", "/orders/create/adhoc", json=payload)
        logger.info("Shiprocket create response for %s: %s", order.order_id, api_result)

        sr_order_id, sr_shipment_id = _extract_shiprocket_ids(api_result)
        if not sr_order_id:
            message = (
                api_result.get("message")
                or api_result.get("error")
                or "Shiprocket did not return an order ID. Check pickup location name, API user, and address."
            )
            if not existing:
                existing = Shipment(
                    order_db_id=order.id,
                    order_id=order.order_id,
                )
                db.add(existing)
                await db.flush()
            existing.status = "failed"
            existing.shiprocket_order_id = None
            existing.shiprocket_shipment_id = None
            existing.updated_at = utcnow()
            await db.commit()
            raise HTTPException(status_code=502, detail=str(message))

        if not existing:
            existing = Shipment(
                order_db_id=order.id,
                order_id=order.order_id,
            )
            db.add(existing)
            await db.flush()

        existing.shiprocket_order_id = sr_order_id
        existing.shiprocket_shipment_id = sr_shipment_id
        existing.status = str(api_result.get("status") or "created")
        existing.updated_at = utcnow()

        await _maybe_assign_awb(existing)

        await db.commit()
        await db.refresh(existing)
        logger.info(
            "Shiprocket order created for %s → SR %s / shipment %s",
            order.order_id,
            existing.shiprocket_order_id,
            existing.shiprocket_shipment_id,
        )
        return serialize_shipment(existing)


async def push_order_to_shiprocket(
    order_id: str, *, raise_on_error: bool = True
) -> dict | None:
    """Create Shiprocket order for a store order. Safe for background use."""
    try:
        if not shiprocket_configured():
            logger.warning("Shiprocket not configured — skip push for %s", order_id)
            if raise_on_error:
                raise HTTPException(status_code=503, detail="Shiprocket is not configured")
            return None
        return await create_shipment(order_id)
    except Exception as exc:
        logger.exception("Shiprocket push failed for %s: %s", order_id, exc)
        if raise_on_error:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return None


async def track_shipment(order_id: str) -> dict:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Shipment).where(Shipment.order_id == order_id))
        shipment = result.scalar_one_or_none()
        if not shipment:
            raise HTTPException(status_code=404, detail="Shipment not found")
        if not shipment.awb_code:
            return {"shipment": serialize_shipment(shipment), "tracking": None}
        data = await _api("GET", f"/courier/track/awb/{shipment.awb_code}")
        return {"shipment": serialize_shipment(shipment), "tracking": data}


async def get_rates(pickup: str, delivery: str, weight: float, cod: bool) -> dict:
    params = {
        "pickup_postcode": pickup,
        "delivery_postcode": delivery,
        "weight": weight,
        "cod": 1 if cod else 0,
    }
    return await _api("GET", "/courier/serviceability/", params=params)


async def cancel_shipment(order_id: str) -> dict:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Shipment).where(Shipment.order_id == order_id))
        shipment = result.scalar_one_or_none()
        if not shipment or not shipment.shiprocket_order_id:
            raise HTTPException(status_code=404, detail="Shipment not found")
        await _api(
            "POST",
            "/orders/cancel",
            json={"ids": [int(shipment.shiprocket_order_id)]},
        )
        shipment.status = "cancelled"
        shipment.updated_at = utcnow()
        await db.commit()
        return serialize_shipment(shipment)


async def list_all_shipments() -> list[dict]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Shipment).order_by(Shipment.created_at.desc()))
        return [serialize_shipment(s) for s in result.scalars().all()]


async def get_shipment_for_order(order_id: str) -> dict | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Shipment).where(Shipment.order_id == order_id))
        shipment = result.scalar_one_or_none()
        return serialize_shipment(shipment) if shipment else None


async def update_shipment_manual(shipment_id: str, updates: dict) -> dict:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Shipment).where(Shipment.id == int(shipment_id))
        )
        shipment = result.scalar_one_or_none()
        if not shipment:
            raise HTTPException(status_code=404, detail="Shipment not found")
        for key, value in updates.items():
            if value is not None and hasattr(shipment, key):
                setattr(shipment, key, value)
        shipment.updated_at = utcnow()
        await db.commit()
        return serialize_shipment(shipment)
