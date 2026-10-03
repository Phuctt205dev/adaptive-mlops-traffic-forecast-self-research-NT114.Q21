"""Compatibility entrypoint for existing uvicorn and Docker commands."""

from backend.app.main import app

__all__ = ["app"]
