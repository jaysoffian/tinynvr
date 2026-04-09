FROM python:3.14-alpine AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY tinynvr/ tinynvr/
COPY static/ static/
RUN uv sync --frozen --no-dev

FROM python:3.14-alpine
RUN apk add --no-cache ffmpeg
WORKDIR /app
COPY --from=builder /app /app
ENV PATH="/app/.venv/bin:$PATH" TINYNVR_CONFIG=/config/config.yaml
VOLUME ["/config", "/recordings"]
EXPOSE 8554
CMD ["uvicorn", "tinynvr.app:app", "--host", "0.0.0.0", "--port", "8554"]
