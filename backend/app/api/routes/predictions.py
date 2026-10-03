import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from backend.app.api.dependencies import get_current_user
from backend.app.core.config import get_settings
from backend.app.core.rate_limit import check_rate_limit
from backend.app.db.models import User
from backend.app.db.session import get_db
from backend.app.schemas.prediction import (
    ForecastDashboardResponse,
    PredictionCreate,
    PredictionResponse,
    PredictionWindowResponse,
)
from backend.app.services.predictions import (
    get_forecast_dashboard,
    get_prediction_window,
    predict_for_region,
)


router = APIRouter()


@router.get(
    "/regions/{region_id}/prediction-window",
    response_model=PredictionWindowResponse,
)
def read_prediction_window(
    region_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_prediction_window(db, region_id)


@router.get(
    "/regions/{region_id}/forecast-dashboard",
    response_model=ForecastDashboardResponse,
)
def read_forecast_dashboard(
    region_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_forecast_dashboard(db, region_id)


@router.post("/regions/{region_id}/predictions", response_model=PredictionResponse)
def create_prediction(
    region_id: uuid.UUID,
    payload: PredictionCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    check_rate_limit(
        f"predict:{current_user.id}:{request.client.host if request.client else 'unknown'}",
        settings.prediction_rate_limit_per_minute,
        60,
    )
    prediction, model_version = predict_for_region(
        db,
        region_id,
        payload,
        current_user,
    )
    return PredictionResponse(
        prediction=prediction,
        model_version=model_version.version,
        model_variant=model_version.variant,
        active_model_version_id=model_version.id,
    )
