"""Delhivery One B2C (Express parcel) client — manual push from Admin → Orders."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from app.common import format_variant_info_label, serialize_shipment, utcnow
from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Order, Shipment
from app.shipping import service as sr

logger = logging.getLogger("shipping.delhivery")

COURIER_NAME = "Delhivery"
TRACKING_URL = "https://www.delhivery.com/track-v2/package/{awb}"

# Delhivery rejects or mis-parses these characters inside manifest fields.
_UNSAFE_CHARS_RE = re.compile(r"[&#%;\\\"'<>]")


def _clean(value: str | None) -> str:
    return (value or "").strip().strip("\"'")


def delhivery_configured() -> bool:
    return bool(_clean(settings.DELHIVERY_API_TOKEN) and _clean(settings.DELHIVERY_PICKUP_LOCATION))


def _require_configured() -> None:
    if not _clean(settings.DELHIVERY_API_TOKEN):
        raise HTTPException(status_code=503, detail="Delhivery API token is not configured")
    if not _clean(settings.DELHIVERY_PICKUP_LOCATION):
        raise HTTPException(status_code=503, detail="Delhivery pickup location is not configured")


def _base_url() -> str:
    return (_clean(settings.DELHIVERY_BASE_URL) or "https://track.delhivery.com").rstrip("/")


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Token {_clean(settings.DELHIVERY_API_TOKEN)}",
        "Accept": "application/json",
    }


_BRAND_SUFFIX_RE = re.compile(r"\s*[|\u2013\u2014-]\s*chair\s*dekho(\.com)?\s*$", re.I)


def _safe(value: Any, limit: int = 250) -> str:
    text = str(value or "").replace("&", " and ")
    text = _UNSAFE_CHARS_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"\s+,", ",", text).strip()[:limit]


def _parse_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except Exception:
        return {"raw": resp.text[:1000]}
    return data if isinstance(data, dict) else {"data": data}


def _is_delhivery(shipment: Shipment | None) -> bool:
    return bool(shipment and (shipment.courier_name or "").lower() == COURIER_NAME.lower())


def _order_weight_grams(order: Order) -> float:
    total = 0.0
    for item in order.items or []:
        info = item.variant_info if isinstance(item.variant_info, dict) else {}
        try:
            grams = float(info.get("weight_grams") or 0)
        except (TypeError, ValueError):
            grams = 0
        total += grams * int(item.quantity or 1)
    return round(total) if total > 0 else float(settings.DELHIVERY_DEFAULT_WEIGHT_GRAMS)


def _order_dimensions(order: Order) -> tuple[float, float, float]:
    length = float(settings.DELHIVERY_DEFAULT_LENGTH)
    breadth = float(settings.DELHIVERY_DEFAULT_BREADTH)
    height = float(settings.DELHIVERY_DEFAULT_HEIGHT)
    for item in order.items or []:
        info = item.variant_info if isinstance(item.variant_info, dict) else {}
        try:
            length = max(length, float(info.get("length_cm") or 0))
            breadth = max(breadth, float(info.get("breadth_cm") or 0))
            height = max(height, float(info.get("height_cm") or 0))
        except (TypeError, ValueError):
            continue
    return length, breadth, height


def _products_desc(order: Order) -> str:
    parts = []
    for item in order.items or []:
        label = format_variant_info_label(item.variant_info)
        base = _BRAND_SUFFIX_RE.sub("", item.name or "").strip() or "Item"
        name = f"{base} ({label})" if label else base
        parts.append(f"{name} x{int(item.quantity or 1)}")
    return _safe(", ".join(parts), 500) or "Chairs"


def _build_manifest(order: Order, reference: str) -> dict:
    is_cod = (order.payment_method or "").lower() == "cod"
    address = ", ".join(
        p for p in (order.address_line1, order.address_line2, getattr(order, "address_landmark", None)) if p
    )
    phone = "".join(c for c in (order.customer_phone or "") if c.isdigit())[-10:]
    length, breadth, height = _order_dimensions(order)
    quantity = sum(int(i.quantity or 1) for i in (order.items or [])) or 1

    shipment = {
        "name": _safe(order.customer_name, 100) or "Customer",
        "add": _safe(address, 500),
        "pin": _safe(order.address_pincode, 6),
        "city": _safe(order.address_city, 100),
        "state": _safe(order.address_state, 100),
        "country": "India",
        "phone": phone,
        "order": reference,
        "payment_mode": "COD" if is_cod else "Prepaid",
        "cod_amount": f"{float(order.total or 0):.2f}" if is_cod else "0",
        "total_amount": f"{float(order.total or 0):.2f}",
        "order_date": (order.created_at or utcnow()).strftime("%Y-%m-%d %H:%M:%S"),
        "products_desc": _products_desc(order),
        "hsn_code": _clean(settings.DELHIVERY_HSN_CODE),
        "quantity": str(quantity),
        "weight": str(int(_order_weight_grams(order))),
        "shipment_length": str(length),
        "shipment_width": str(breadth),
        "shipment_height": str(height),
        "shipping_mode": _clean(settings.DELHIVERY_SHIPPING_MODE) or "Surface",
        "address_type": "home",
        "seller_name": "ChairDekho",
        "seller_add": "",
        "seller_inv": order.order_id,
        "waybill": "",
        # Empty return fields make Delhivery return RTOs to the registered pickup warehouse.
        "return_name": "",
        "return_add": "",
        "return_city": "",
        "return_state": "",
        "return_country": "",
        "return_pin": "",
        "return_phone": "",
    }
    gstin = _clean(settings.DELHIVERY_SELLER_GSTIN)
    if gstin:
        shipment["seller_gst_tin"] = gstin

    return {
        "shipments": [shipment],
        "pickup_location": {"name": _clean(settings.DELHIVERY_PICKUP_LOCATION)},
    }


def _manifest_error(body: dict) -> str:
    packages = body.get("packages") if isinstance(body.get("packages"), list) else []
    remarks: list[str] = []
    for pkg in packages:
        rem = pkg.get("remarks") if isinstance(pkg, dict) else None
        if isinstance(rem, list):
            remarks.extend(str(r) for r in rem if r)
        elif rem:
            remarks.append(str(rem))
    detail = "; ".join(remarks) or body.get("rmk") or body.get("error") or body.get("raw")
    return str(detail or "Delhivery rejected the shipment")


async def _load_order_and_shipment(db, order_id: str) -> tuple[Order, Shipment | None]:
    order = await sr._load_order(db, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    result = await db.execute(select(Shipment).where(Shipment.order_id == order.order_id))
    return order, result.scalar_one_or_none()


async def push_order_to_delhivery(order_id: str) -> dict:
    _require_configured()

    async with AsyncSessionLocal() as db:
        order, shipment = await _load_order_and_shipment(db, order_id)
        if not order.items:
            raise HTTPException(status_code=400, detail="Order has no items")
        if (order.order_status or "").lower() == "cancelled":
            raise HTTPException(status_code=400, detail="Cancelled orders cannot be shipped")
        if _is_delhivery(shipment) and shipment.awb_code and shipment.status != "cancelled":
            return serialize_shipment(shipment)

        # Delhivery refuses a reused order reference, so re-sends after a cancel get a suffix.
        reference = order.order_id
        if _is_delhivery(shipment) and shipment.status == "cancelled":
            reference = f"{order.order_id}-R{int(utcnow().timestamp()) % 100000}"

        payload = _build_manifest(order, reference)
        logger.info(
            "Delhivery manifest for %s ref=%s pin=%s mode=%s cod=%s",
            order.order_id,
            reference,
            payload["shipments"][0]["pin"],
            payload["shipments"][0]["shipping_mode"],
            payload["shipments"][0]["cod_amount"],
        )

        async with httpx.AsyncClient(base_url=_base_url(), timeout=45) as client:
            resp = await client.post(
                "/api/cmu/create.json",
                headers=_auth_headers(),
                data={"format": "json", "data": json.dumps(payload)},
            )
        body = _parse_json(resp)
        logger.info("Delhivery manifest response for %s: HTTP %s %s", order.order_id, resp.status_code, body)

        if resp.status_code in (401, 403):
            raise HTTPException(status_code=502, detail="Delhivery rejected the API token")

        packages = body.get("packages") if isinstance(body.get("packages"), list) else []
        package = packages[0] if packages and isinstance(packages[0], dict) else {}
        waybill = str(package.get("waybill") or "").strip()
        succeeded = str(package.get("status") or "").lower() == "success" and waybill

        if resp.status_code >= 400 or not succeeded:
            raise HTTPException(status_code=502, detail=_manifest_error(body))

        if not shipment:
            shipment = Shipment(order_db_id=order.id, order_id=order.order_id)
            db.add(shipment)
        shipment.courier_name = COURIER_NAME
        shipment.awb_code = waybill
        shipment.status = "manifested"
        shipment.tracking_url = TRACKING_URL.format(awb=waybill)
        shipment.updated_at = utcnow()
        await db.commit()
        await db.refresh(shipment)
        logger.info("Delhivery shipment created for %s → AWB %s", order.order_id, waybill)
        return serialize_shipment(shipment)


async def _get_delhivery_shipment(order_id: str) -> Shipment:
    async with AsyncSessionLocal() as db:
        _, shipment = await _load_order_and_shipment(db, order_id)
    if not _is_delhivery(shipment) or not shipment.awb_code:
        raise HTTPException(status_code=404, detail="This order has not been sent to Delhivery")
    return shipment


async def get_label_url(order_id: str) -> dict:
    _require_configured()
    shipment = await _get_delhivery_shipment(order_id)
    async with httpx.AsyncClient(base_url=_base_url(), timeout=90) as client:
        resp = await client.get(
            "/api/p/packing_slip",
            headers=_auth_headers(),
            params={"wbns": shipment.awb_code, "pdf": "true", "pdf_size": "4R"},
        )
    body = _parse_json(resp)
    packages = body.get("packages") if isinstance(body.get("packages"), list) else []
    link = next(
        (p.get("pdf_download_link") for p in packages if isinstance(p, dict) and p.get("pdf_download_link")),
        None,
    )
    if resp.status_code >= 400 or not link:
        logger.error("Delhivery label failed for %s: HTTP %s %s", order_id, resp.status_code, body)
        raise HTTPException(status_code=502, detail="Delhivery did not return a shipping label yet")
    return {"awb_code": shipment.awb_code, "label_url": link}


async def cancel_shipment(order_id: str) -> dict:
    _require_configured()
    shipment = await _get_delhivery_shipment(order_id)
    if shipment.status == "cancelled":
        return serialize_shipment(shipment)

    async with httpx.AsyncClient(base_url=_base_url(), timeout=45) as client:
        resp = await client.post(
            "/api/p/edit",
            headers={**_auth_headers(), "Content-Type": "application/json"},
            json={"waybill": shipment.awb_code, "cancellation": "true"},
        )
    body = _parse_json(resp)
    logger.info("Delhivery cancel response for %s: HTTP %s %s", order_id, resp.status_code, body)
    if resp.status_code >= 400 or body.get("status") in (False, "false", "False"):
        detail = body.get("remark") or body.get("error") or body.get("raw") or "Delhivery cancellation failed"
        raise HTTPException(status_code=502, detail=str(detail))

    async with AsyncSessionLocal() as db:
        _, fresh = await _load_order_and_shipment(db, order_id)
        fresh.status = "cancelled"
        fresh.updated_at = utcnow()
        await db.commit()
        await db.refresh(fresh)
        return serialize_shipment(fresh)


async def refresh_tracking(order_id: str) -> dict:
    _require_configured()
    shipment = await _get_delhivery_shipment(order_id)
    async with httpx.AsyncClient(base_url=_base_url(), timeout=30) as client:
        resp = await client.get(
            "/api/v1/packages/json/",
            headers=_auth_headers(),
            params={"waybill": shipment.awb_code},
        )
    body = _parse_json(resp)
    data = body.get("ShipmentData") if isinstance(body.get("ShipmentData"), list) else []
    status_block = ((data[0] or {}).get("Shipment") or {}).get("Status") or {} if data else {}
    status = str(status_block.get("Status") or "").strip()
    if resp.status_code >= 400 or not status:
        logger.error("Delhivery tracking failed for %s: HTTP %s %s", order_id, resp.status_code, body)
        raise HTTPException(status_code=502, detail="Could not fetch Delhivery tracking status")

    async with AsyncSessionLocal() as db:
        _, fresh = await _load_order_and_shipment(db, order_id)
        if fresh.status != "cancelled" or status.lower() != "manifested":
            fresh.status = status.lower()
        fresh.updated_at = utcnow()
        await db.commit()
        await db.refresh(fresh)
        return {
            "shipment": serialize_shipment(fresh),
            "status": status,
            "status_location": status_block.get("StatusLocation"),
            "status_time": status_block.get("StatusDateTime"),
            "instructions": status_block.get("Instructions"),
        }
