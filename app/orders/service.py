import logging
import re

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common import serialize_order, utcnow
from app.database import AsyncSessionLocal
from app.models import Order, OrderItem, Product, ProductVariant, ProductVariantOption
from app.orders.notifications import notify_order_placed
from app.auth import service as auth_service

from .models import ORDER_STATUSES, calc_subtotal, generate_order_id, normalize_items, total_cart_weight_grams

logger = logging.getLogger("orders")

_COLOR_VARIANT_RE = re.compile(r"colou?r", re.I)


def _customer_phone(customer: dict) -> str:
    return customer.get("phone") or customer.get("mobile") or ""


def _parse_product_id(raw) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text.isdigit():
        return int(text)
    return None


def _preferred_option_id(variant_info: dict | None) -> int | None:
    """Pick the stock-bearing option id from checkout variant_info."""
    if not isinstance(variant_info, dict):
        return None

    for key in ("option_id", "optionId"):
        raw = variant_info.get(key)
        if raw is not None and str(raw).strip().isdigit():
            return int(str(raw).strip())

    selections = variant_info.get("selections")
    if not isinstance(selections, list):
        return None

    color_id = None
    first_id = None
    for sel in selections:
        if not isinstance(sel, dict):
            continue
        raw = sel.get("option_id", sel.get("optionId"))
        if raw is None or not str(raw).strip().isdigit():
            continue
        oid = int(str(raw).strip())
        if first_id is None:
            first_id = oid
        variant_name = str(sel.get("variant") or sel.get("variantName") or "")
        if _COLOR_VARIANT_RE.search(variant_name):
            color_id = oid
            break
    return color_id if color_id is not None else first_id


def _match_option_by_names(
    product: Product, variant_info: dict | None
) -> ProductVariantOption | None:
    if not isinstance(variant_info, dict):
        return None
    option_name = (variant_info.get("option") or "").strip().lower()
    variant_name = (variant_info.get("variant") or "").strip().lower()
    if not option_name:
        return None

    color_match = None
    name_match = None
    for variant in product.variants or []:
        vname = (variant.name or "").strip().lower()
        for opt in variant.options or []:
            if (opt.name or "").strip().lower() != option_name:
                continue
            if variant_name and vname == variant_name:
                return opt
            if _COLOR_VARIANT_RE.search(variant.name or ""):
                color_match = color_match or opt
            name_match = name_match or opt
    return color_match or name_match


async def _apply_stock_for_items(
    db: AsyncSession, items: list[dict], *, deduct: bool
) -> None:
    """Validate (and optionally reduce) product/option stock for order items."""
    for item in items:
        pid = _parse_product_id(item.get("product_id"))
        qty = int(item.get("qty") or 0)
        if not pid or qty <= 0:
            continue

        result = await db.execute(
            select(Product)
            .options(
                selectinload(Product.variants).selectinload(ProductVariant.options)
            )
            .where(Product.id == pid)
            .with_for_update()
        )
        product = result.scalar_one_or_none()
        if not product:
            raise HTTPException(
                status_code=400,
                detail=f"Product not found for stock update (id={pid})",
            )

        variant_info = item.get("variant_info")
        option: ProductVariantOption | None = None
        option_id = _preferred_option_id(variant_info)
        if option_id is not None:
            for variant in product.variants or []:
                for opt in variant.options or []:
                    if opt.id == option_id:
                        option = opt
                        break
                if option:
                    break
        if option is None:
            option = _match_option_by_names(product, variant_info)

        label = item.get("name") or product.name
        if option is not None:
            if option.stock < qty:
                raise HTTPException(
                    status_code=400,
                    detail=f"Insufficient stock for {label} ({option.name})",
                )
            if deduct:
                option.stock -= qty
                product.stock = max(0, int(product.stock or 0) - qty)
                logger.info(
                    "Stock deducted product=%s option=%s qty=%s remaining_option=%s remaining_product=%s",
                    product.id,
                    option.id,
                    qty,
                    option.stock,
                    product.stock,
                )
        else:
            if int(product.stock or 0) < qty:
                raise HTTPException(
                    status_code=400,
                    detail=f"Insufficient stock for {label}",
                )
            if deduct:
                product.stock = int(product.stock or 0) - qty
                logger.info(
                    "Stock deducted product=%s qty=%s remaining=%s",
                    product.id,
                    qty,
                    product.stock,
                )


async def _deduct_stock_for_items(db: AsyncSession, items: list[dict]) -> None:
    await _apply_stock_for_items(db, items, deduct=True)


