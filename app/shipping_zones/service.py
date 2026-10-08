import json
import urllib.request

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ShippingZone
from app.orders.models import (
    FLAT_SHIPPING_CHARGE,
    FREE_SHIPPING_THRESHOLD,
    FREE_WEIGHT_GRAMS,
    WEIGHT_SURCHARGE_PER_KG,
)


def serialize_zone(zone: ShippingZone) -> dict:
    prepaid = (
        zone.prepaid_rate
        if getattr(zone, "prepaid_rate", None) is not None
        else zone.rate
    )
    cod = (
        zone.cod_rate
        if getattr(zone, "cod_rate", None) is not None
        else prepaid
    )
    return {
        "id": zone.id,
        "name": zone.name,
        "is_all_india": zone.is_all_india,
        "states": zone.states or [],
        "rate": float(prepaid),
        "prepaid_rate": float(prepaid),
        "cod_rate": float(cod),
        "free_shipping_threshold": zone.free_shipping_threshold,
        "is_active": zone.is_active,
        "position": zone.position,
        "created_at": zone.created_at.isoformat() if zone.created_at else None,
        "updated_at": zone.updated_at.isoformat() if zone.updated_at else None,
    }


def zone_base_rate(zone: ShippingZone, payment_method: str | None = None) -> float:
    method = (payment_method or "prepaid").strip().lower()
    prepaid = (
        zone.prepaid_rate
        if getattr(zone, "prepaid_rate", None) is not None
        else zone.rate
    )
    if method == "cod":
        if getattr(zone, "cod_rate", None) is not None:
            return float(zone.cod_rate)
        return float(prepaid)
    return float(prepaid)


