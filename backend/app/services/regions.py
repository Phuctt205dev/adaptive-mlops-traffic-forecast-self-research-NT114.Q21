import uuid
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.errors import ApplicationError
from backend.app.core.config import get_settings
from backend.app.db.models import ModelVersion, Region
from backend.app.schemas.region import RegionCreate, RegionUpdate


def _attach_active_model_details(db: Session, regions: list[Region]) -> None:
    active_model_ids = [region.active_model_version_id for region in regions if region.active_model_version_id]
    if not active_model_ids:
        return

    model_versions = {
        model.id: model
        for model in db.scalars(select(ModelVersion).where(ModelVersion.id.in_(active_model_ids)))
    }
    for region in regions:
        model = model_versions.get(region.active_model_version_id)
        setattr(region, "active_model_version", model.version if model else None)
        setattr(region, "active_model_variant", model.variant if model else None)


def list_regions(
    db: Session,
    page: int,
    page_size: int,
    include_inactive: bool,
) -> tuple[list[Region], int]:
    filters = [] if include_inactive else [Region.is_active.is_(True)]
    total = db.scalar(select(func.count()).select_from(Region).where(*filters)) or 0
    regions = list(
        db.scalars(
            select(Region)
            .where(*filters)
            .order_by(Region.name)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    _attach_active_model_details(db, regions)
    return regions, total


def get_region_or_404(db: Session, region_id: uuid.UUID) -> Region:
    region = db.get(Region, region_id)
    if region is None:
        raise ApplicationError("region_not_found", "Region was not found.", 404)
    _attach_active_model_details(db, [region])
    return region


def create_region(db: Session, payload: RegionCreate) -> Region:
    region = Region(**payload.model_dump())
    db.add(region)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise ApplicationError(
            "region_name_conflict",
            "A region with this name already exists.",
            409,
        ) from error
    db.refresh(region)
    _attach_active_model_details(db, [region])
    return region


def update_region(
    db: Session,
    region_id: uuid.UUID,
    payload: RegionUpdate,
) -> Region:
    region = get_region_or_404(db, region_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(region, field, value)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise ApplicationError(
            "region_name_conflict",
            "A region with this name already exists.",
            409,
        ) from error
    db.refresh(region)
    _attach_active_model_details(db, [region])
    return region


def delete_region(db: Session, region_id: uuid.UUID) -> None:
    from backend.app.services.region_cleanup import delete_region as cleanup_region
    cleanup_region(db, region_id)


def lookup_region(query: str) -> dict:
    query = query.strip()
    if not query:
        raise ApplicationError("empty_region_lookup", "Search query is required.", 400)

    settings = get_settings()
    headers = {"User-Agent": f"{settings.app_name}/1.0"}
    try:
        with httpx.Client(timeout=8.0, headers=headers) as client:
            geocode_response = client.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": query,
                    "format": "jsonv2",
                    "addressdetails": 1,
                    "limit": 1,
                },
            )
            geocode_response.raise_for_status()
            geocode_items = geocode_response.json()
            if not geocode_items:
                raise ApplicationError(
                    "region_lookup_not_found",
                    "No matching location was found.",
                    404,
                )

            item = geocode_items[0]
            latitude = Decimal(str(item["lat"])).quantize(Decimal("0.000001"))
            longitude = Decimal(str(item["lon"])).quantize(Decimal("0.000001"))
            timezone_response = client.get(
                "https://timeapi.io/api/TimeZone/coordinate",
                params={
                    "latitude": str(latitude),
                    "longitude": str(longitude),
                },
            )
            timezone_response.raise_for_status()
            timezone_payload = timezone_response.json()
    except ApplicationError:
        raise
    except Exception as error:
        raise ApplicationError(
            "region_lookup_failed",
            "Could not look up the selected location.",
            502,
        ) from error

    address = item.get("address") or {}
    country_code = (address.get("country_code") or "").lower()
    timezone = timezone_payload.get("timeZone") or timezone_payload.get("timezone")
    if country_code == "vn":
        timezone = "Asia/Ho_Chi_Minh"
    if not timezone:
        raise ApplicationError(
            "timezone_lookup_failed",
            "Could not resolve timezone for the selected location.",
            502,
        )

    name = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality")
        or item.get("name")
        or query
    )

    return {
        "name": name,
        "display_name": item.get("display_name") or name,
        "latitude": latitude,
        "longitude": longitude,
        "timezone": timezone,
        "provider": "OpenStreetMap Nominatim + TimeAPI.io",
    }
