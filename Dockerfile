FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
    && groupadd --gid 10001 mcp \
    && useradd --uid 10001 --gid mcp --no-create-home --shell /usr/sbin/nologin mcp \
    && mkdir -p /app/var \
    && chown mcp:mcp /app/var
ENV PATH="/app/.venv/bin:$PATH" \
    SELLSY_MCP_HOST=0.0.0.0
USER 10001:10001
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health/ready', timeout=3)" || exit 1
CMD ["sellsy-mcp"]
