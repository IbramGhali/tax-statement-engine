FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra postgres
COPY config ./config
COPY prompts ./prompts
ENV PATH="/app/.venv/bin:$PATH" TSE_DATA_DIR=/app/data
EXPOSE 8000 9000
CMD ["tse", "serve", "--host", "0.0.0.0"]
