import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from backend.app.api.dependencies import require_admin
from backend.app.db.session import get_db
from backend.app.schemas.region import (
    RegionCreate,
    RegionList,
    RegionLookupResponse,
    RegionRead,
    RegionUpdate,
)
from backend.app.services import regions as region_service


router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("", response_model=RegionList)
def list_regions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    include_inactive: bool = False,
    db: Session = Depends(get_db),
):
    items, total = region_service.list_regions(
        db, page, page_size, include_inactive
    )
    return RegionList(items=items, page=page, page_size=page_size, total=total)


@router.post("", response_model=RegionRead, status_code=status.HTTP_201_CREATED)
def create_region(payload: RegionCreate, db: Session = Depends(get_db)):
    return region_service.create_region(db, payload)


@router.get("/lookup", response_model=RegionLookupResponse)
def lookup_region(q: str = Query(min_length=1, max_length=200)):
    return region_service.lookup_region(q)


@router.get("/{region_id}", response_model=RegionRead)
def get_region(region_id: uuid.UUID, db: Session = Depends(get_db)):
    return region_service.get_region_or_404(db, region_id)


@router.patch("/{region_id}", response_model=RegionRead)
def update_region(
    region_id: uuid.UUID,
    payload: RegionUpdate,
    db: Session = Depends(get_db),
):
    return region_service.update_region(db, region_id, payload)


@router.delete("/{region_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_region(region_id: uuid.UUID, db: Session = Depends(get_db)):
    region_service.delete_region(db, region_id)
    return None
