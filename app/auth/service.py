from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import serialize_user
from app.models import User

from .utils import create_access_token, create_refresh_token, decode_token, verify_password

__all__ = [
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "verify_password",
    "get_or_create_user",
    "get_or_create_user_by_phone",
    "get_user_by_id",
    "get_user_by_email",
    "get_user_by_phone",
    "update_user_profile",
    "get_current_user_profile",
    "refresh_access_token",
]


async def get_or_create_user_by_phone(
    db: AsyncSession,
    phone: str,
    name: str = "",
    *,
    allow_create: bool = True,
) -> dict:
    result = await db.execute(select(User).where(User.phone == phone))
    user = result.scalar_one_or_none()

    if not user:
        if not allow_create:
            raise HTTPException(
                status_code=404,
                detail="ACCOUNT_NOT_FOUND",
            )
        derived_name = name.strip() or f"Customer {phone[-4:]}"
        user = User(
            phone=phone,
            email=f"{phone}@mobile.chairdekho.local",
            name=derived_name,
            role="customer",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    elif name.strip() and (not user.name or user.name.startswith("Customer ")):
        user.name = name.strip()
        await db.commit()
        await db.refresh(user)

    return serialize_user(user)


async def get_or_create_user(
    db: AsyncSession,
    email: str,
    name: str = "",
) -> dict:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user:
        derived_name = name.strip() or email.split("@")[0].replace(".", " ").title()
        user = User(
            email=email,
            name=derived_name,
            role="customer",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    return serialize_user(user)


async def get_user_by_id(db: AsyncSession, user_id: int | str) -> Optional[dict]:
    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()
    return serialize_user(user) if user else None


async def get_user_by_phone(db: AsyncSession, phone: str) -> Optional[dict]:
    result = await db.execute(select(User).where(User.phone == phone))
    user = result.scalar_one_or_none()
    return serialize_user(user) if user else None


async def get_user_by_email(db: AsyncSession, email: str) -> Optional[dict]:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    return serialize_user(user) if user else None


async def update_user_profile(
    db: AsyncSession,
    user_id: int | str,
    name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    date_of_birth: str | None = None,
    address_line1: str | None = None,
    address_line2: str | None = None,
    address_landmark: str | None = None,
    address_city: str | None = None,
    address_state: str | None = None,
    address_pincode: str | None = None,
) -> Optional[dict]:
    from datetime import date as date_cls

    result = await db.execute(select(User).where(User.id == int(user_id)))
    user = result.scalar_one_or_none()
    if not user:
        return None

    if name is not None:
        user.name = name.strip() or None
    if phone is not None:
        user.phone = phone
    if email is not None:
        cleaned = email.strip().lower()
        if cleaned and "@mobile." in cleaned:
            cleaned = ""
        user.email = cleaned or None
    if date_of_birth is not None:
        if date_of_birth == "":
            user.date_of_birth = None
        else:
            user.date_of_birth = date_cls.fromisoformat(date_of_birth[:10])
    if address_line1 is not None:
        user.address_line1 = address_line1.strip() or None
    if address_line2 is not None:
        user.address_line2 = address_line2.strip() or None
    if address_landmark is not None:
        user.address_landmark = address_landmark.strip() or None
    if address_city is not None:
        user.address_city = address_city.strip() or None
    if address_state is not None:
        user.address_state = address_state.strip() or None
    if address_pincode is not None:
        pin = "".join(ch for ch in address_pincode if ch.isdigit())
        user.address_pincode = pin or None

    await db.commit()
    await db.refresh(user)
    return serialize_user(user)


async def get_current_user_profile(user_id: str, db: AsyncSession) -> dict:
    user = await get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def refresh_access_token(db: AsyncSession, refresh_token: str) -> dict:
    data = decode_token(refresh_token)
    if not data or data.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user = await get_user_by_id(db, data["sub"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.get("is_active"):
        raise HTTPException(status_code=403, detail="Account is deactivated")

    token_data = {
        "sub": str(user["id"]),
        "phone": user.get("phone"),
        "email": user.get("email"),
        "role": user["role"],
    }
    return {
        "access_token": create_access_token(token_data),
        "token_type": "bearer",
    }
