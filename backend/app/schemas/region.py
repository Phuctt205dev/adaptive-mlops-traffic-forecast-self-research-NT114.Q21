import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class RegionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    latitude: Decimal = Field(ge=-90, le=90, decimal_places=6)
    longitude: Decimal = Field(ge=-180, le=180, decimal_places=6)
    timezone: str = Field(min_length=1, max_length=100)


class RegionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    latitude: Decimal | None = Field(default=None, ge=-90, le=90, decimal_places=6)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180, decimal_places=6)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    is_active: bool | None = None


class RegionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    latitude: Decimal
    longitude: Decimal
    timezone: str
    is_active: bool
    active_model_version_id: uuid.UUID | None
    active_model_version: str | None = None
    active_model_variant: str | None = None
    created_at: datetime
    updated_at: datetime


class RegionList(BaseModel):
    items: list[RegionRead]
    page: int
    page_size: int
    total: int


class RegionLookupResponse(BaseModel):
    name: str
    display_name: str
    latitude: Decimal
    longitude: Decimal
    timezone: str
    provider: str
