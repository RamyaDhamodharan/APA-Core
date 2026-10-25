FROM python:3.12-slim

# LibreOffice is installed at image build time for offline DOCX previews.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        libreoffice-writer \
        fonts-liberation \
        fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_HTTP_TIMEOUT=300

WORKDIR /app

# 1) Dependencies first: this layer is rebuilt only when pyproject.toml or uv.lock change
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra documents --no-install-project

# 2) Code last: changes here are fast
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
COPY scripts ./scripts

EXPOSE 8000

CMD ["uv", "run", "--frozen", "--no-sync", "uvicorn", "src.api.rest.app:app", "--host", "0.0.0.0", "--port", "8000"]
