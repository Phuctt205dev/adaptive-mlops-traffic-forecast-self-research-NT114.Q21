import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.errors import ApplicationError
from backend.app.core.security import hash_password, verify_password
from backend.app.db.models import User, UserRole
from backend.app.schemas.user import UserCreate, UserUpdate


def normalize_email(email: str) -> str:
    return email.strip().lower()


def list_users(db: Session, page: int, page_size: int) -> tuple[list[User], int]:
    total = db.scalar(select(func.count()).select_from(User)) or 0
    users = list(
        db.scalars(
            select(User)
            .order_by(User.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return users, total


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == normalize_email(email)))


def get_user_or_404(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise ApplicationError("user_not_found", "User was not found.", 404)
    return user


def create_user(
    db: Session,
    payload: UserCreate,
    role: UserRole = UserRole.USER,
) -> User:
    user = User(
        email=normalize_email(payload.email),
        display_name=(payload.display_name.strip() if payload.display_name else None),
        password_hash=hash_password(payload.password),
        role=role,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise ApplicationError(
            "email_conflict",
            "A user with this email already exists.",
            409,
        ) from error
    db.refresh(user)
    return user


def authenticate_user(db: Session, email: str, password: str) -> User:
    user = get_user_by_email(db, email)
    if user is None or not verify_password(password, user.password_hash):
        raise ApplicationError("invalid_credentials", "Invalid email or password.", 401)
    if not user.is_active:
        raise ApplicationError("inactive_user", "User account is inactive.", 403)
    user.last_login_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    return user


def update_user(db: Session, user_id: uuid.UUID, payload: UserUpdate) -> User:
    user = get_user_or_404(db, user_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return user


def update_profile(db: Session, user: User, display_name: str | None) -> User:
    user.display_name = display_name.strip() if display_name else None
    db.commit()
    db.refresh(user)
    return user


def change_password(
    db: Session,
    user: User,
    current_password: str,
    new_password: str,
) -> User:
    if not verify_password(current_password, user.password_hash):
        raise ApplicationError(
            "invalid_current_password",
            "Current password is incorrect.",
            400,
        )
    user.password_hash = hash_password(new_password)
    db.commit()
    db.refresh(user)
    return user


def ensure_bootstrap_admin(db: Session, email: str, password: str) -> None:
    normalized_email = normalize_email(email)
    if not normalized_email or not password:
        return
    existing = get_user_by_email(db, normalized_email)
    if existing is not None:
        if existing.role != UserRole.ADMIN:
            existing.role = UserRole.ADMIN
            db.commit()
        return
    create_user(
        db,
        UserCreate(email=normalized_email, password=password),
        role=UserRole.ADMIN,
    )