async def assert_stock_available(items: list[dict]) -> None:
    """Read-only stock check used before starting online payment."""
    normalized = normalize_items(items)
    async with AsyncSessionLocal() as db:
        await _apply_stock_for_items(db, normalized, deduct=False)
        await db.rollback()


async def _load_order(db: AsyncSession, order_id: str) -> Order | None:
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.shipment))
        .where(Order.order_id == order_id)
    )
    order = result.scalar_one_or_none()
    if order:
        return order
    if order_id.isdigit():
        result = await db.execute(
            select(Order)
            .options(selectinload(Order.items), selectinload(Order.shipment))
            .where(Order.id == int(order_id))
        )
        return result.scalar_one_or_none()
    return None


async def save_checkout_address(user_id: int | None, customer: dict, address: dict) -> None:
    """Remember the latest checkout address on the customer's profile (best effort)."""
    if not user_id or not (address or {}).get("line1"):
        return
    try:
        async with AsyncSessionLocal() as db:
            await auth_service.update_user_profile(
                db,
                user_id=user_id,
                name=(customer or {}).get("name") or None,
                email=(customer or {}).get("email") or None,
                address_line1=address.get("line1"),
                address_line2=address.get("line2") or "",
                address_landmark=address.get("landmark") or "",
                address_city=address.get("city"),
                address_state=address.get("state"),
                address_pincode=address.get("pincode"),
            )
    except Exception as exc:
        logger.warning("Failed to save checkout address for user %s: %s", user_id, exc)


async def create_customer_order(
    customer: dict,
    address: dict,
    items: list[dict],
    user_id: int | None = None,
    payment_method: str = "cod",
    payment_status: str = "pending",
    order_status: str = "processing",
    razorpay_payment_id: str | None = None,
    razorpay_order_id: str | None = None,
    promo_code: str | None = None,
    meta_event_id: str | None = None,
    meta_fbp: str | None = None,
    meta_fbc: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> dict:
    if not items:
        raise HTTPException(status_code=400, detail="Cart is empty")

    normalized = normalize_items(items)
    subtotal = calc_subtotal(normalized)
    weight = total_cart_weight_grams(normalized)

    async with AsyncSessionLocal() as db:
        from app.promocodes import service as promo_service
        from app.shipping_zones import service as zone_service

        # Lock + reduce stock before creating the order (COD and paid online).
        await _deduct_stock_for_items(db, normalized)

        shipping = await zone_service.resolve_shipping_charge(
            db,
            subtotal=subtotal,
            state=address.get("state"),
            pincode=address.get("pincode"),
            weight_grams=weight,
            payment_method=payment_method,
        )

        discount = 0.0
        applied_code = None
        if promo_code:
            discount, shipping, applied_code = await promo_service.resolve_promo_for_order(
                db,
                promo_code,
                subtotal,
                shipping,
                user_id=user_id,
                phone=_customer_phone(customer),
                consume=True,
            )

        order = Order(
            order_id=generate_order_id(),
            customer_name=customer.get("name", "Customer"),
            customer_phone=_customer_phone(customer),
            customer_email=customer.get("email"),
            address_line1=address.get("line1", ""),
            address_line2=address.get("line2"),
            address_landmark=address.get("landmark") or None,
            address_city=address.get("city", ""),
            address_state=address.get("state", ""),
            address_pincode=address.get("pincode", ""),
            subtotal=subtotal,
            shipping_charge=shipping,
            discount_amount=discount,
            promo_code=applied_code,
            total=round(max(subtotal - discount, 0) + shipping, 2),
            payment_method=payment_method,
            payment_status=payment_status,
            order_status=order_status,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_order_id=razorpay_order_id,
            user_id=user_id,
        )
        db.add(order)
        await db.flush()

        for item in normalized:
            pid = _parse_product_id(item.get("product_id"))
            slug = item.get("slug")
            if not slug and pid:
                prod_row = await db.execute(select(Product.slug).where(Product.id == pid))
                slug = prod_row.scalar_one_or_none()
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_id=pid,
                    name=item["name"],
                    slug=slug,
                    price=item["price"],
                    quantity=item["qty"],
                    image=item.get("image"),
                    variant_info=item.get("variant_info"),
                )
            )

        await db.commit()
        await db.refresh(order, ["items"])

        await save_checkout_address(user_id, customer, address)

        try:
            await notify_order_placed(
                customer_phone=order.customer_phone,
                customer_name=order.customer_name,
                order_id=order.order_id,
            )
        except Exception as exc:
            logger.warning("Order placement notifications failed: %s", exc)

        shipment_data = None
        try:
            from app.config import settings as app_settings
            from app.shipping import service as shipping_service

            if app_settings.SHIPROCKET_AUTO_PUSH and shipping_service.shiprocket_configured():
                shipment_data = await shipping_service.push_order_to_shiprocket(
                    order.order_id, raise_on_error=False
                )
        except Exception as exc:
            logger.warning("Shiprocket auto-push failed: %s", exc)

        payload = serialize_order(order)
        if shipment_data:
            payload["shipment"] = shipment_data

        try:
            from app.meta import capi

            await capi.track_purchase(
                order=payload,
                event_id=meta_event_id or order.order_id,
                fbp=meta_fbp,
                fbc=meta_fbc,
                client_ip=client_ip,
                user_agent=user_agent,
            )
        except Exception as exc:
            logger.warning("Meta CAPI Purchase failed: %s", exc)

        return payload


