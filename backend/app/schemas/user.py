import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.app.db.models import UserRole


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=160)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str | None
    role: UserRole
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UserList(BaseModel):
    items: list[UserRead]
    page: int
    page_size: int
    total: int


class UserUpdate(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=160)
