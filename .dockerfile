FROM ghcr.io/astral-sh/uv:0.12.19-python3.14-trixie-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable --no-cache

# Auto uses CPU Torch and Transformers; weights are cached in /data at startup.
RUN uv pip install --no-cache --index-url https://download.pytorch.org/whl/cpu 'torch==2.14.0' \
    && uv pip install --no-cache 'transformers==5.17.0'

COPY config.docker.toml ./config.toml
COPY policy.toml ./policy.toml
RUN useradd --uid 10001 --create-home --user-group mayi \
    && install -d -o mayi -g mayi -m 0700 /data

USER mayi
VOLUME ["/data"]
EXPOSE 7411

ENTRYPOINT ["mayi", "--config", "/app/config.toml"]
CMD ["serve"]
