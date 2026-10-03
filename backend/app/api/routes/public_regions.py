from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.api.dependencies import get_current_user
from backend.app.db.models import User
from backend.app.db.session import get_db
from backend.app.schemas.region import RegionList
from backend.app.services import regions as region_service


router = APIRouter(dependencies=[Depends(get_current_user)])


@router.get("/regions", response_model=RegionList)
def list_active_regions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    items, total = region_service.list_regions(
        db,
        page,
        page_size,
        include_inactive=False,
    )
    return RegionList(items=items, page=page, page_size=page_size, total=total)
