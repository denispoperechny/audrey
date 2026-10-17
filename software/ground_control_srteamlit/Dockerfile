# Audrey ground control as a web app on port 8501.
#
#   docker compose up --build -d          (see docker-compose.yml)
#
# The image includes .env, so it carries the MQTT password: keep it on machines you control
# and don't push it to a public registry. Saved positions go to /app/data, a volume, so
# they survive rebuilds.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SAVED_POSITIONS_FILE=/app/data/saved_positions.json

WORKDIR /app
RUN useradd --create-home --uid 1000 app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .
RUN mkdir -p /app/data && chown app:app /app/data
USER app

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4)"

CMD ["streamlit", "run", "main.py", \
     "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
