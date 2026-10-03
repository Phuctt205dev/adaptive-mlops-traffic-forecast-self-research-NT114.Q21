import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.api.dependencies import require_admin
from backend.app.db.models import User
from backend.app.db.session import get_db
from backend.app.schemas.user import UserList, UserRead, UserUpdate
from backend.app.services import users as user_service


router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("", response_model=UserList)
def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    items, total = user_service.list_users(db, page, page_size)
    return UserList(items=items, page=page, page_size=page_size, total=total)


@router.get("/{user_id}", response_model=UserRead)
def get_user(user_id: uuid.UUID, db: Session = Depends(get_db)):
    return user_service.get_user_or_404(db, user_id)


@router.patch("/{user_id}", response_model=UserRead)
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    return user_service.update_user(db, user_id, payload)
