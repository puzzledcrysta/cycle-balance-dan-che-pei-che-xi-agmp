from fastapi import APIRouter
from app.inputs import HandBack
from .service import board, sign, hand_back

router = APIRouter()


@router.get('/api/allocate/{run_id}')
def sheets(run_id: str):
    return board(run_id)


@router.post('/api/allocate/{run_id}/{district}/sign')
def commit(run_id: str, district: str):
    return sign(run_id, district)


@router.post('/api/allocate/{run_id}/{district}/return')
def release(run_id: str, district: str, payload: HandBack | None = None):
    return hand_back(run_id, district, payload.note if payload else '')