def _norm_state(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def lookup_state_from_pincode(pincode: str) -> str | None:
    result = lookup_pincode_details(pincode)
    return (result or {}).get("state")


def lookup_pincode_details(pincode: str) -> dict | None:
    """Resolve city/state for an Indian pincode via postalpincode.in (server-side)."""
    pin = "".join(c for c in str(pincode or "") if c.isdigit())
    if len(pin) != 6:
        return None
    try:
        url = f"https://api.postalpincode.in/pincode/{pin}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "ChairDekho/1.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        entry = data[0] if isinstance(data, list) and data else None
        if not entry or entry.get("Status") != "Success":
            return None
        offices = entry.get("PostOffice") or []
        if not offices:
            return None
        office = offices[0]
        return {
            "pincode": pin,
            "city": office.get("District")
            or office.get("Block")
            or office.get("Name")
            or "",
            "state": office.get("State") or "",
            "post_office": office.get("Name") or "",
        }
    except Exception:
        return None


def calc_zone_shipping(
    subtotal: float,
    base_rate: float,
    total_weight_grams: float = 0,
    free_threshold: float | None = None,
) -> float:
    threshold = (
        FREE_SHIPPING_THRESHOLD if free_threshold is None else float(free_threshold)
    )
    if subtotal <= 0:
        return 0.0
    if threshold > 0 and subtotal >= threshold:
        charge = 0.0
    else:
        charge = float(base_rate)

    if total_weight_grams > FREE_WEIGHT_GRAMS:
        extra_kg = (total_weight_grams - FREE_WEIGHT_GRAMS) / 1000
        charge += round(extra_kg * WEIGHT_SURCHARGE_PER_KG, 2)

    return round(charge, 2)


async def list_zones(db: AsyncSession, active_only: bool = False) -> list[dict]:
    q = select(ShippingZone).order_by(ShippingZone.position.asc(), ShippingZone.id.asc())
    if active_only:
        q = q.where(ShippingZone.is_active == True)  # noqa: E712
    result = await db.execute(q)
    return [serialize_zone(z) for z in result.scalars().all()]


async def create_zone(db: AsyncSession, data: dict) -> dict:
    if data.get("is_all_india"):
        data["states"] = []
    elif not data.get("states"):
        raise HTTPException(
            status_code=400,
            detail="Select at least one state, or enable All over India",
        )
    prepaid = data.get("prepaid_rate")
    if prepaid is None:
        prepaid = data.get("rate", 49)
    cod = data.get("cod_rate")
    if cod is None:
        cod = prepaid
    data["prepaid_rate"] = float(prepaid)
    data["cod_rate"] = float(cod)
    data["rate"] = float(prepaid)
    zone = ShippingZone(**data)
    db.add(zone)
    await db.commit()
    await db.refresh(zone)
    return serialize_zone(zone)


async def update_zone(db: AsyncSession, zone_id: int, data: dict) -> dict | None:
    result = await db.execute(select(ShippingZone).where(ShippingZone.id == zone_id))
    zone = result.scalar_one_or_none()
    if not zone:
        return None

    for key, value in data.items():
        setattr(zone, key, value)

    if zone.is_all_india:
        zone.states = []
    elif not (zone.states or []):
        raise HTTPException(
            status_code=400,
            detail="Select at least one state, or enable All over India",
        )

    if zone.prepaid_rate is None and zone.rate is not None:
        zone.prepaid_rate = zone.rate
    if zone.prepaid_rate is not None:
        zone.rate = zone.prepaid_rate
    if zone.cod_rate is None and zone.prepaid_rate is not None:
        zone.cod_rate = zone.prepaid_rate

    await db.commit()
    await db.refresh(zone)
    return serialize_zone(zone)


async def delete_zone(db: AsyncSession, zone_id: int) -> bool:
    result = await db.execute(select(ShippingZone).where(ShippingZone.id == zone_id))
    zone = result.scalar_one_or_none()
    if not zone:
        return False
    await db.delete(zone)
    await db.commit()
    return True


async def find_zone_for_state(
    db: AsyncSession, state: str | None
) -> ShippingZone | None:
    result = await db.execute(
        select(ShippingZone)
        .where(ShippingZone.is_active == True)  # noqa: E712
        .order_by(ShippingZone.position.asc(), ShippingZone.id.asc())
    )
    zones = list(result.scalars().all())
    target = _norm_state(state)

    if target:
        for zone in zones:
            if zone.is_all_india:
                continue
            for s in zone.states or []:
                if _norm_state(s) == target:
                    return zone

    for zone in zones:
        if zone.is_all_india:
            return zone

    return None


async def quote_shipping(
    db: AsyncSession,
    *,
    subtotal: float,
    state: str | None = None,
    pincode: str | None = None,
    weight_grams: float = 0,
    payment_method: str | None = "prepaid",
) -> dict:
    resolved_state = (state or "").strip() or None
    if not resolved_state and pincode:
        resolved_state = lookup_state_from_pincode(pincode)

    method = (payment_method or "prepaid").strip().lower()
    if method in {"razorpay", "payu"}:
        method = "prepaid"

    zone = await find_zone_for_state(db, resolved_state)
    if zone:
        prepaid_base = zone_base_rate(zone, "prepaid")
        cod_base = zone_base_rate(zone, "cod")
        prepaid_charge = calc_zone_shipping(
            subtotal,
            prepaid_base,
            weight_grams,
            zone.free_shipping_threshold,
        )
        cod_charge = calc_zone_shipping(
            subtotal,
            cod_base,
            weight_grams,
            zone.free_shipping_threshold,
        )
        charge = cod_charge if method == "cod" else prepaid_charge
        base_rate = cod_base if method == "cod" else prepaid_base
        return {
            "shipping_charge": charge,
            "rate": base_rate,
            "prepaid_rate": prepaid_base,
            "cod_rate": cod_base,
            "prepaid_shipping_charge": prepaid_charge,
            "cod_shipping_charge": cod_charge,
            "payment_method": method,
            "zone_id": zone.id,
            "zone_name": zone.name,
            "is_all_india": zone.is_all_india,
            "state": resolved_state,
            "free_shipping_threshold": zone.free_shipping_threshold
            if zone.free_shipping_threshold is not None
            else FREE_SHIPPING_THRESHOLD,
            "fallback": False,
        }

    prepaid_charge = calc_zone_shipping(
        subtotal, FLAT_SHIPPING_CHARGE, weight_grams, FREE_SHIPPING_THRESHOLD
    )
    return {
        "shipping_charge": prepaid_charge,
        "rate": FLAT_SHIPPING_CHARGE,
        "prepaid_rate": FLAT_SHIPPING_CHARGE,
        "cod_rate": FLAT_SHIPPING_CHARGE,
        "prepaid_shipping_charge": prepaid_charge,
        "cod_shipping_charge": prepaid_charge,
        "payment_method": method,
        "zone_id": None,
        "zone_name": None,
        "is_all_india": False,
        "state": resolved_state,
        "free_shipping_threshold": FREE_SHIPPING_THRESHOLD,
        "fallback": True,
    }


async def resolve_shipping_charge(
    db: AsyncSession,
    *,
    subtotal: float,
    state: str | None,
    pincode: str | None = None,
    weight_grams: float = 0,
    payment_method: str | None = "prepaid",
) -> float:
    quote = await quote_shipping(
        db,
        subtotal=subtotal,
        state=state,
        pincode=pincode,
        weight_grams=weight_grams,
        payment_method=payment_method,
    )
    return float(quote["shipping_charge"])
