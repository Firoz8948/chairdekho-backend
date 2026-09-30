import hashlib
import hmac
import json
import logging
import uuid

from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.common import serialize_payment, utcnow
from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Order, Payment
from app.orders import service as order_service
from app.orders.models import calc_subtotal, normalize_items, total_cart_weight_grams
from app.payments import payu as payu_lib
from app.promocodes import service as promo_service

logger = logging.getLogger("payments")


def razorpay_configured() -> bool:
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def payu_configured() -> bool:
    return payu_lib.payu_configured()


def online_provider() -> str | None:
    """Preferred online gateway: PayU first, then Razorpay."""
    if payu_configured():
        return "payu"
    if razorpay_configured():
        return "razorpay"
    return None


def _client():
    if not razorpay_configured():
        raise HTTPException(
            status_code=503,
            detail="Razorpay is not configured. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET.",
        )
    import razorpay

    return razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )


def get_public_config() -> dict:
    provider = online_provider()
    return {
        "configured": provider is not None,
        "provider": provider,
        "key_id": settings.RAZORPAY_KEY_ID if provider == "razorpay" else None,
        "payu_mode": settings.PAYU_MODE if provider == "payu" else None,
        "currency": "INR",
    }


def _api_public_base() -> str:
    """Absolute API origin used for PayU surl/furl callbacks.

    Preference:
    1. API_PUBLIC_URL (full URL, optional)
    2. Derive from FRONTEND_URL for Lansdowne (api.lansdowneleather.com + API_V1_PREFIX)
    3. Localhost for development
    """
    base = (settings.API_PUBLIC_URL or "").rstrip("/")
    if base:
        return base

    frontend = (settings.FRONTEND_URL or "").lower()
    prefix = settings.API_V1_PREFIX or "/api/v1"
    if "lansdowneleather.com" in frontend:
        return f"https://api.lansdowneleather.com{prefix}"

    if settings.ENVIRONMENT.lower() in {"production", "prod"}:
        return f"https://api.lansdowneleather.com{prefix}"

    return f"http://localhost:8000{prefix}"


def _frontend_base() -> str:
    return (settings.FRONTEND_URL or "https://www.lansdowneleather.com").rstrip("/")


async def _build_checkout(
    customer: dict,
    address: dict,
    items: list[dict],
    promo_code: str | None = None,
    user_id: int | None = None,
) -> dict:
    normalized = normalize_items(items)
    subtotal = calc_subtotal(normalized)
    weight = total_cart_weight_grams(normalized)
    discount = 0.0
    applied_code = None

    async with AsyncSessionLocal() as db:
        from app.shipping_zones import service as zone_service

        shipping = await zone_service.resolve_shipping_charge(
            db,
            subtotal=subtotal,
            state=address.get("state"),
            pincode=address.get("pincode"),
            weight_grams=weight,
            payment_method="prepaid",
        )
        if promo_code:
            discount, shipping, applied_code = await promo_service.resolve_promo_for_order(
                db,
                promo_code,
                subtotal,
                shipping,
                user_id=user_id,
                phone=customer.get("phone") or customer.get("mobile"),
                consume=False,
            )

    return {
        "customer": customer,
        "address": address,
        "items": normalized,
        "subtotal": subtotal,
        "shipping_charge": shipping,
        "discount_amount": discount,
        "promo_code": applied_code,
        "total": round(max(subtotal - discount, 0) + shipping, 2),
    }


def _customer_email(customer: dict) -> str:
    email = (customer.get("email") or "").strip()
    if email:
        return email
    phone = "".join(ch for ch in str(customer.get("mobile") or "") if ch.isdigit())
    if phone:
        return f"{phone}@orders.lansdowneleather.com"
    return "orders@lansdowneleather.com"


