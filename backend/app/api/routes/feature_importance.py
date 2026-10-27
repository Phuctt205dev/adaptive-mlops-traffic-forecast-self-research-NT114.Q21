import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.api.dependencies import require_admin
from backend.app.db.session import get_db
from backend.app.schemas.feature_importance import FeatureImportanceReport
from backend.app.services.feature_importance import get_feature_importance


router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/regions/{region_id}/feature-importance", response_model=FeatureImportanceReport)
def read_feature_importance(region_id: uuid.UUID, model_version_id: uuid.UUID | None = None,
                            db: Session = Depends(get_db)):
    return get_feature_importance(db, region_id, expected_model_id=model_version_id)
