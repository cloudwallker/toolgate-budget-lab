FROM python:3.12-slim
WORKDIR /app
RUN python -m pip install --no-cache-dir uv==0.10.12
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONUTF8=1
RUN useradd --system --uid 10001 toolgate
USER toolgate
ENTRYPOINT ["python", "-m", "toolgate.cli"]