async def create_payment_order(
    customer: dict,
    address: dict,
    items: list[dict],
    user_id: int | None = None,
    promo_code: str | None = None,
    meta_event_id: str | None = None,
    meta_fbp: str | None = None,
    meta_fbc: str | None = None,
) -> dict:
    if not items:
        raise HTTPException(status_code=400, detail="Cart is empty")

    provider = online_provider()
    if not provider:
        raise HTTPException(
            status_code=503,
            detail="Online payment is not configured. Set PAYU_KEY/PAYU_SALT (or Razorpay keys).",
        )

    checkout = await _build_checkout(
        customer, address, items, promo_code=promo_code, user_id=user_id
    )
    checkout["user_id"] = user_id
    checkout["payment_method"] = provider
    if meta_event_id:
        checkout["meta_event_id"] = meta_event_id
    if meta_fbp:
        checkout["meta_fbp"] = meta_fbp
    if meta_fbc:
        checkout["meta_fbc"] = meta_fbc

    if checkout["total"] < 1:
        raise HTTPException(
            status_code=400, detail="Order total must be at least ₹1.00"
        )

    # Fail early if cart items are already out of stock (before charging).
    await order_service.assert_stock_available(checkout["items"])

    await order_service.save_checkout_address(user_id, customer, address)

    if provider == "payu":
        return await _create_payu_order(checkout, customer, user_id)
    return await _create_razorpay_order(checkout, customer, user_id)


async def _create_payu_order(checkout: dict, customer: dict, user_id: int | None) -> dict:
    txnid = f"ld{uuid.uuid4().hex[:20]}"
    amount = payu_lib.format_amount(checkout["total"])
    firstname = (customer.get("name") or "Customer")[:60]
    email = _customer_email(customer)
    phone = "".join(ch for ch in str(customer.get("mobile") or "") if ch.isdigit())[-10:]
    productinfo = "Lansdowne Order"
    udf1 = str(user_id or "")
    key = settings.PAYU_KEY
    salt = settings.PAYU_SALT
    hash_value = payu_lib.request_hash(
        key=key,
        txnid=txnid,
        amount=amount,
        productinfo=productinfo,
        firstname=firstname,
        email=email,
        salt=salt,
        udf1=udf1,
    )

    api_base = _api_public_base()
    surl = f"{api_base}/payments/payu/success"
    furl = f"{api_base}/payments/payu/failure"

    async with AsyncSessionLocal() as db:
        payment = Payment(
            amount=checkout["total"],
            currency="INR",
            razorpay_order_id=txnid,  # stores PayU txnid
            status="created",
            checkout_snapshot=checkout,
        )
        db.add(payment)
        await db.commit()
        await db.refresh(payment)
        logger.info(
            "PayU txn created %s amount=%s payment_row=%s mode=%s",
            txnid,
            checkout["total"],
            payment.id,
            settings.PAYU_MODE,
        )

    fields = {
        "key": key,
        "txnid": txnid,
        "amount": amount,
        "productinfo": productinfo,
        "firstname": firstname,
        "email": email,
        "phone": phone,
        "surl": surl,
        "furl": furl,
        "hash": hash_value,
        "service_provider": "payu_paisa",
        "udf1": udf1,
        "udf2": "",
        "udf3": "",
        "udf4": "",
        "udf5": "",
    }

    return {
        "provider": "payu",
        "payment_id": str(payment.id),
        "amount": float(checkout["total"]),
        "currency": "INR",
        "payment_url": payu_lib.payment_url(),
        "payu": fields,
    }


