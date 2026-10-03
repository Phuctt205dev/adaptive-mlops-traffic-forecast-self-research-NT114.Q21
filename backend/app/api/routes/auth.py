from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from backend.app.api.dependencies import get_current_user
from backend.app.core.config import get_settings
from backend.app.core.rate_limit import check_rate_limit
from backend.app.core.security import create_access_token
from backend.app.db.models import User
from backend.app.db.session import get_db
from backend.app.schemas.user import (
    LoginRequest,
    PasswordChangeRequest,
    ProfileUpdate,
    TokenResponse,
    UserCreate,
    UserRead,
)
from backend.app.services import users as user_service


router = APIRouter()


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def register(
    payload: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    settings = get_settings()
    check_rate_limit(
        f"register:{request.client.host if request.client else 'unknown'}",
        settings.auth_rate_limit_per_minute,
        60,
    )
    return user_service.create_user(db, payload)


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    settings = get_settings()
    check_rate_limit(
        f"login:{request.client.host if request.client else 'unknown'}",
        settings.auth_rate_limit_per_minute,
        60,
    )
    user = user_service.authenticate_user(db, payload.email, payload.password)
    return TokenResponse(
        access_token=create_access_token(user.id, user.role.value),
    )


@router.get("/me", response_model=UserRead)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=UserRead)
def update_me(
    payload: ProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return user_service.update_profile(db, current_user, payload.display_name)


@router.patch("/password", response_model=UserRead)
def change_password(
    payload: PasswordChangeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return user_service.change_password(
        db,
        current_user,
        payload.current_password,
        payload.new_password,
    )
