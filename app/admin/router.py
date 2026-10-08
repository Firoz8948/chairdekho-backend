from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin
from app.database import get_db
from app.models import VideoProduct
from app.payments import service as payments_service
from app.shipping import service as shipping_service
from app.storage import ALLOWED_IMAGE_EXT, ALLOWED_VIDEO_EXT, upload_file

from . import service
from .schemas import (
    AdminLoginResponse,
    AdminProfileUpdateRequest,
    DashboardStats,
    ProductCreateRequest,
    ProductUpdateRequest,
)
from .schemas import AdminLoginRequest

router = APIRouter()

ALLOWED_EXT = ALLOWED_IMAGE_EXT
ALLOWED_VIDEO_EXT = ALLOWED_VIDEO_EXT


@router.post("/login", response_model=AdminLoginResponse)
async def admin_login(body: AdminLoginRequest, db: AsyncSession = Depends(get_db)):
    return await service.login(db, body.username, body.password)


@router.get("/me")
async def get_me(
    admin=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    profile = await service.get_admin_by_id(db, int(admin["id"]))
    if not profile:
        raise HTTPException(status_code=404, detail="Admin not found")
    return profile


@router.get("/settings")
async def get_brand_settings(_=Depends(get_current_admin)):
    """Locked brand / notification settings (env-managed, read-only)."""
    from app.config import settings

    return {
        "brand_name": settings.BRAND_NAME or "ChairDekho",
        "phone": settings.ADMIN_NOTIFY_PHONE or "9699164131",
        "email": settings.ADMIN_NOTIFY_EMAIL or "brjangu29@gmail.com",
        "locked": True,
        "otp_enabled": bool(settings.RENFLAIR_API_KEY) and not settings.OTP_DEBUG,
        "order_sms_enabled": bool(settings.RENFLAIR_API_KEY),
    }


@router.put("/me/profile")
async def update_me_profile(
    body: AdminProfileUpdateRequest,
    admin=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await service.update_admin_profile(
        db, int(admin["id"]), body.model_dump(exclude_unset=True)
    )


@router.get("/dashboard/stats", response_model=DashboardStats)
async def dashboard_stats(
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await service.dashboard_stats(db)


@router.get("/dashboard/sales")
async def dashboard_sales(
    range: str = Query("10d", pattern="^(7d|10d|30d|90d|12m)$"),
    scope: str = Query("all", pattern="^(all|paid)$"),
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await service.sales_trend(db, range_key=range, scope=scope)


@router.get("/products")
async def list_products(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
    category: str | None = None,
    search: str | None = None,
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await service.get_all_products(db, page, limit, category, search)


@router.get("/products/{product_id}")
async def get_product(
    product_id: int,
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    product = await service.get_product_by_id(db, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.post("/products", status_code=201)
async def create_product(
    body: ProductCreateRequest,
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await service.create_product(db, body.model_dump())


@router.post("/products/{product_id}/duplicate", status_code=201)
async def duplicate_product(
    product_id: int,
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    duplicated = await service.duplicate_product(db, product_id)
    if not duplicated:
        raise HTTPException(status_code=404, detail="Product not found")
    return duplicated


@router.put("/products/{product_id}")
async def update_product(
    product_id: int,
    body: ProductUpdateRequest,
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    updated = await service.update_product(
        db, product_id, body.model_dump(exclude_unset=True)
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Product not found")
    return updated


@router.delete("/products/{product_id}")
async def delete_product(
    product_id: int,
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    deleted = await service.delete_product(db, product_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"message": "Product deleted"}


@router.post("/products/{product_id}/images")
async def upload_images(
    product_id: int,
    files: list[UploadFile] = File(...),
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    product = await service.get_product_by_id(db, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    saved_urls = []
    for file in files:
        saved_urls.append(await upload_file(file, "products"))

    return await service.add_product_images(db, product_id, saved_urls)


@router.post("/products/{product_id}/images/from-urls")
async def attach_image_urls(
    product_id: int,
    body: dict,
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    product = await service.get_product_by_id(db, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    urls = body.get("urls") or []
    if not isinstance(urls, list) or not urls:
        raise HTTPException(status_code=400, detail="urls list is required")
    return await service.add_product_images(db, product_id, [str(u) for u in urls if u])


@router.get("/media/product-images")
async def list_product_media(
    product_id: int | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await service.list_product_media_library(db, product_id=product_id, limit=limit)


@router.delete("/products/{product_id}/images")
async def remove_image(
    product_id: int,
    image_url: str,
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    updated = await service.remove_product_image(db, product_id, image_url)
    if not updated:
        raise HTTPException(status_code=404, detail="Product not found")
    return updated


@router.put("/products/{product_id}/images/reorder")
async def reorder_images(
    product_id: int,
    body: dict,
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    urls = body.get("urls") or []
    if not isinstance(urls, list):
        raise HTTPException(status_code=400, detail="urls list is required")
    updated = await service.reorder_product_images(db, product_id, urls)
    if not updated:
        raise HTTPException(status_code=404, detail="Product not found")
    return updated


@router.get("/orders")
async def list_orders(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status: str | None = None,
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await service.get_all_orders(db, page, limit, status)


@router.put("/orders/{order_id}/status")
async def update_order(
    order_id: str,
    body: dict,
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    updated = await service.update_order_status(db, order_id, body.get("status"))
    if not updated:
        raise HTTPException(status_code=404, detail="Order not found")
    return updated


@router.get("/payments")
async def list_payments(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await service.get_all_payments(db, page, limit)


@router.post("/payments/{payment_id}/refund")
async def refund_payment(
    payment_id: str,
    body: dict | None = None,
    _=Depends(get_current_admin),
):
    """Full or partial Razorpay refund for a payment row."""
    body = body or {}
    return await payments_service.refund_payment(
        payment_id,
        amount=body.get("amount"),
        reason=body.get("reason"),
    )


@router.get("/users/")
async def admin_list_users(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await service.list_users(db, page, limit)


@router.get("/users/{user_id}")
async def admin_get_user(
    user_id: int, _=Depends(get_current_admin), db: AsyncSession = Depends(get_db)
):
    return await service.get_user(db, user_id)


@router.get("/shipping/")
async def admin_list_shipments(_=Depends(get_current_admin)):
    return await shipping_service.list_all_shipments()


@router.put("/shipping/{shipment_id}")
async def admin_update_shipment(
    shipment_id: str, payload: dict, _=Depends(get_current_admin)
):
    return await shipping_service.update_shipment_manual(shipment_id, payload)


@router.post("/orders/{order_id}/shiprocket")
async def admin_push_order_shiprocket(
    order_id: str, _=Depends(get_current_admin)
):
    """Manually push / re-sync an order to Shiprocket."""
    return await shipping_service.push_order_to_shiprocket(
        order_id, raise_on_error=True
    )


@router.post("/orders/{order_id}/shipmozo")
async def admin_push_order_shipmozo(
    order_id: str, _=Depends(get_current_admin)
):
    """Manually push an order to Shipmozo (push-order + optional auto-assign)."""
    from app.shipping import shipmozo

    return await shipmozo.push_order_to_shipmozo(order_id)


@router.post("/orders/{order_id}/delhivery")
async def admin_push_order_delhivery(order_id: str, _=Depends(get_current_admin)):
    """Create the B2C shipment for an order in Delhivery One."""
    from app.shipping import delhivery

    return await delhivery.push_order_to_delhivery(order_id)


@router.get("/orders/{order_id}/delhivery/label")
async def admin_delhivery_label(order_id: str, _=Depends(get_current_admin)):
    from app.shipping import delhivery

    return await delhivery.get_label_url(order_id)


@router.post("/orders/{order_id}/delhivery/cancel")
async def admin_cancel_delhivery(order_id: str, _=Depends(get_current_admin)):
    from app.shipping import delhivery

    return await delhivery.cancel_shipment(order_id)


@router.post("/orders/{order_id}/delhivery/track")
async def admin_track_delhivery(order_id: str, _=Depends(get_current_admin)):
    from app.shipping import delhivery

    return await delhivery.refresh_tracking(order_id)


@router.get("/orders/{order_id}/shipment")
async def admin_get_order_shipment(
    order_id: str, _=Depends(get_current_admin)
):
    shipment = await shipping_service.get_shipment_for_order(order_id)
    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")
    return shipment


@router.post("/products/add")
async def admin_add_product_legacy(
    body: ProductCreateRequest,
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    return await service.create_product(db, body.model_dump())


@router.post("/products/upload-image")
async def admin_upload_image_legacy(
    file: UploadFile = File(...), _=Depends(get_current_admin)
):
    url = await upload_file(file, "products")
    return {"url": url}


# ── Video Products ──────────────────────────────────────────────────────────

@router.get("/video-products")
async def list_video_products(
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import select
    result = await db.execute(select(VideoProduct).order_by(VideoProduct.position))
    items = result.scalars().all()
    return [_vp_to_dict(vp) for vp in items]


async def _upload_vp_images(files: list[UploadFile] | None) -> list[str]:
    urls: list[str] = []
    for f in files or []:
        if f and f.filename:
            urls.append(
                await upload_file(
                    f, "video-products", allowed_ext=ALLOWED_IMAGE_EXT, default_ext=".jpg"
                )
            )
    return urls


@router.post("/video-products", status_code=201)
async def create_video_product(
    name: str = Form(...),
    price: float = Form(...),
    mrp: float = Form(...),
    category: str = Form(...),
    description: str = Form(""),
    stock: int = Form(0),
    unit: str = Form("piece"),
    position: int = Form(0),
    is_active: bool = Form(True),
    product_id: int = Form(None),
    weight: float = Form(None),
    length_cm: float = Form(None),
    breadth_cm: float = Form(None),
    height_cm: float = Form(None),
    images_json: str = Form("[]"),
    video: UploadFile = File(None),
    images: list[UploadFile] = File(None),
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    import json

    video_url = None
    if video and video.filename:
        video_url = await upload_file(
            video, "videos", allowed_ext=ALLOWED_VIDEO_EXT, default_ext=".mp4"
        )

    try:
        existing = json.loads(images_json or "[]")
        if not isinstance(existing, list):
            existing = []
    except Exception:
        existing = []
    uploaded = await _upload_vp_images(images if isinstance(images, list) else ([images] if images else []))
    image_urls = [*existing, *uploaded]

    vp = VideoProduct(
        name=name,
        description=description,
        price=price,
        mrp=mrp,
        category=category,
        stock=stock,
        unit=unit,
        position=position,
        is_active=is_active,
        product_id=product_id,
        video_url=video_url,
        images=image_urls,
        weight=weight,
        length_cm=length_cm,
        breadth_cm=breadth_cm,
        height_cm=height_cm,
    )
    db.add(vp)
    await db.commit()
    await db.refresh(vp)
    return _vp_to_dict(vp)


@router.put("/video-products/{vp_id}")
async def update_video_product(
    vp_id: int,
    name: str = Form(None),
    price: float = Form(None),
    mrp: float = Form(None),
    category: str = Form(None),
    description: str = Form(None),
    stock: int = Form(None),
    unit: str = Form(None),
    position: int = Form(None),
    is_active: bool = Form(None),
    product_id: int = Form(None),
    clear_attached: bool = Form(False),
    weight: float = Form(None),
    length_cm: float = Form(None),
    breadth_cm: float = Form(None),
    height_cm: float = Form(None),
    images_json: str = Form(None),
    video: UploadFile = File(None),
    images: list[UploadFile] = File(None),
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    import json
    from sqlalchemy import select

    result = await db.execute(select(VideoProduct).where(VideoProduct.id == vp_id))
    vp = result.scalar_one_or_none()
    if not vp:
        raise HTTPException(status_code=404, detail="Video product not found")

    if name is not None:
        vp.name = name
    if price is not None:
        vp.price = price
    if mrp is not None:
        vp.mrp = mrp
    if category is not None:
        vp.category = category
    if description is not None:
        vp.description = description
    if stock is not None:
        vp.stock = stock
    if unit is not None:
        vp.unit = unit
    if position is not None:
        vp.position = position
    if is_active is not None:
        vp.is_active = is_active
    if clear_attached:
        vp.product_id = None
    elif product_id is not None and int(product_id) > 0:
        vp.product_id = int(product_id)
    if weight is not None:
        vp.weight = weight
    if length_cm is not None:
        vp.length_cm = length_cm
    if breadth_cm is not None:
        vp.breadth_cm = breadth_cm
    if height_cm is not None:
        vp.height_cm = height_cm

    if video and video.filename:
        vp.video_url = await upload_file(
            video, "videos", allowed_ext=ALLOWED_VIDEO_EXT, default_ext=".mp4"
        )

    if images_json is not None:
        try:
            kept = json.loads(images_json or "[]")
            if not isinstance(kept, list):
                kept = []
        except Exception:
            kept = list(vp.images or [])
        uploaded = await _upload_vp_images(
            images if isinstance(images, list) else ([images] if images else [])
        )
        vp.images = [*kept, *uploaded]
    elif images:
        uploaded = await _upload_vp_images(
            images if isinstance(images, list) else [images]
        )
        vp.images = [*(vp.images or []), *uploaded]

    await db.commit()
    await db.refresh(vp)
    return _vp_to_dict(vp)


@router.delete("/video-products/{vp_id}")
async def delete_video_product(
    vp_id: int,
    _=Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import select
    result = await db.execute(select(VideoProduct).where(VideoProduct.id == vp_id))
    vp = result.scalar_one_or_none()
    if not vp:
        raise HTTPException(status_code=404, detail="Video product not found")
    await db.delete(vp)
    await db.commit()
    return {"message": "Deleted"}


def _vp_to_dict(vp: VideoProduct) -> dict:
    return {
        "id": vp.id,
        "name": vp.name,
        "description": vp.description,
        "price": vp.price,
        "mrp": vp.mrp,
        "category": vp.category,
        "stock": vp.stock,
        "unit": vp.unit,
        "video_url": vp.video_url,
        "images": list(vp.images or []),
        "weight": vp.weight,
        "length_cm": vp.length_cm,
        "breadth_cm": vp.breadth_cm,
        "height_cm": vp.height_cm,
        "is_active": vp.is_active,
        "position": vp.position,
        "product_id": vp.product_id,
        "slug": f"video-{vp.id}",
        "created_at": vp.created_at.isoformat() if vp.created_at else None,
    }