async def _create_razorpay_order(
    checkout: dict, customer: dict, user_id: int | None
) -> dict:
    amount_paise = int(round(checkout["total"] * 100))
    receipt = f"rcpt_{uuid.uuid4().hex[:12]}"
    client = _client()

    try:
        rzp_order = client.order.create(
            {
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt,
                "payment_capture": 1,
                "notes": {
                    "customer_name": (customer.get("name") or "")[:100],
                    "customer_phone": (customer.get("mobile") or "")[:20],
                    "user_id": str(user_id or ""),
                },
            }
        )
    except Exception as exc:
        logger.exception("Razorpay order.create failed: %s", exc)
        raise HTTPException(
            status_code=502, detail=f"Razorpay order creation failed: {exc}"
        ) from exc

    async with AsyncSessionLocal() as db:
        payment = Payment(
            amount=checkout["total"],
            currency="INR",
            razorpay_order_id=rzp_order["id"],
            status="created",
            checkout_snapshot=checkout,
        )
        db.add(payment)
        await db.commit()
        await db.refresh(payment)
        logger.info(
            "Razorpay order created %s amount=%s payment_row=%s",
            rzp_order["id"],
            checkout["total"],
            payment.id,
        )

    return {
        "provider": "razorpay",
        "razorpay_order_id": rzp_order["id"],
        "amount": amount_paise,
        "currency": "INR",
        "key_id": settings.RAZORPAY_KEY_ID,
        "payment_id": str(payment.id),
    }


def _verify_signature(
    razorpay_order_id: str, razorpay_payment_id: str, signature: str
) -> bool:
    if not settings.RAZORPAY_KEY_SECRET:
        return False
    message = f"{razorpay_order_id}|{razorpay_payment_id}".encode()
    generated = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(), message, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(generated, signature)


def _fetch_razorpay_payment(razorpay_payment_id: str) -> dict:
    client = _client()
    try:
        return client.payment.fetch(razorpay_payment_id)
    except Exception as exc:
        logger.exception("Razorpay payment.fetch failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="Could not fetch payment from Razorpay"
        ) from exc


def _assert_payment_amount(rzp_payment: dict, expected_rupees: float) -> None:
    paid_paise = int(rzp_payment.get("amount") or 0)
    expected_paise = int(round(float(expected_rupees) * 100))
    if abs(paid_paise - expected_paise) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"Paid amount mismatch (expected {expected_paise} paise, got {paid_paise})",
        )
    status = (rzp_payment.get("status") or "").lower()
    if status not in {"captured", "authorized"}:
        raise HTTPException(
            status_code=400,
            detail=f"Payment not successful on Razorpay (status={status or 'unknown'})",
        )


async def _fulfill_paid_payment(
    payment: Payment,
    gateway_payment_id: str,
    gateway_order_id: str,
    user_id: int | None,
    db,
    payment_method: str | None = None,
) -> dict:
    """Create store order from snapshot if not already done. Idempotent."""
    if payment.order_db_id:
        order = await order_service.get_order(str(payment.order_db_id))
        return {
            "message": "Payment already verified",
            "order_id": order["order_id"],
            "order": order,
        }

    checkout = payment.checkout_snapshot
    if not checkout:
        raise HTTPException(status_code=400, detail="Checkout data missing")

    method = (
        payment_method
        or (checkout.get("payment_method") if checkout else None)
        or "razorpay"
    )

    order = await order_service.create_order_from_checkout(
        checkout,
        gateway_payment_id,
        gateway_order_id,
        user_id=user_id or checkout.get("user_id"),
        payment_method=method,
    )

    payment.order_db_id = int(order["id"])
    payment.razorpay_payment_id = gateway_payment_id
    payment.status = "paid"
    payment.updated_at = utcnow()
    await db.commit()

    return {
        "message": "Payment verified and order saved",
        "order_id": order["order_id"],
        "order": order,
    }


