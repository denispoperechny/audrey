#!/usr/bin/env bash
# Builds the mission control image in the registry and deploys it as the container app
# mission-control in rg-audrey (creates it the first time, updates it afterwards).
#
#   ./deploy.sh            (from software/mission_control, after az login)
#
# Settings: the broker's from .env (the password as a container app secret, not in the image),
# the rest below. The app runs exactly one replica: two would mean two captains.
# The mission UI is public on port 8200 (http and https), with no sign-in.
# After the first deploy, its identity needs the roles in grant_access.sh.
set -euo pipefail
cd "$(dirname "$0")"

RG=rg-audrey
APP=mission-control
ENVIRONMENT=cae-audrey
REGISTRY=audreyacr
TAG=$(date -u +%Y%m%d-%H%M%S)
IMAGE=$REGISTRY.azurecr.io/$APP:$TAG

env_value() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
for name in MQTT_HOST MQTT_PORT MQTT_USER MQTT_PASSWORD; do
  [ -n "$(env_value $name)" ] || { echo "$name missing in .env"; exit 1; }
done
[ -f mqtt_ca.pem ] || { echo "mqtt_ca.pem missing (copy it from ../mqtt_logger)"; exit 1; }

ENV_VARS=(
  "MQTT_HOST=$(env_value MQTT_HOST)"
  "MQTT_PORT=$(env_value MQTT_PORT)"
  "MQTT_USER=$(env_value MQTT_USER)"
  "MQTT_PASSWORD=secretref:mqtt-password"
  "MQTT_CA_FILE=mqtt_ca.pem"
  "TRACE_STORAGE_ACCOUNT_URL=https://audreylogs.blob.core.windows.net"
  "TRACE_STORAGE_CONTAINER=logs"
)

echo "== building $IMAGE"
az acr build -r "$REGISTRY" -t "$APP:$TAG" -t "$APP:latest" --platform linux/amd64 . --only-show-errors -o none

if az containerapp show -g "$RG" -n "$APP" -o none 2>/dev/null; then
  echo "== updating $APP"
  az containerapp secret set -g "$RG" -n "$APP" --secrets "mqtt-password=$(env_value MQTT_PASSWORD)" -o none
  az containerapp update -g "$RG" -n "$APP" --image "$IMAGE" --set-env-vars "${ENV_VARS[@]}" -o none
else
  echo "== creating $APP"
  az containerapp create -g "$RG" -n "$APP" --environment "$ENVIRONMENT" --image "$IMAGE" \
    --registry-server "$REGISTRY.azurecr.io" --registry-identity system --system-assigned \
    --ingress external --target-port 8200 --transport http \
    --min-replicas 1 --max-replicas 1 --cpu 1.0 --memory 2.0Gi \
    --secrets "mqtt-password=$(env_value MQTT_PASSWORD)" --env-vars "${ENV_VARS[@]}" -o none
  az containerapp ingress update -g "$RG" -n "$APP" --allow-insecure true -o none
fi

FQDN=$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)
echo "== deployed $IMAGE"
echo "mission UI: http://$FQDN"
echo "identity:   $(az containerapp show -g "$RG" -n "$APP" --query identity.principalId -o tsv)"
