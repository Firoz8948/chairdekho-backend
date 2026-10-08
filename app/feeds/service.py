"""Product catalog feeds for Meta Commerce Manager and Google Merchant Center."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Product, ProductVariant


FEED_BRAND = "ChairDekho"

_BRAND_SUFFIX_RE = re.compile(r"\s*[|\u2013\u2014-]\s*chair\s*dekho(\.com)?\s*$", re.I)

# First match wins; checked against name, then product_type metafield, then category.
_GOOGLE_CATEGORIES = [
    (
        re.compile(r"office|revolving|executive|ergonomic|computer|study", re.I),
        "Furniture > Office Furniture > Office Chairs",
    ),
    (re.compile(r"\bstools?\b", re.I), "Furniture > Chairs > Table & Bar Stools"),
    (re.compile(r"\btables?\b", re.I), "Furniture > Tables"),
    (
        re.compile(r"garden|outdoor|patio|lawn|balcony", re.I),
        "Furniture > Outdoor Furniture > Outdoor Seating",
    ),
    (
        re.compile(r"dining|cafe|restaurant", re.I),
        "Furniture > Chairs > Kitchen & Dining Room Chairs",
    ),
    (
        re.compile(r"arm\s*chair|with\s+arms?", re.I),
        "Furniture > Chairs > Arm Chairs, Recliners & Sleeper Chairs",
    ),
]
_DEFAULT_GOOGLE_CATEGORY = "Furniture > Chairs"
_KIDS_RE = re.compile(r"\bkids?\b|child|baby", re.I)


def _site_url() -> str:
    """Storefront origin for product links; must match the canonical (non-www) URLs."""
    url = (settings.FRONTEND_URL or "https://chairdekho.in").rstrip("/")
    return re.sub(r"^(https?://)www\.", r"\1", url)


def _cdn_base() -> str:
    return (settings.BUNNY_CDN_URL or "").rstrip("/")


def _abs_image(url: str | None) -> str:
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    cdn = _cdn_base()
    if cdn:
        return f"{cdn}/{url.lstrip('/')}"
    # Legacy local uploads — expose via frontend rewrite or absolute API later
    return url


def _clean_text(value: str | None, limit: int = 5000) -> str:
    if not value:
        return ""
    text = (
        str(value)
        .replace("<br>", " ")
        .replace("<br/>", " ")
        .replace("<br />", " ")
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _price(amount: float | None) -> str:
    return f"{float(amount or 0):.2f} INR"


async def load_active_products(db: AsyncSession) -> list[Product]:
    result = await db.execute(
        select(Product)
        .options(
            selectinload(Product.images),
            selectinload(Product.variants).selectinload(ProductVariant.options),
        )
        .where(Product.is_active == True)  # noqa: E712
        .order_by(Product.id.asc())
    )
    return list(result.scalars().all())


def _metafield(product: Product, key: str) -> str:
    return _clean_text((product.metafields or {}).get(key), 100)


def _google_category(product: Product) -> str:
    for source in (product.name, _metafield(product, "product_type"), product.category):
        if not source:
            continue
        for pattern, category in _GOOGLE_CATEGORIES:
            if pattern.search(source):
                return category
    return _DEFAULT_GOOGLE_CATEGORY


def _age_group(product: Product) -> str | None:
    for source in (product.name, product.category):
        if source and _KIDS_RE.search(source):
            return "kids"
    return None


def _color(product: Product) -> str:
    color = _metafield(product, "color")
    if color:
        return color[:100]
    names = [c.get("name") for c in (product.colors or []) if isinstance(c, dict) and c.get("name")]
    return "/".join(names[:3])[:100]


def _total_stock(product: Product) -> int:
    option_stocks = [o.stock or 0 for v in (product.variants or []) for o in (v.options or [])]
    return sum(option_stocks) if option_stocks else (product.stock or 0)


def product_to_feed_row(product: Product) -> dict:
    images = sorted(product.images or [], key=lambda i: i.position or 0)
    primary = _abs_image(images[0].url) if images else ""
    extra = [_abs_image(img.url) for img in images[1:5] if img.url]
    availability = "in stock" if _total_stock(product) > 0 else "out of stock"
    title = _BRAND_SUFFIX_RE.sub("", _clean_text(product.name, 200)).strip()[:150]
    description = _clean_text(product.description) or f"{title} at an affordable price from {FEED_BRAND}."
    link = f"{_site_url()}/products/{product.slug}"
    material = _metafield(product, "material")
    row = {
        "id": str(product.id),
        "title": title,
        "description": description,
        "availability": availability,
        "condition": "new",
        "price": _price(product.mrp if product.mrp and product.mrp > 0 else product.price),
        "sale_price": None,
        "link": link,
        "image_link": primary,
        "additional_image_link": extra,
        "brand": FEED_BRAND,
        "product_type": _clean_text(product.category, 200) or "Chairs",
        "google_product_category": _google_category(product),
        "age_group": _age_group(product),
        "color": _color(product),
        "material": material or None,
        "item_group_id": product.color_group_id or None,
    }
    if product.mrp and product.price and product.mrp > product.price:
        row["price"] = _price(product.mrp)
        row["sale_price"] = _price(product.price)
    else:
        row["price"] = _price(product.price)
    return row


def build_facebook_rss(products: list[Product]) -> str:
    site = _site_url()
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items = []
    for p in products:
        row = product_to_feed_row(p)
        if not row["image_link"] or not row["title"]:
            continue
        extra_xml = "".join(
            f"<g:additional_image_link>{escape(u)}</g:additional_image_link>"
            for u in row["additional_image_link"]
            if u
        )
        sale = (
            f"<g:sale_price>{escape(row['sale_price'])}</g:sale_price>"
            if row.get("sale_price")
            else ""
        )
        attributes = "".join(
            f"<g:{key}>{escape(row[key])}</g:{key}>"
            for key in ("age_group", "color", "material", "item_group_id")
            if row.get(key)
        )
        items.append(
            f"""
    <item>
      <g:id>{escape(row['id'])}</g:id>
      <g:title>{escape(row['title'])}</g:title>
      <g:description>{escape(row['description'])}</g:description>
      <g:availability>{escape(row['availability'])}</g:availability>
      <g:condition>{escape(row['condition'])}</g:condition>
      <g:price>{escape(row['price'])}</g:price>
      {sale}
      <g:link>{escape(row['link'])}</g:link>
      <g:image_link>{escape(row['image_link'])}</g:image_link>
      {extra_xml}
      <g:brand>{escape(row['brand'])}</g:brand>
      <g:identifier_exists>false</g:identifier_exists>
      <g:product_type>{escape(row['product_type'])}</g:product_type>
      <g:google_product_category>{escape(row['google_product_category'])}</g:google_product_category>
      {attributes}
    </item>"""
        )

    body = "\n".join(items)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">
  <channel>
    <title>{escape(FEED_BRAND)} Product Feed</title>
    <link>{escape(site)}</link>
    <description>Product catalog feed for Meta Commerce / Google Merchant</description>
    <lastBuildDate>{now}</lastBuildDate>
{body}
  </channel>
</rss>
"""


def build_google_merchant_rss(products: list[Product]) -> str:
    # Same Google namespace format; Meta and Google both accept g: fields
    return build_facebook_rss(products)
