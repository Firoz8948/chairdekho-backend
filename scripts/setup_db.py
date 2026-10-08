"""
Create PostgreSQL role + database, then run table migrations (create_all) + admin seed.

Usage:
  set POSTGRES_ADMIN_PASSWORD=your_postgres_superuser_password
  python scripts/setup_db.py

Or:
  python scripts/setup_db.py --admin-password YOUR_POSTGRES_PASSWORD
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

APP_USER = "postgres"
APP_PASSWORD = "postgres_password"
APP_DB = "chairdekho"


def _quote_literal(value: str) -> str:
    return value.replace("'", "''")


async def ensure_role_and_database(admin_password: str) -> None:
    conn = await asyncpg.connect(
        host="localhost",
        port=5432,
        user="postgres",
        password=admin_password,
        database="postgres",
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = $1", APP_USER
        )
        pwd = _quote_literal(APP_PASSWORD)
        if not exists:
            await conn.execute(
                f"CREATE ROLE {APP_USER} WITH LOGIN PASSWORD '{pwd}'"
            )
            print(f"Created role: {APP_USER}")
        else:
            await conn.execute(
                f"ALTER ROLE {APP_USER} WITH LOGIN PASSWORD '{pwd}'"
            )
            print(f"Updated role password: {APP_USER}")

        db_exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", APP_DB
        )
        if not db_exists:
            await conn.execute(f'CREATE DATABASE "{APP_DB}" OWNER {APP_USER}')
            print(f"Created database: {APP_DB}")
        else:
            print(f"Database already exists: {APP_DB}")

        await conn.execute(f"GRANT ALL PRIVILEGES ON DATABASE {APP_DB} TO {APP_USER}")
    finally:
        await conn.close()


async def init_tables() -> None:
    from app.database import connect_db, disconnect_db

    await connect_db()
    await disconnect_db()


async def verify_app_connection() -> None:
    conn = await asyncpg.connect(
        host="localhost",
        port=5432,
        user=APP_USER,
        password=APP_PASSWORD,
        database=APP_DB,
    )
    try:
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        print(f"Connected as {APP_USER}. Tables ({len(tables)}):")
        for row in tables:
            print(f"  - {row['tablename']}")
    finally:
        await conn.close()


async def main(admin_password: str) -> None:
    print("Step 1/3: Creating role and database...")
    await ensure_role_and_database(admin_password)
    print("Step 2/3: Creating tables and seeding admin...")
    await init_tables()
    print("Step 3/3: Verifying connection...")
    await verify_app_connection()
    print("\nPostgreSQL is ready.")
    print(
        "DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/chairdekho"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-password", default=os.getenv("POSTGRES_ADMIN_PASSWORD"))
    args = parser.parse_args()
    if not args.admin_password:
        print("Set POSTGRES_ADMIN_PASSWORD or pass --admin-password")
        sys.exit(1)
    asyncio.run(main(args.admin_password))
