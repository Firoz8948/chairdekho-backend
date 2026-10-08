"""One-off: seed / replace categories with ChairDekho chair categories via SQL."""
import asyncio
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

NEW = [
    ("Plastic Chairs", "plastic-chairs", 0),
    ("Arm Chairs", "arm-chairs", 1),
    ("Armless Chairs", "armless-chairs", 2),
    ("Dining Chairs", "dining-chairs", 3),
    ("Garden Chairs", "garden-chairs", 4),
    ("Office Chairs", "office-chairs", 5),
    ("Kids Chairs", "kids-chairs", 6),
    ("Plastic Stools", "plastic-stools", 7),
]


async def main():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("DATABASE_URL not set")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    parsed = urlparse(url)
    conn = await asyncpg.connect(
        host=parsed.hostname or "localhost",
        port=parsed.port or 5432,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=(parsed.path or "/").lstrip("/") or "postgres",
    )
    try:
        cols = await conn.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'categories'
            """
        )
        col_names = {r["column_name"] for r in cols}
        if not col_names:
            raise SystemExit("categories table not found")

        await conn.execute("DELETE FROM categories")
        for name, slug, position in NEW:
            if "position" in col_names and "is_active" in col_names:
                await conn.execute(
                    """
                    INSERT INTO categories (name, slug, position, is_active)
                    VALUES ($1, $2, $3, true)
                    """,
                    name,
                    slug,
                    position,
                )
            elif "position" in col_names:
                await conn.execute(
                    """
                    INSERT INTO categories (name, slug, position)
                    VALUES ($1, $2, $3)
                    """,
                    name,
                    slug,
                    position,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO categories (name, slug)
                    VALUES ($1, $2)
                    """,
                    name,
                    slug,
                )
        print(f"Seeded {len(NEW)} ChairDekho categories")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
