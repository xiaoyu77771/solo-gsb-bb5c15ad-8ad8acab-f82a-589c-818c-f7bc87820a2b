FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AUDIT_DB_PATH=/data/audits.db \
    AUDIT_SEED_PATH=/app/data/seed_registry.json

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data
COPY README.md ./README.md
COPY docker-entrypoint.sh ./docker-entrypoint.sh

RUN mkdir -p /data && \
    addgroup --system app && adduser --system --ingroup app app && \
    chmod +x /app/docker-entrypoint.sh && \
    chown -R app:app /data /app
USER app

VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import json,urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3); sys.exit(0 if json.load(r)['status']=='ok' else 1)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
