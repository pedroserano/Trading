FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.9.*

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app

# 568 is the "apps" user on TrueNAS, so files on the dataset get a sensible owner.
RUN useradd --uid 568 --no-create-home --shell /usr/sbin/nologin apps \
    && mkdir /data && chown 568:568 /data
USER 568
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD ["/app/.venv/bin/python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"]

# The backend is only reachable through Caddy on the compose network, so forwarded
# headers from any peer are trusted.
CMD ["/app/.venv/bin/uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", \
     "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
