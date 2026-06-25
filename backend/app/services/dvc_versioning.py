import logging
import re
import subprocess
import uuid
from pathlib import Path

from backend.app.core.config import Settings, get_settings


logger = logging.getLogger(__name__)


class DVCVersioningError(RuntimeError):
    pass


def _run_dvc_with_process_env(args: list[str], settings: Settings) -> subprocess.CompletedProcess:
    root = Path(settings.dvc_repo_root).resolve()
    import os

    environment = os.environ.copy()
    environment.update(
        {
            "AWS_ACCESS_KEY_ID": settings.aws_access_key_id,
            "AWS_SECRET_ACCESS_KEY": settings.aws_secret_access_key,
            "AWS_DEFAULT_REGION": settings.aws_default_region,
            "MLFLOW_S3_ENDPOINT_URL": settings.dvc_remote_endpoint,
        }
    )
    command = ["dvc", *args]
    try:
        return subprocess.run(
            command,
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise DVCVersioningError("DVC CLI is not installed in the API image.") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise DVCVersioningError(f"DVC command failed: {' '.join(command)}: {detail}") from error


def _ensure_remote(settings: Settings) -> None:
    _run_dvc_with_process_env(
        [
            "remote",
            "add",
            "-d",
            settings.dvc_remote_name,
            settings.dvc_remote_url,
            "--force",
        ],
        settings,
    )
    _run_dvc_with_process_env(
        [
            "remote",
            "modify",
            settings.dvc_remote_name,
            "endpointurl",
            settings.dvc_remote_endpoint,
        ],
        settings,
    )


def _relative_to_repo(path: Path, settings: Settings) -> str:
    root = Path(settings.dvc_repo_root).resolve()
    return path.resolve().relative_to(root).as_posix()


def _extract_md5(dvc_file: Path) -> str | None:
    match = re.search(r"^\s*md5:\s*([0-9a-fA-F]+)\s*$", dvc_file.read_text(), re.MULTILINE)
    return match.group(1) if match else None


def version_dataset(
    *,
    region_id: uuid.UUID,
    dataset_id: uuid.UUID,
    filename: str,
    content: bytes,
) -> dict:
    settings = get_settings()
    if not settings.dvc_enabled:
        return {
            "enabled": False,
            "rev": None,
            "path": None,
            "remote": settings.dvc_remote_url,
        }

    root = Path(settings.dvc_repo_root).resolve()
    snapshot_path = (
        root
        / settings.dvc_workspace_dir
        / "regions"
        / str(region_id)
        / "datasets"
        / str(dataset_id)
        / "raw.csv"
    )
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_bytes(content)

    try:
        _ensure_remote(settings)
        relative_snapshot = _relative_to_repo(snapshot_path, settings)
        _run_dvc_with_process_env(["add", relative_snapshot], settings)
        dvc_file = snapshot_path.with_suffix(snapshot_path.suffix + ".dvc")
        relative_dvc_file = _relative_to_repo(dvc_file, settings)
        _run_dvc_with_process_env(["push", relative_dvc_file], settings)
    except Exception:
        if not settings.dvc_keep_local_snapshot:
            try:
                snapshot_path.unlink()
            except FileNotFoundError:
                pass
        raise

    md5 = _extract_md5(dvc_file)
    if not md5:
        raise DVCVersioningError(f"DVC metadata hash was not found in {relative_dvc_file}.")

    if not settings.dvc_keep_local_snapshot:
        try:
            snapshot_path.unlink()
        except FileNotFoundError:
            pass

    rev = f"dvc://{settings.dvc_remote_name}/{relative_dvc_file}#md5={md5}"
    logger.info(
        "Dataset %s was versioned with DVC at %s",
        dataset_id,
        rev,
    )
    return {
        "enabled": True,
        "rev": rev,
        "path": relative_dvc_file,
        "remote": settings.dvc_remote_url,
        "endpoint": settings.dvc_remote_endpoint,
        "hash": md5,
        "original_filename": filename,
    }
