import bcrypt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def connect_db():
    from app.models import (  # noqa: F401
        Admin,
        BannerSlide,
        Cart,
        CartItem,
        Category,
        Contact,
        Order,
        OrderItem,
        OTP,
        Payment,
        Product,
        ProductImage,
        ProductVariant,
        ProductVariantOption,
        PromoCode,
        MetafieldDefinition,
        Shipment,
        ShippingZone,
        User,
        VideoProduct,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        from sqlalchemy import text

        await conn.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_products_category_id ON products(category_id)"
            )
        )

        # Phone-based OTP migration (users + otps)
        await conn.execute(
            text("ALTER TABLE users ALTER COLUMN phone DROP NOT NULL")
        )
        await conn.execute(text("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_phone_key"))
        await conn.execute(
            text("ALTER TABLE users ALTER COLUMN email DROP NOT NULL")
        )
        await conn.execute(text("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key"))
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_phone_unique "
                "ON users(phone) WHERE phone IS NOT NULL AND phone <> ''"
            )
        )

        await conn.execute(text("DELETE FROM otps"))
        await conn.execute(
            text("ALTER TABLE otps ADD COLUMN IF NOT EXISTS phone VARCHAR(20)")
        )
        await conn.execute(text("ALTER TABLE otps DROP COLUMN IF EXISTS email"))
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_otps_phone ON otps(phone)")
        )

        await conn.execute(
            text(
                "ALTER TABLE product_variant_options "
                "ADD COLUMN IF NOT EXISTS weight DOUBLE PRECISION"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE products "
                "ADD COLUMN IF NOT EXISTS metafields JSONB DEFAULT '{}'"
            )
        )
        await conn.execute(
            text("ALTER TABLE admins ADD COLUMN IF NOT EXISTS phone VARCHAR(20)")
        )
        await conn.execute(
            text(
                "ALTER TABLE admins ADD COLUMN IF NOT EXISTS company_name VARCHAR(255)"
            )
        )
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS address_line1 VARCHAR(500)")
        )
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS address_line2 VARCHAR(500)")
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS address_landmark VARCHAR(255)"
            )
        )
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS address_city VARCHAR(100)")
        )
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS address_state VARCHAR(100)")
        )
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS address_pincode VARCHAR(10)")
        )
        await conn.execute(
            text("ALTER TABLE users ADD COLUMN IF NOT EXISTS date_of_birth DATE")
        )
        await conn.execute(
            text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS address_landmark VARCHAR(255)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS discount_amount DOUBLE PRECISION DEFAULT 0"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE orders ADD COLUMN IF NOT EXISTS promo_code VARCHAR(50)"
            )
        )
        # Databases shared with an older release can carry a NOT NULL orders.is_custom
        # column this model never writes; give it a default so inserts succeed.
        await conn.execute(
            text(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'orders'
                          AND column_name = 'is_custom'
                    ) THEN
                        ALTER TABLE orders ALTER COLUMN is_custom SET DEFAULT FALSE;
                    END IF;
                END
                $$;
                """
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS audience VARCHAR(20) DEFAULT 'all'"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS max_uses INTEGER"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE promo_codes ADD COLUMN IF NOT EXISTS uses_count INTEGER DEFAULT 0"
            )
        )
        await conn.execute(
            text(
                "UPDATE promo_codes SET audience = 'all' WHERE audience IS NULL"
            )
        )
        await conn.execute(
            text(
                "UPDATE promo_codes SET uses_count = 0 WHERE uses_count IS NULL"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE shipping_zones ADD COLUMN IF NOT EXISTS prepaid_rate DOUBLE PRECISION"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE shipping_zones ADD COLUMN IF NOT EXISTS cod_rate DOUBLE PRECISION"
            )
        )
        await conn.execute(
            text(
                "UPDATE shipping_zones SET prepaid_rate = rate "
                "WHERE prepaid_rate IS NULL"
            )
        )
        await conn.execute(
            text(
                "UPDATE shipping_zones SET cod_rate = COALESCE(prepaid_rate, rate) "
                "WHERE cod_rate IS NULL"
            )
        )

        # Razorpay payment extras
        await conn.execute(
            text(
                "ALTER TABLE payments "
                "ADD COLUMN IF NOT EXISTS razorpay_refund_id VARCHAR(255)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE payments "
                "ADD COLUMN IF NOT EXISTS failure_reason VARCHAR(500)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE payments "
                "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()"
            )
        )

        await conn.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS length_cm DOUBLE PRECISION"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS breadth_cm DOUBLE PRECISION"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS height_cm DOUBLE PRECISION"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "colors JSONB DEFAULT '[]'"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                "color_group_id VARCHAR(36)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_products_color_group_id "
                "ON products(color_group_id)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE product_variant_options "
                "ADD COLUMN IF NOT EXISTS hex VARCHAR(7)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE product_variant_options "
                "ADD COLUMN IF NOT EXISTS colors JSONB DEFAULT '[]'"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE product_variant_options "
                "ADD COLUMN IF NOT EXISTS image_url VARCHAR(500)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE product_variant_options "
                "ADD COLUMN IF NOT EXISTS images JSONB DEFAULT '[]'"
            )
        )
        # Backfill images from legacy single image_url where empty
        await conn.execute(
            text(
                """
                UPDATE product_variant_options
                SET images = jsonb_build_array(image_url)
                WHERE image_url IS NOT NULL
                  AND image_url <> ''
                  AND (images IS NULL OR images = '[]'::jsonb)
                """
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE video_products ADD COLUMN IF NOT EXISTS images JSONB DEFAULT '[]'"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE video_products ADD COLUMN IF NOT EXISTS weight DOUBLE PRECISION"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE video_products ADD COLUMN IF NOT EXISTS length_cm DOUBLE PRECISION"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE video_products ADD COLUMN IF NOT EXISTS breadth_cm DOUBLE PRECISION"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE video_products ADD COLUMN IF NOT EXISTS height_cm DOUBLE PRECISION"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE shipments "
                "ADD COLUMN IF NOT EXISTS shipmozo_reference_id VARCHAR(100)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE products ADD COLUMN IF NOT EXISTS seo_title VARCHAR(200)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE products "
                "ADD COLUMN IF NOT EXISTS seo_description VARCHAR(320)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE categories ADD COLUMN IF NOT EXISTS seo_title VARCHAR(200)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE categories "
                "ADD COLUMN IF NOT EXISTS seo_description VARCHAR(320)"
            )
        )
        # One-time, deployment-safe cleanup for databases created by older releases.
        # Preserve each product's parent category before removing the legacy schema.
        await conn.execute(
            text(
                """
                DO $$
                BEGIN
                    IF to_regclass('public.subcategories') IS NOT NULL
                       AND EXISTS (
                           SELECT 1
                           FROM information_schema.columns
                           WHERE table_schema = 'public'
                             AND table_name = 'products'
                             AND column_name = 'subcategory_id'
                       )
                    THEN
                        EXECUTE '
                            UPDATE products AS p
                            SET category_id = s.category_id
                            FROM subcategories AS s
                            WHERE p.category_id IS NULL
                              AND p.subcategory_id = s.id
                        ';
                    END IF;

                    IF to_regclass('public.product_subcategories') IS NOT NULL
                       AND to_regclass('public.subcategories') IS NOT NULL
                    THEN
                        EXECUTE '
                            UPDATE products AS p
                            SET category_id = mapped.category_id
                            FROM (
                                SELECT ps.product_id, MIN(s.category_id) AS category_id
                                FROM product_subcategories AS ps
                                JOIN subcategories AS s ON s.id = ps.subcategory_id
                                GROUP BY ps.product_id
                            ) AS mapped
                            WHERE p.id = mapped.product_id
                              AND p.category_id IS NULL
                        ';
                    END IF;
                END
                $$;
                """
            )
        )
        await conn.execute(text("DROP TABLE IF EXISTS product_subcategories"))
        await conn.execute(
            text("ALTER TABLE products DROP COLUMN IF EXISTS subcategory_id")
        )
        await conn.execute(text("DROP TABLE IF EXISTS subcategories"))
        await conn.execute(
            text(
                """
                UPDATE products AS p
                SET category = c.name
                FROM categories AS c
                WHERE p.category_id = c.id
                  AND p.category IS DISTINCT FROM c.name
                """
            )
        )

        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS product_categories (
                    product_id INTEGER NOT NULL
                        REFERENCES products(id) ON DELETE CASCADE,
                    category_id INTEGER NOT NULL
                        REFERENCES categories(id) ON DELETE CASCADE,
                    PRIMARY KEY (product_id, category_id)
                )
                """
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_product_categories_category_id "
                "ON product_categories(category_id)"
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO product_categories (product_id, category_id)
                SELECT p.id, p.category_id
                FROM products AS p
                WHERE p.category_id IS NOT NULL
                ON CONFLICT DO NOTHING
                """
            )
        )

        await conn.execute(
            text(
                "ALTER TABLE categories ADD COLUMN IF NOT EXISTS is_reels BOOLEAN DEFAULT FALSE"
            )
        )

    await seed_admin()
    await ensure_reels_category()
    print("PostgreSQL connected and tables ready")


async def disconnect_db():
    await engine.dispose()
    print("PostgreSQL disconnected")


async def seed_admin():
    from sqlalchemy import select

    from app.models import Admin

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Admin).where(Admin.email == settings.ADMIN_EMAIL)
        )
        existing = result.scalar_one_or_none()
        hashed = bcrypt.hashpw(
            settings.ADMIN_PASSWORD.encode(), bcrypt.gensalt()
        ).decode()
        notify_phone = (settings.ADMIN_NOTIFY_PHONE or "").strip() or None
        brand_name = (settings.BRAND_NAME or "").strip() or None
        if not existing:
            session.add(
                Admin(
                    email=settings.ADMIN_EMAIL,
                    password=hashed,
                    name=settings.ADMIN_USERNAME,
                    phone=notify_phone,
                    company_name=brand_name,
                    role="admin",
                    is_active=True,
                )
            )
            print(f"Admin seeded: {settings.ADMIN_USERNAME}")
        else:
            existing.password = hashed
            existing.name = settings.ADMIN_USERNAME
            # Keep locked brand notify fields in sync with env
            if notify_phone:
                existing.phone = notify_phone
            if brand_name:
                existing.company_name = brand_name
            print(f"Admin password synced: {settings.ADMIN_USERNAME}")
        await session.commit()


async def ensure_reels_category():
    """Ensure the special Reels category exists for admin + storefront."""
    from sqlalchemy import select

    from app.models import Category

    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(Category).where(
                    (Category.is_reels == True) | (Category.slug == "reels")  # noqa: E712
                )
            )
        ).scalar_one_or_none()

        if existing:
            if not existing.is_reels:
                existing.is_reels = True
            if existing.slug != "reels":
                existing.slug = "reels"
            await session.commit()
            return

        session.add(
            Category(
                name="Reels",
                slug="reels",
                description="Watch product videos and shop from Reels",
                is_active=True,
                position=2,
                is_reels=True,
            )
        )
        await session.commit()
        print("Reels category seeded")


async def seed_default_categories():
    """Seed default chair categories and map existing products."""
    import re

    from sqlalchemy import select, update

    from app.models import Category, Product

    def _slugify(name: str) -> str:
        s = name.lower().strip()
        s = re.sub(r"[^\w\s-]", "", s)
        s = re.sub(r"[\s_-]+", "-", s)
        return s

    defaults = [
        {"name": "Plastic Chairs", "position": 0},
        {"name": "Arm Chairs", "position": 1},
        {"name": "Armless Chairs", "position": 2},
        {"name": "Dining Chairs", "position": 3},
        {"name": "Garden Chairs", "position": 4},
        {"name": "Office Chairs", "position": 5},
        {"name": "Kids Chairs", "position": 6},
        {"name": "Plastic Stools", "position": 7},
    ]

    async with AsyncSessionLocal() as session:
        for cat_data in defaults:
            existing = (
                await session.execute(
                    select(Category).where(Category.name == cat_data["name"])
                )
            ).scalar_one_or_none()
            if not existing:
                session.add(
                    Category(
                        name=cat_data["name"],
                        slug=_slugify(cat_data["name"]),
                        is_active=True,
                        position=cat_data["position"],
                    )
                )

        await session.commit()

        result = await session.execute(select(Category))
        for cat in result.scalars().all():
            await session.execute(
                update(Product)
                .where(Product.category.ilike(cat.name))
                .where(Product.category_id.is_(None))
                .values(category_id=cat.id, category=cat.name)
            )

        await session.commit()
        print("Default categories seeded")
