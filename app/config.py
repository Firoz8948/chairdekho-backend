from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    APP_NAME: str = "ChairDekho"
    ENVIRONMENT: str = "development"
    API_V1_PREFIX: str = "/api/v1"
    FRONTEND_URL: str = "http://localhost:3000"
    # Public API base for PayU surl/furl (e.g. https://api.chairdekho.in/api/v1)
    API_PUBLIC_URL: str = ""
    # Comma-separated extra origins (e.g. preview URL + custom domain)
    CORS_ORIGINS: str = ""

    DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/chairdekho"
    )

    JWT_SECRET_KEY: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    OTP_EXPIRE_MINUTES: int = 10
    OTP_LENGTH: int = 4
    OTP_DEBUG: bool = True

    RENFLAIR_API_KEY: str = ""

    EMAILJS_SERVICE_ID: str = ""
    EMAILJS_TEMPLATE_ID: str = ""
    EMAILJS_PUBLIC_KEY: str = ""

    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""

    ADMIN_EMAIL: str = "admin@chairdekho.in"
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin"
    # Locked storefront / notification identity (shown in Admin → Settings)
    BRAND_NAME: str = "ChairDekho"
    ADMIN_NOTIFY_PHONE: str = "9699164131"
    ADMIN_NOTIFY_EMAIL: str = "brjangu29@gmail.com"

    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # PayU Biz (preferred online checkout when configured)
    PAYU_KEY: str = ""
    PAYU_SALT: str = ""
    # live | test
    PAYU_MODE: str = "live"

    SHIPROCKET_EMAIL: str = ""
    SHIPROCKET_PASSWORD: str = ""
    SHIPROCKET_BASE_URL: str = "https://apiv2.shiprocket.in/v1/external"
    # Pickup nickname exactly as shown in Shiprocket → Settings → Pickup Addresses
    SHIPROCKET_PICKUP_LOCATION: str = "Primary"
    SHIPROCKET_CHANNEL_ID: str = ""
    SHIPROCKET_COURIER_ID: str = ""
    # Auto-create Shiprocket order when a store order is placed
    SHIPROCKET_AUTO_PUSH: bool = True
    # After create, try to assign AWB automatically (requires active courier wallet)
    SHIPROCKET_AUTO_AWB: bool = False
    SHIPROCKET_DEFAULT_LENGTH: float = 10
    SHIPROCKET_DEFAULT_BREADTH: float = 10
    SHIPROCKET_DEFAULT_HEIGHT: float = 10

    # Shipmozo (manual push from Admin → Orders)
    SHIPMOZO_BASE_URL: str = "https://shipping-api.com/app/api/v1"
    SHIPMOZO_PUBLIC_KEY: str = ""
    SHIPMOZO_PRIVATE_KEY: str = ""
    # Required warehouse id from Shipmozo Get Warehouses / Create Warehouse
    SHIPMOZO_WAREHOUSE_ID: str = ""
    SHIPMOZO_AUTO_ASSIGN: bool = True
    SHIPMOZO_DEFAULT_LENGTH: float = 10
    SHIPMOZO_DEFAULT_BREADTH: float = 10
    SHIPMOZO_DEFAULT_HEIGHT: float = 10

    # Delhivery One B2C (Express parcel) — manual push from Admin → Orders
    DELHIVERY_API_TOKEN: str = ""
    DELHIVERY_BASE_URL: str = "https://track.delhivery.com"
    # Warehouse name exactly as registered in Delhivery One → Settings → Pickup Locations
    DELHIVERY_PICKUP_LOCATION: str = ""
    # Surface | Express
    DELHIVERY_SHIPPING_MODE: str = "Surface"
    DELHIVERY_DEFAULT_WEIGHT_GRAMS: float = 500
    DELHIVERY_DEFAULT_LENGTH: float = 25
    DELHIVERY_DEFAULT_BREADTH: float = 20
    DELHIVERY_DEFAULT_HEIGHT: float = 8
    DELHIVERY_SELLER_GSTIN: str = ""
    DELHIVERY_HSN_CODE: str = "9401"

    # Media storage: "auto" uses BunnyCDN when configured, otherwise local disk
    STORAGE_BACKEND: str = "auto"
    # Local upload directory; relative paths resolve against the backend root
    UPLOAD_DIR: str = ""

    BUNNY_STORAGE_ZONE: str = ""
    BUNNY_STORAGE_API_KEY: str = ""
    BUNNY_STORAGE_REGION: str = "sg"
    BUNNY_CDN_URL: str = ""

    # Meta / Facebook Ads
    META_PIXEL_ID: str = ""
    META_ACCESS_TOKEN: str = ""
    META_TEST_EVENT_CODE: str = ""
    META_API_VERSION: str = "v21.0"

    # Google reviews (Places API (New)) — storefront rating + latest reviews
    GOOGLE_PLACES_API_KEY: str = ""
    # Optional; when empty the place is looked up once from GOOGLE_PLACE_QUERY
    GOOGLE_PLACE_ID: str = ""
    GOOGLE_PLACE_QUERY: str = (
        "Chairdekho, Umadevi Mandir, Bolinj Road, Nalasopara West, Vasai-Virar"
    )
    # Business Profile → "Ask for reviews" link; falls back to a writereview URL from the place id
    GOOGLE_REVIEW_URL: str = ""
    GOOGLE_REVIEWS_CACHE_HOURS: float = 12

    # One-click switch between this admin panel and the paired store's admin.
    # The same ADMIN_SWITCH_SECRET (32+ chars) must be set on both backends; empty disables it.
    ADMIN_SWITCH_SECRET: str = ""
    ADMIN_SWITCH_SITE: str = "chairdekho"
    ADMIN_SWITCH_TARGET: str = "chakladekho"
    ADMIN_SWITCH_TARGET_URL: str = "https://www.chakladekho.com"

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origins(self) -> list[str]:
        def _clean(origin: str) -> str:
            origin = (origin or "").strip().strip("\"'")
            return origin.rstrip("/")

        def _with_www_variants(origin: str) -> list[str]:
            cleaned = _clean(origin)
            if not cleaned:
                return []
            out = [cleaned]
            # Allow both apex and www for the same site
            try:
                from urllib.parse import urlparse

                parsed = urlparse(cleaned)
                host = parsed.hostname or ""
                if host and not host.startswith("www."):
                    out.append(
                        f"{parsed.scheme}://www.{host}"
                        + (f":{parsed.port}" if parsed.port else "")
                    )
                elif host.startswith("www."):
                    out.append(
                        f"{parsed.scheme}://{host[4:]}"
                        + (f":{parsed.port}" if parsed.port else "")
                    )
            except Exception:
                pass
            return out

        origins: set[str] = set()
        for o in _with_www_variants(self.FRONTEND_URL):
            origins.add(o)
        origins.update(
            {
                "http://localhost:3000",
                "http://127.0.0.1:3000",
                "http://localhost:3001",
                "http://127.0.0.1:3001",
            }
        )
        if self.CORS_ORIGINS:
            for origin in self.CORS_ORIGINS.split(","):
                for o in _with_www_variants(origin):
                    origins.add(o)
        return [origin for origin in origins if origin]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