async def verify_payment(payload: dict, user_id: int | None = None) -> dict:
    razorpay_order_id = payload.get("razorpay_order_id")
    razorpay_payment_id = payload.get("razorpay_payment_id")
    signature = payload.get("razorpay_signature")
    if not (razorpay_order_id and razorpay_payment_id and signature):
        raise HTTPException(status_code=400, detail="Missing Razorpay verify fields")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Payment).where(Payment.razorpay_order_id == razorpay_order_id)
        )
        payment = result.scalar_one_or_none()

        if not _verify_signature(razorpay_order_id, razorpay_payment_id, signature):
            if payment:
                payment.status = "failed"
                payment.failure_reason = "signature_mismatch"
                payment.updated_at = utcnow()
                await db.commit()
            raise HTTPException(
                status_code=400, detail="Payment signature verification failed"
            )

        if not payment:
            raise HTTPException(status_code=404, detail="Payment session not found")

        if payment.status == "refunded":
            raise HTTPException(status_code=400, detail="Payment was refunded")

        snap = dict(payment.checkout_snapshot or {})
        if payload.get("meta_event_id"):
            snap["meta_event_id"] = payload["meta_event_id"]
        if payload.get("meta_fbp"):
            snap["meta_fbp"] = payload["meta_fbp"]
        if payload.get("meta_fbc"):
            snap["meta_fbc"] = payload["meta_fbc"]
        payment.checkout_snapshot = snap

        rzp_payment = _fetch_razorpay_payment(razorpay_payment_id)
        _assert_payment_amount(rzp_payment, payment.amount)

        return await _fulfill_paid_payment(
            payment,
            razorpay_payment_id,
            razorpay_order_id,
            user_id,
            db,
            payment_method="razorpay",
        )


async def handle_payu_callback(form: dict, *, success_path: bool) -> RedirectResponse:
    """
    PayU POSTs browser redirect here. Verify reverse hash, fulfill order, redirect
    to the storefront.
    """
    txnid = str(form.get("txnid") or "")
    status = str(form.get("status") or "").lower()
    mihpayid = str(form.get("mihpayid") or form.get("payuMoneyId") or "")
    amount = str(form.get("amount") or "")
    frontend = _frontend_base()

    if not txnid:
        return RedirectResponse(
            url=f"{frontend}/checkout?payment=failed&reason=missing_txn",
            status_code=303,
        )

    if not payu_lib.verify_response_hash(form):
        logger.warning("PayU hash mismatch for txnid=%s status=%s", txnid, status)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Payment).where(Payment.razorpay_order_id == txnid)
            )
            payment = result.scalar_one_or_none()
            if payment and payment.status not in {"paid", "refunded"}:
                payment.status = "failed"
                payment.failure_reason = "payu_hash_mismatch"
                payment.updated_at = utcnow()
                await db.commit()
        return RedirectResponse(
            url=f"{frontend}/checkout?payment=failed&reason=hash",
            status_code=303,
        )

    if status != "success" or not success_path:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Payment).where(Payment.razorpay_order_id == txnid)
            )
            payment = result.scalar_one_or_none()
            if payment and payment.status not in {"paid", "refunded"}:
                payment.status = "failed"
                payment.razorpay_payment_id = mihpayid or payment.razorpay_payment_id
                payment.failure_reason = status or "payu_failed"
                payment.updated_at = utcnow()
                await db.commit()
        return RedirectResponse(
            url=f"{frontend}/checkout?payment=failed&reason={status or 'failed'}",
            status_code=303,
        )

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Payment).where(Payment.razorpay_order_id == txnid)
        )
        payment = result.scalar_one_or_none()
        if not payment:
            return RedirectResponse(
                url=f"{frontend}/checkout?payment=failed&reason=unknown_txn",
                status_code=303,
            )

        expected = payu_lib.format_amount(payment.amount)
        if amount and abs(float(amount) - float(expected)) > 0.05:
            payment.status = "failed"
            payment.failure_reason = "amount_mismatch"
            payment.updated_at = utcnow()
            await db.commit()
            return RedirectResponse(
                url=f"{frontend}/checkout?payment=failed&reason=amount",
                status_code=303,
            )

        try:
            verify = await payu_lib.verify_payment_api(txnid)
            details = (verify.get("transaction_details") or {}).get(txnid) or {}
            mapped = str(
                details.get("status") or details.get("unmappedstatus") or ""
            ).lower()
            if details and mapped and mapped not in {"success", "captured", "completed"}:
                logger.warning(
                    "PayU verify_payment status unexpected txn=%s data=%s",
                    txnid,
                    details,
                )
        except Exception as exc:
            logger.warning("PayU verify_payment API failed for %s: %s", txnid, exc)

        user_id = None
        if payment.checkout_snapshot:
            user_id = payment.checkout_snapshot.get("user_id")

        result_payload = await _fulfill_paid_payment(
            payment,
            mihpayid or txnid,
            txnid,
            user_id,
            db,
            payment_method="payu",
        )
        order_id = result_payload.get("order_id")
        return RedirectResponse(
            url=f"{frontend}/orders?order={order_id}&payment=success",
            status_code=303,
        )