async def create_guest_order(*args, **kwargs) -> dict:
    """Backward-compatible alias."""
    return await create_customer_order(*args, **kwargs)


async def create_order_from_checkout(
    checkout: dict,
    razorpay_payment_id: str,
    razorpay_order_id: str,
    user_id: int | None = None,
    payment_method: str = "razorpay",
) -> dict:
    method = (payment_method or checkout.get("payment_method") or "razorpay").lower()
    if method not in {"razorpay", "payu"}:
        method = "razorpay"
    return await create_customer_order(
        customer=checkout["customer"],
        address=checkout["address"],
        items=checkout["items"],
        user_id=user_id,
        payment_method=method,
        payment_status="paid",
        order_status="processing",
        razorpay_payment_id=razorpay_payment_id,
        razorpay_order_id=razorpay_order_id,
        promo_code=checkout.get("promo_code"),
        meta_event_id=checkout.get("meta_event_id"),
        meta_fbp=checkout.get("meta_fbp"),
        meta_fbc=checkout.get("meta_fbc"),
    )


async def _enrich_order_item_slugs(db: AsyncSession, orders: list[dict]) -> list[dict]:
    """Fill missing item.slug from products for older orders."""
    missing: set[int] = set()
    for order in orders:
        for item in order.get("items") or []:
            if item.get("slug"):
                continue
            pid = item.get("product_id")
            if pid is not None and str(pid).isdigit():
                missing.add(int(pid))
    if not missing:
        return orders
    result = await db.execute(select(Product.id, Product.slug).where(Product.id.in_(missing)))
    mapping = {row[0]: row[1] for row in result.all()}
    for order in orders:
        for item in order.get("items") or []:
            if item.get("slug"):
                continue
            pid = item.get("product_id")
            if pid is not None and str(pid).isdigit():
                item["slug"] = mapping.get(int(pid))
    return orders


async def get_order(order_id: str, is_admin: bool = False) -> dict:
    async with AsyncSessionLocal() as db:
        order = await _load_order(db, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        payload = serialize_order(order)
        await _enrich_order_item_slugs(db, [payload])
        return payload


async def update_status(order_id: str, status: str) -> dict:
    if not status:
        raise HTTPException(status_code=400, detail="Status is required")
    if status not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")

    async with AsyncSessionLocal() as db:
        order = await _load_order(db, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        order.order_status = status
        order.updated_at = utcnow()
        await db.commit()
        await db.refresh(order, ["items"])
        payload = serialize_order(order)
        await _enrich_order_item_slugs(db, [payload])
        return payload


async def list_user_orders(db: AsyncSession, user_id: int, limit: int = 20) -> list[dict]:
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.shipment))
        .where(Order.user_id == user_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    orders = [serialize_order(o) for o in result.scalars().all()]
    return await _enrich_order_item_slugs(db, orders)


async def list_orders_paginated(
    page: int = 1, limit: int = 20, status: str | None = None
) -> dict:
    async with AsyncSessionLocal() as db:
        query = select(Order).options(
            selectinload(Order.items), selectinload(Order.shipment)
        )
        count_query = select(func.count(Order.id))
        if status:
            query = query.where(Order.order_status == status)
            count_query = count_query.where(Order.order_status == status)
        total = (await db.execute(count_query)).scalar() or 0
        result = await db.execute(
            query.order_by(Order.created_at.desc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
        orders = [serialize_order(o) for o in result.scalars().all()]
        await _enrich_order_item_slugs(db, orders)
        return {"orders": orders, "total": total, "page": page, "limit": limit}


async def count_orders() -> int:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(func.count(Order.id)))).scalar() or 0
