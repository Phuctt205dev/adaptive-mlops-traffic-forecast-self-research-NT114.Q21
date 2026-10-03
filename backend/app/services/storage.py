from functools import lru_cache
from typing import Any

import boto3

from backend.app.core.config import get_settings


@lru_cache
def get_s3_client() -> Any:
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_default_region,
    )


def put_object(bucket: str, key: str, body: bytes, content_type: str) -> str:
    get_s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
    )
    return f"s3://{bucket}/{key}"
