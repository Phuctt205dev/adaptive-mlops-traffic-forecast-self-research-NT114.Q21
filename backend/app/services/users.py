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


def _lock_active_admins(db: Session) -> list[User]:
    """Lock every active admin row in a fixed order.

    Two admins changing each other at the same time then wait for one another,
    so the check "another active admin remains" cannot be passed by both.
    """
    return list(
        db.scalars(
            select(User)
            .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
            .order_by(User.id)
            .with_for_update()
        )
    )


def update_user(db: Session, user_id: uuid.UUID, payload: UserUpdate) -> User:
    # A null value means "not provided"; it must never be written to a required column.
    changes = {
        field: value
        for field, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if "role" in changes or "is_active" in changes:
        active_admins = _lock_active_admins(db)
        user = get_user_or_404(db, user_id)
        db.refresh(user)
        stays_admin = (
            changes.get("role", user.role) == UserRole.ADMIN
            and changes.get("is_active", user.is_active)
        )
        was_active_admin = user.role == UserRole.ADMIN and user.is_active
        if was_active_admin and not stays_admin and not any(
            admin.id != user.id for admin in active_admins
        ):
            db.rollback()
            raise ApplicationError(
                "last_admin_required",
                "At least one active administrator must remain. "
                "Make another account an active administrator first.",
                409,
            )
    else:
        user = get_user_or_404(db, user_id)
    for field, value in changes.items():
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
    """Make sure the configured bootstrap account can administer a new system.

    A missing account is created as admin. An existing account is only
    promoted/re-enabled when no active admin is left, so that an admin who was
    deliberately demoted or disabled is not silently restored at the next start.
    """
    normalized_email = normalize_email(email)
    if not normalized_email or not password:
        return
    existing = get_user_by_email(db, normalized_email)
    if existing is None:
        create_user(
            db,
            UserCreate(email=normalized_email, password=password),
            role=UserRole.ADMIN,
        )
        return
    if existing.role == UserRole.ADMIN and existing.is_active:
        return
    if db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
    ):
        return
    existing.role = UserRole.ADMIN
    existing.is_active = True
    db.commit()
