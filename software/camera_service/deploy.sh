#!/usr/bin/env bash
# Builds the camera service image in the registry and updates the container app camera-service
# in rg-audrey to it.
#
#   ./deploy.sh            (from software/camera_service, after az login)
#
# Settings come from .env, which isn't in the image (.dockerignore): CAMERA_VISUALS_ACCESS as a
# container app secret, the others as plain environment variables. The ingress also accepts
# plain http (for the camera's push); the service itself refuses it for everything else.
set -euo pipefail
cd "$(dirname "$0")"

RG=rg-audrey
APP=camera-service
REGISTRY=audreyacr
TAG=$(date -u +%Y%m%d-%H%M)
IMAGE=$REGISTRY.azurecr.io/$APP:$TAG

env_value() { { grep -E "^$1=" .env || true; } | tail -1 | cut -d= -f2- | sed -E 's/[[:space:]]+#.*$//'; }
[ -n "$(env_value CAMERA_VISUALS_ACCESS)" ] || { echo "CAMERA_VISUALS_ACCESS missing in .env"; exit 1; }
ENV_VARS=("CAMERA_VISUALS_ACCESS=secretref:visuals-access")
for name in CAMERA_STORAGE_ACCOUNT_URL CAMERA_STORAGE_CONTAINER CAMERA_STREAM_CONTAINER FOUNDRY_OPENAI_URL FOUNDRY_MODEL \
            CAMERA_STREAM_STORE_ALL CAMERA_REQUIRE_HTTPS; do
  value=$(env_value $name)
  if [ -n "$value" ]; then ENV_VARS+=("$name=$value"); fi
done

echo "== building $IMAGE"
az acr build -r "$REGISTRY" -t "$APP:$TAG" -t "$APP:latest" --platform linux/amd64 . --only-show-errors -o none

echo "== updating $APP"
az containerapp secret set -g "$RG" -n "$APP" --secrets "visuals-access=$(env_value CAMERA_VISUALS_ACCESS)" -o none
az containerapp update -g "$RG" -n "$APP" --image "$IMAGE" --set-env-vars "${ENV_VARS[@]}" -o none
echo "== deployed $IMAGE: https://$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)"
