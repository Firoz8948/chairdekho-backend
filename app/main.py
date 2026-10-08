from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.admin.router import router as admin_router
from app.admin_switch.router import router as admin_switch_router
from app.auth.router import router as auth_router
from app.banners.router import router as banners_router
from app.categories.router import router as categories_router
from app.config import settings
from app.contact.router import router as contact_router
from app.database import connect_db, disconnect_db
from app.feeds.router import router as feeds_router
from app.meta.router import router as meta_router
from app.metafields.router import router as metafields_router
from app.orders.router import router as orders_router
from app.otp.router import router as otp_router
from app.payments.router import router as payments_router
from app.products.router import router as products_router
from app.promocodes.router import router as promocodes_router
from app.reviews.router import router as reviews_router
from app.shipping.router import router as shipping_router
from app.shipping_zones.router import router as shipping_zones_router
from app.storage.local import UPLOADS_DIR

prefix = settings.API_V1_PREFIX


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    yield
    await disconnect_db()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Outermost: trust Nginx X-Forwarded-Proto so slash redirects stay https://
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")

app.include_router(products_router, prefix=f"{prefix}/products", tags=["Products"])
app.include_router(categories_router, prefix=f"{prefix}/categories", tags=["Categories"])
app.include_router(otp_router, prefix=prefix)
app.include_router(auth_router, prefix=prefix)
app.include_router(metafields_router, prefix=f"{prefix}/metafields", tags=["Metafields"])
app.include_router(orders_router, prefix=f"{prefix}/orders", tags=["Orders"])
app.include_router(payments_router, prefix=f"{prefix}/payments", tags=["Payments"])
app.include_router(shipping_router, prefix=f"{prefix}/shipping", tags=["Shipping"])
app.include_router(admin_switch_router, prefix=f"{prefix}/admin/switch", tags=["Admin"])
app.include_router(admin_router, prefix=f"{prefix}/admin", tags=["Admin"])
app.include_router(contact_router, prefix=f"{prefix}/contact", tags=["Contact"])
app.include_router(banners_router, prefix=f"{prefix}/banners", tags=["Banners"])
app.include_router(promocodes_router, prefix=f"{prefix}/promocodes", tags=["Promo Codes"])
app.include_router(
    shipping_zones_router, prefix=f"{prefix}/shipping-zones", tags=["Shipping Zones"]
)
app.include_router(feeds_router, prefix=f"{prefix}/feeds", tags=["Catalog Feeds"])
app.include_router(meta_router, prefix=f"{prefix}/meta", tags=["Meta"])
app.include_router(reviews_router, prefix=f"{prefix}/reviews", tags=["Reviews"])


@app.get("/")
async def root():
    return {"message": "ChairDekho API", "docs": "/docs"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get(f"{prefix}/video-products")
async def public_video_products():
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.common import serialize_product
    from app.database import AsyncSessionLocal
    from app.models import Product, ProductVariant, VideoProduct

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(VideoProduct)
            .where(VideoProduct.is_active == True)  # noqa: E712
            .order_by(VideoProduct.position)
        )
        items = result.scalars().all()

        product_ids = [vp.product_id for vp in items if vp.product_id]
        products_by_id: dict = {}
        if product_ids:
            prod_result = await db.execute(
                select(Product)
                .options(
                    selectinload(Product.images),
                    selectinload(Product.variants).selectinload(ProductVariant.options),
                )
                .where(Product.id.in_(product_ids))
            )
            products_by_id = {p.id: p for p in prod_result.scalars().all()}

        out = []
        for vp in items:
            row = {
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
                "metafields": {},
                "weight": vp.weight,
                "length_cm": vp.length_cm,
                "breadth_cm": vp.breadth_cm,
                "height_cm": vp.height_cm,
                "product_id": vp.product_id,
                "slug": f"video-{vp.id}",
                "attached": False,
            }
            product = products_by_id.get(vp.product_id) if vp.product_id else None
            if product:
                pdata = serialize_product(product, include_relations=True)
                price = pdata.get("price")
                mrp = pdata.get("mrp")
                stock = pdata.get("stock")
                for variant in pdata.get("variants") or []:
                    for opt in variant.get("options") or []:
                        if (opt.get("stock") or 0) > 0:
                            price = opt.get("price", price)
                            mrp = opt.get("mrp", mrp)
                            stock = opt.get("stock", stock)
                            break
                    else:
                        continue
                    break

                row.update(
                    {
                        "name": pdata.get("name") or row["name"],
                        "description": pdata.get("description") or row["description"],
                        "price": price if price is not None else row["price"],
                        "mrp": mrp if mrp is not None else row["mrp"],
                        "category": pdata.get("category") or row["category"],
                        "stock": stock if stock is not None else row["stock"],
                        "unit": pdata.get("unit") or row["unit"],
                        "images": pdata.get("images") or row["images"],
                        "metafields": pdata.get("metafields") or {},
                        "weight": pdata.get("weight")
                        if pdata.get("weight") is not None
                        else row["weight"],
                        "length_cm": pdata.get("length_cm")
                        if pdata.get("length_cm") is not None
                        else row["length_cm"],
                        "breadth_cm": pdata.get("breadth_cm")
                        if pdata.get("breadth_cm") is not None
                        else row["breadth_cm"],
                        "height_cm": pdata.get("height_cm")
                        if pdata.get("height_cm") is not None
                        else row["height_cm"],
                        "product_id": product.id,
                        "slug": pdata.get("slug") or f"product-{product.id}",
                        "attached": True,
                    }
                )
            out.append(row)
        return out


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(__import__("os").environ.get("PORT", "8000")),
        reload=settings.ENVIRONMENT == "development",
    )


if __name__ == "__main__":
    main()
