# Audrey MQTT logger: records the boat's MQTT topics into the storage account, in
# 10-minute JSON Lines files (see logger.py). A background service: no ports.
#
#   az acr build -r audreyacr -t mqtt-logger:latest --platform linux/amd64 .
#
# The image includes .env, so it carries the MQTT password: keep it in the private
# registry. The storage account needs no secret: the container app's managed identity is
# used.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN useradd --create-home --uid 1000 app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .
USER app

CMD ["python", "logger.py"]
