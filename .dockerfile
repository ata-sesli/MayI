FROM ghcr.io/astral-sh/uv:0.12.19-python3.14-trixie-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable --no-cache

# Keep the checkpoint out of the image; the daemon caches it in /data on startup.
ARG JULIA_REVISION=a85b127321d580d65176c89ced8273f305745d85
RUN uv pip install --no-cache --index-url https://download.pytorch.org/whl/cpu 'torch>=2.6' \
    && uv pip install --no-cache huggingface_hub \
    && python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='SupersonicLabs/Julia-1', revision='${JULIA_REVISION}', local_dir='/tmp/julia-source', allow_patterns=['pyproject.toml', 'julia/**'])" \
    && uv pip install --no-cache /tmp/julia-source \
    && rm -rf /tmp/julia-source

COPY config.docker.toml ./config.toml
COPY policy.toml ./policy.toml
RUN useradd --uid 10001 --create-home --user-group mayi \
    && install -d -o mayi -g mayi -m 0700 /data

USER mayi
VOLUME ["/data"]
EXPOSE 7411

ENTRYPOINT ["mayi", "--config", "/app/config.toml"]
CMD ["serve"]
