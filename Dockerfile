# ---- build stage: install dependencies into an isolated prefix ----
FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- runtime stage: small, non-root ----
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

RUN groupadd --system app && useradd --system --gid app --home /app app
WORKDIR /app

COPY --from=builder /install /usr/local
COPY app ./app
COPY migrations ./migrations
COPY migrate.py .

USER app
EXPOSE 8000

# Liveness probe inside the container (slim image has no curl; use stdlib).
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
      sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=2).status == 200 else 1)"

# Run migrations, then serve. Racing replicas are safe: the migration
# runner serializes itself with a Postgres advisory lock.
CMD ["sh", "-c", "python migrate.py && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