async def handle_webhook(body: bytes, signature: str) -> dict:
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        logger.warning("Razorpay webhook received but RAZORPAY_WEBHOOK_SECRET is empty")
        if settings.ENVIRONMENT.lower() not in ("development", "dev", "local"):
            raise HTTPException(
                status_code=503, detail="Webhook secret is not configured"
            )
    else:
        generated = hmac.new(
            settings.RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(generated, signature or ""):
            raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        payload = json.loads(body.decode("utf-8") if body else "{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook JSON") from exc

    event = payload.get("event") or ""
    entity = (
        (payload.get("payload") or {}).get("payment") or {}
    ).get("entity") or {}
    refund_entity = (
        (payload.get("payload") or {}).get("refund") or {}
    ).get("entity") or {}

    logger.info("Razorpay webhook event=%s", event)

    if event in ("payment.captured", "payment.authorized"):
        await _webhook_payment_success(entity)
    elif event in ("payment.failed",):
        await _webhook_payment_failed(entity)
    elif event in ("refund.processed", "refund.created"):
        await _webhook_refund(refund_entity or entity)
    else:
        logger.info("Unhandled Razorpay webhook event: %s", event)

    return {"status": "ok", "event": event}


async def _webhook_payment_success(entity: dict) -> None:
    razorpay_payment_id = entity.get("id")
    razorpay_order_id = entity.get("order_id")
    if not razorpay_order_id or not razorpay_payment_id:
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Payment).where(Payment.razorpay_order_id == razorpay_order_id)
        )
        payment = result.scalar_one_or_none()
        if not payment:
            logger.warning(
                "Webhook payment for unknown order %s", razorpay_order_id
            )
            return
        if payment.order_db_id or payment.status == "paid":
            payment.razorpay_payment_id = (
                payment.razorpay_payment_id or razorpay_payment_id
            )
            payment.status = "paid"
            payment.updated_at = utcnow()
            await db.commit()
            return

        try:
            _assert_payment_amount(entity, payment.amount)
        except HTTPException as exc:
            payment.status = "failed"
            payment.failure_reason = str(exc.detail)
            payment.updated_at = utcnow()
            await db.commit()
            logger.error("Webhook amount/status check failed: %s", exc.detail)
            return

        await _fulfill_paid_payment(
            payment,
            razorpay_payment_id,
            razorpay_order_id,
            payment.checkout_snapshot.get("user_id")
            if payment.checkout_snapshot
            else None,
            db,
            payment_method="razorpay",
        )


async def _webhook_payment_failed(entity: dict) -> None:
    razorpay_order_id = entity.get("order_id")
    if not razorpay_order_id:
        return
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Payment).where(Payment.razorpay_order_id == razorpay_order_id)
        )
        payment = result.scalar_one_or_none()
        if not payment or payment.status == "paid":
            return
        payment.status = "failed"
        payment.razorpay_payment_id = entity.get("id") or payment.razorpay_payment_id
        payment.failure_reason = (
            entity.get("error_description")
            or entity.get("error_reason")
            or "payment_failed"
        )
        payment.updated_at = utcnow()
        await db.commit()


