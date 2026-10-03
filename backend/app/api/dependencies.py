import uuid

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.app.core.errors import ApplicationError
from backend.app.core.security import decode_access_token
from backend.app.db.models import User, UserRole
from backend.app.db.session import get_db


bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApplicationError("not_authenticated", "Authentication is required.", 401)
    payload = decode_access_token(credentials.credentials)
    try:
        user_id = uuid.UUID(str(payload.get("sub")))
    except (TypeError, ValueError) as error:
        raise ApplicationError("invalid_token", "Invalid access token.", 401) from error
    user = db.get(User, user_id)
    if user is None:
        raise ApplicationError("invalid_token", "Invalid access token.", 401)
    if not user.is_active:
        raise ApplicationError("inactive_user", "User account is inactive.", 403)
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.ADMIN:
        raise ApplicationError(
            "admin_required",
            "Admin role is required for this action.",
            403,
        )
    return current_user
