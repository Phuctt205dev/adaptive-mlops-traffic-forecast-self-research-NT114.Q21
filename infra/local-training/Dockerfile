FROM docker.io/library/python:3.11-slim-bookworm@sha256:e8c69d86735f6df3efa06b324694fe51f0422785f83a3bd463052636c2421944
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements /app/requirements
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install -r requirements/local-training.txt
COPY src /app/src
COPY backend /app/backend
# Real ML sources come from the hash-checked per-run source snapshot.
RUN useradd --uid 10001 --no-create-home training && mkdir /work && chown training:training /work
USER 10001:10001
WORKDIR /work
ENV PYTHONPATH=/app
CMD ["python", "-u", "-B", "-m", "src.local_training.job"]
