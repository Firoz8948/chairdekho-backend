from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service as auth_service
from app.config import settings
from app.database import get_db
from app.otp import send_otp as send_otp_service
from app.otp import verify_otp as verify_otp_service
from app.otp.schemas import SendOTPRequest, SendOTPResponse, VerifyOTPRequest
from app.sms.utils import normalize_phone

router = APIRouter(prefix="/otp", tags=["OTP"])


@router.post("/send", response_model=SendOTPResponse)
async def send_otp(
    body: SendOTPRequest,
    db: AsyncSession = Depends(get_db),
):
    mode = (body.mode or "signin").strip().lower()
    phone = normalize_phone(body.phone)

    if mode == "signup" and not (body.name or "").strip():
        raise HTTPException(status_code=400, detail="Please enter your name to sign up.")

    if mode != "auto":
        existing = await auth_service.get_user_by_phone(db, phone)
        if mode == "signin" and not existing:
            raise HTTPException(
                status_code=404,
                detail="ACCOUNT_NOT_FOUND",
            )
        if mode == "signup" and existing:
            raise HTTPException(
                status_code=400,
                detail="ACCOUNT_EXISTS",
            )

    return await send_otp_service.send_otp_to_phone(db, body.phone, body.name or "")


@router.post("/verify")
async def verify_otp(
    body: VerifyOTPRequest,
    db: AsyncSession = Depends(get_db),
):
    phone = normalize_phone(body.phone)
    otp_code = body.otp.strip()
    mode = (body.mode or "signin").strip().lower()
    name = (body.name or "").strip()

    if len(otp_code) != settings.OTP_LENGTH or not otp_code.isdigit():
        raise HTTPException(
            status_code=400,
            detail=f"OTP must be {settings.OTP_LENGTH} digits.",
        )

    if mode == "signup" and not name:
        raise HTTPException(status_code=400, detail="Please enter your name to sign up.")

    success, message = await verify_otp_service.verify_otp_for_phone(
        db, phone, otp_code
    )
    if not success:
        raise HTTPException(status_code=400, detail=message)

    existing = await auth_service.get_user_by_phone(db, phone)
    if mode == "signup" and existing:
        raise HTTPException(status_code=400, detail="ACCOUNT_EXISTS")
    if mode == "signin" and not existing:
        raise HTTPException(status_code=404, detail="ACCOUNT_NOT_FOUND")
    # New number in auto mode: keep the OTP valid so the client can resubmit it with a name.
    if mode == "auto" and not existing and not name:
        return {"needs_name": True, "message": "Please tell us your name to continue."}

    user = await auth_service.get_or_create_user_by_phone(
        db,
        phone,
        name,
        allow_create=(mode in ("signup", "auto")),
    )
    await verify_otp_service.consume_otps_for_phone(db, phone)

    token_data = {
        "sub": str(user["id"]),
        "phone": user.get("phone"),
        "email": user.get("email"),
        "role": user["role"],
    }
    access_token = auth_service.create_access_token(token_data)
    refresh_token = auth_service.create_refresh_token(token_data)

    response = JSONResponse(
        {
            "message": "Login successful.",
            "access_token": access_token,
            "token_type": "bearer",
            "user": user,
        }
    )

    secure = settings.ENVIRONMENT != "development"
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/",
    )
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )

    return response


@router.get("/status")
async def otp_status(phone: str, db: AsyncSession = Depends(get_db)):
    normalized = normalize_phone(phone)
    status = await verify_otp_service.get_otp_status(db, normalized)

    if not status:
        return {"has_valid_otp": False, "phone": normalized}

    return {
        "has_valid_otp": True,
        "phone": normalized,
        "expires_at": status["expires_at"],
        "created_at": status["created_at"],
    }
