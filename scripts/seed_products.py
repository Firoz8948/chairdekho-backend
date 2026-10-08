import asyncio
import sys
from pathlib import Path
import re

# Add backend to python path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import AsyncSessionLocal
from app.models import Product, Category, ProductImage
from sqlalchemy import select

# weight is in grams (see app.orders.models.total_cart_weight_grams)
PRODUCTS_DATA = [
    {
        "name": "Classic Plastic Arm Chair",
        "description": "Sturdy moulded plastic arm chair with a comfortable curved back. Stackable, easy to clean and ideal for homes, shops and events.",
        "price": 649.0,
        "mrp": 899.0,
        "category_name": "Arm Chairs",
        "stock": 100,
        "unit": "piece",
        "weight": 3200,
        "is_featured": True,
        "tags": ["arm chair", "plastic chair", "stackable"],
    },
    {
        "name": "Stackable Armless Plastic Chair",
        "description": "Lightweight armless plastic chair that stacks neatly to save space. Perfect for functions, classrooms and everyday seating.",
        "price": 449.0,
        "mrp": 649.0,
        "category_name": "Armless Chairs",
        "stock": 150,
        "unit": "piece",
        "weight": 2400,
        "is_featured": True,
        "tags": ["armless chair", "plastic chair", "stackable"],
    },
    {
        "name": "Plastic Dining Chair with Backrest",
        "description": "Modern plastic dining chair with a supportive backrest and anti-slip legs. Pairs well with any dining table.",
        "price": 799.0,
        "mrp": 1099.0,
        "category_name": "Dining Chairs",
        "stock": 80,
        "unit": "piece",
        "weight": 3000,
        "is_featured": True,
        "tags": ["dining chair", "plastic chair"],
    },
    {
        "name": "Weatherproof Garden Chair",
        "description": "UV-resistant outdoor plastic chair for gardens, balconies and terraces. Handles sun and rain with ease.",
        "price": 899.0,
        "mrp": 1299.0,
        "category_name": "Garden Chairs",
        "stock": 60,
        "unit": "piece",
        "weight": 3500,
        "is_featured": True,
        "tags": ["garden chair", "outdoor chair", "plastic chair"],
    },
    {
        "name": "Revolving Office Chair with Mesh Back",
        "description": "Ergonomic revolving office chair with breathable mesh back, height adjustment and smooth-rolling wheels.",
        "price": 3499.0,
        "mrp": 4999.0,
        "category_name": "Office Chairs",
        "stock": 30,
        "unit": "piece",
        "weight": 9000,
        "is_featured": True,
        "tags": ["office chair", "revolving chair", "mesh chair"],
    },
    {
        "name": "Kids Plastic Chair",
        "description": "Colourful, lightweight plastic chair sized for kids. Rounded edges and a stable base for safe everyday use.",
        "price": 299.0,
        "mrp": 449.0,
        "category_name": "Kids Chairs",
        "stock": 120,
        "unit": "piece",
        "weight": 1200,
        "is_featured": False,
        "tags": ["kids chair", "plastic chair"],
    },
    {
        "name": "Plastic Stool Medium Height",
        "description": "Strong, compact plastic stool for the kitchen, bathroom or shop counter. Stacks easily when not in use.",
        "price": 249.0,
        "mrp": 349.0,
        "category_name": "Plastic Stools",
        "stock": 200,
        "unit": "piece",
        "weight": 900,
        "is_featured": False,
        "tags": ["stool", "plastic stool"],
    },
]

def slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s

async def seed_products():
    async with AsyncSessionLocal() as session:
        for data in PRODUCTS_DATA:
            cat_result = await session.execute(
                select(Category).where(Category.name == data["category_name"])
            )
            category = cat_result.scalar_one_or_none()
            if not category:
                category = Category(
                    name=data["category_name"],
                    slug=slugify(data["category_name"]),
                    is_active=True,
                )
                session.add(category)
                await session.flush()

            existing = await session.execute(
                select(Product).where(Product.slug == slugify(data["name"]))
            )
            if existing.scalar_one_or_none():
                continue

            product = Product(
                name=data["name"],
                slug=slugify(data["name"]),
                description=data["description"],
                price=data["price"],
                mrp=data["mrp"],
                category=data["category_name"],
                category_id=category.id,
                stock=data["stock"],
                unit=data["unit"],
                weight=data["weight"],
                is_featured=data["is_featured"],
                is_active=True,
                tags=data["tags"],
            )
            session.add(product)
            await session.flush()

            if data.get("image_url"):
                session.add(
                    ProductImage(
                        product_id=product.id,
                        url=data["image_url"],
                        position=0,
                    )
                )

        await session.commit()
        print("ChairDekho sample products seeded")


if __name__ == "__main__":
    asyncio.run(seed_products())
