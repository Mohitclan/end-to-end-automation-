# One image for both processes: `growthstack web` and `growthstack worker`.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY growthstack ./growthstack
COPY alembic ./alembic
COPY alembic.ini ./
RUN pip install ".[postgres]" \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data && chown app /data

USER app
ENV DATA_DIR=/data LANGGRAPH_CHECKPOINT_PATH=/data/langgraph-checkpoints.sqlite
EXPOSE 8000
CMD ["growthstack", "web", "--host", "0.0.0.0", "--port", "8000"]
