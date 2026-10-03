# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

ARG MLFLOW_VERSION=2.22.0

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
    "mlflow==${MLFLOW_VERSION}" \
    boto3 \
    psycopg2-binary

EXPOSE 5000
