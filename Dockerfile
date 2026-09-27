# The gateway and, behind its password, the dashboard (reseau.front --dashboard), for Render (render.yaml).
# Secrets come from the environment, never the image.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY reseau reseau
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
CMD ["sh", "-c", "exec python -m reseau.front --host 0.0.0.0 --port ${PORT:-8080} --dashboard"]
