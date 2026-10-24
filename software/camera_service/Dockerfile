# Audrey camera service (FastAPI) on port 8000.
#
#   az acr build -r audreyacr -t camera-service:latest --platform linux/amd64 .
#
# The image includes .env, which only says where the storage is (no secrets): in Azure the
# container app signs in to storage with its managed identity.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN useradd --create-home --uid 1000 app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app main.py store.py store_blob.py .env ./
COPY --chown=app:app static ./static
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=4)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
