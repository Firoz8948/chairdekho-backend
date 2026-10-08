from fastapi import APIRouter

from app.reviews import service

router = APIRouter()


@router.get("/google")
async def google_reviews():
    return await service.get_google_reviews()
