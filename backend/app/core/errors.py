from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from backend.app.core.application_error import ApplicationError


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApplicationError)
    async def handle_application_error(
        _request: Request,
        error: ApplicationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": {},
                }
            },
        )
