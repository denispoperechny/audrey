# Audrey mission control: several Python processes side by side, run by supervisord
# (supervisord.conf lists them; each one is restarted on its own if it stops). Port 8100: the
# boat's MCP tools (boat_mcp); port 8200: the mission UI (mission_ui).
#
#   az acr build -r audreyacr -t mission-control:latest --platform linux/amd64 .
#   docker run --rm --env-file .env mission-control      # locally
#
# Settings come from the environment (or a .env file next to the scripts, not copied into the
# image here): broker, camera service and Foundry addresses. In Azure the processes sign in to
# Foundry with the container's managed identity.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN useradd --create-home --uid 1000 app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .
USER app

EXPOSE 8100 8200

CMD ["supervisord", "-c", "/app/supervisord.conf"]
