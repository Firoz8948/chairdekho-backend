"""One-click switch between this admin panel and the paired store's admin panel.

The source backend signs a one-time ticket (60 s) with ADMIN_SWITCH_SECRET; the
target backend redeems it for a normal admin access token. JWT secrets are never shared.
"""

import time
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin
from app.auth.utils import create_access_token
from app.common import serialize_admin, utcnow
from app.config import settings
from app.database import get_db
from app.models import Admin

router = APIRouter()

TICKET_TTL_SECONDS = 60
_ALGORITHM = "HS256"
_TICKET_TYPE = "admin_switch"
_used_tickets: dict[str, float] = {}


class RedeemRequest(BaseModel):
    ticket: str


def _secret() -> str:
    secret = (settings.ADMIN_SWITCH_SECRET or "").strip()
    if len(secret) < 32:
        raise HTTPException(status_code=503, detail="Admin switch is not configured")
    return secret


def _claim_ticket(jti: str, expires_at: float) -> bool:
    now = time.time()
    for key, exp in list(_used_tickets.items()):
        if exp < now:
            _used_tickets.pop(key, None)
    if jti in _used_tickets:
        return False
    _used_tickets[jti] = expires_at
    return True


@router.post("/ticket")
async def create_ticket(admin=Depends(get_current_admin)):
    secret = _secret()
    email = (admin.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Admin account has no email")

    now = utcnow()
    ticket = jwt.encode(
        {
            "typ": _TICKET_TYPE,
            "iss": settings.ADMIN_SWITCH_SITE,
            "aud": settings.ADMIN_SWITCH_TARGET,
            "email": email,
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + timedelta(seconds=TICKET_TTL_SECONDS),
        },
        secret,
        algorithm=_ALGORITHM,
    )
    target = settings.ADMIN_SWITCH_TARGET_URL.rstrip("/")
    # Fragment, not query string: never sent to servers or written to access logs
    return {"redirect_url": f"{target}/admin/switch#ticket={ticket}"}


@router.post("/redeem")
async def redeem_ticket(body: RedeemRequest, db: AsyncSession = Depends(get_db)):
    secret = _secret()
    invalid = HTTPException(
        status_code=400, detail="This switch link is invalid or has expired. Please log in."
    )
    try:
        claims = jwt.decode(
            body.ticket,
            secret,
            algorithms=[_ALGORITHM],
            audience=settings.ADMIN_SWITCH_SITE,
            issuer=settings.ADMIN_SWITCH_TARGET,
        )
    except JWTError:
        raise invalid

    jti = claims.get("jti")
    if claims.get("typ") != _TICKET_TYPE or not jti:
        raise invalid
    if not _claim_ticket(jti, float(claims.get("exp", 0))):
        raise invalid

    email = (claims.get("email") or "").strip().lower()
    admin = None
    for lookup in (email, (settings.ADMIN_EMAIL or "").strip().lower()):
        if not lookup:
            continue
        result = await db.execute(
            select(Admin).where(func.lower(Admin.email) == lookup, Admin.is_active == True)  # noqa: E712
        )
        admin = result.scalars().first()
        if admin:
            break
    if not admin:
        raise HTTPException(status_code=403, detail="No matching admin account on this store")

    admin_data = serialize_admin(admin)
    token = create_access_token(
        {
            "sub": admin_data["id"],
            "email": admin_data["email"],
            "role": admin_data.get("role", "admin"),
        }
    )
    return {"access_token": token, "token_type": "bearer", "admin": admin_data}