async def _webhook_refund(entity: dict) -> None:
    razorpay_payment_id = entity.get("payment_id") or entity.get("id")
    if not razorpay_payment_id:
        return
    pay_id = entity.get("payment_id") or None
    refund_id = entity.get("id") if entity.get("payment_id") else None

    async with AsyncSessionLocal() as db:
        q = select(Payment)
        if pay_id:
            q = q.where(Payment.razorpay_payment_id == pay_id)
        else:
            q = q.where(Payment.razorpay_payment_id == razorpay_payment_id)
        result = await db.execute(q)
        payment = result.scalar_one_or_none()
        if not payment:
            return
        payment.status = "refunded"
        if refund_id:
            payment.razorpay_refund_id = str(refund_id)
        payment.updated_at = utcnow()
        if payment.order_db_id:
            order_result = await db.execute(
                select(Order).where(Order.id == payment.order_db_id)
            )
            order = order_result.scalar_one_or_none()
            if order:
                order.payment_status = "refunded"
        await db.commit()


async def refund_payment(
    payment_id: str,
    amount: float | None = None,
    reason: str | None = None,
) -> dict:
    """Full or partial refund via Razorpay. Admin use."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Payment).where(Payment.id == int(payment_id))
        )
        payment = result.scalar_one_or_none()
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.status == "refunded":
            return serialize_payment(payment)
        if payment.status != "paid" or not payment.razorpay_payment_id:
            raise HTTPException(
                status_code=400, detail="Only paid Razorpay payments can be refunded"
            )

        snap = payment.checkout_snapshot or {}
        if snap.get("payment_method") == "payu":
            raise HTTPException(
                status_code=400,
                detail="PayU refunds must be done from the PayU merchant dashboard for now",
            )

        refund_rupees = float(amount) if amount is not None else float(payment.amount)
        if refund_rupees <= 0 or refund_rupees > float(payment.amount) + 0.001:
            raise HTTPException(status_code=400, detail="Invalid refund amount")

        refund_paise = int(round(refund_rupees * 100))
        client = _client()
        try:
            body: dict = {"amount": refund_paise}
            if reason:
                body["notes"] = {"reason": reason[:200]}
            refund = client.payment.refund(payment.razorpay_payment_id, body)
        except Exception as exc:
            logger.exception("Razorpay refund failed: %s", exc)
            raise HTTPException(
                status_code=502, detail=f"Razorpay refund failed: {exc}"
            ) from exc

        payment.razorpay_refund_id = str(refund.get("id") or "")
        is_full = refund_paise >= int(round(float(payment.amount) * 100))
        payment.status = "refunded" if is_full else "partially_refunded"
        payment.updated_at = utcnow()

        if payment.order_db_id and is_full:
            order_result = await db.execute(
                select(Order).where(Order.id == payment.order_db_id)
            )
            order = order_result.scalar_one_or_none()
            if order:
                order.payment_status = "refunded"

        await db.commit()
        await db.refresh(payment)
        logger.info(
            "Refunded payment %s refund_id=%s amount=%s",
            payment.id,
            payment.razorpay_refund_id,
            refund_rupees,
        )
        return serialize_payment(payment)


async def get_payment(payment_id: str) -> dict:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Payment).where(Payment.id == int(payment_id))
        )
        payment = result.scalar_one_or_none()
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        return serialize_payment(payment)


async def list_payments_paginated(page: int = 1, limit: int = 20) -> dict:
    async with AsyncSessionLocal() as db:
        total = (await db.execute(select(func.count(Payment.id)))).scalar() or 0
        result = await db.execute(
            select(Payment)
            .options(selectinload(Payment.order))
            .order_by(Payment.created_at.desc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
        payments = [serialize_payment(p) for p in result.scalars().all()]
        return {"payments": payments, "total": total, "page": page, "limit": limit}
