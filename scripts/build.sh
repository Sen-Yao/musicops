#!/bin/bash
# Build musicops Docker image locally
set -e

# Build from the repository root, regardless of where the script is invoked.
cd "$(dirname "$0")/.."

# Build image with provenance, not an independent API version.
docker build \
  --build-arg MUSICOPS_BUILD_COMMIT="$(git rev-parse HEAD)" \
  --build-arg MUSICOPS_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -t musicops:latest .

# Optional: tag for registry
# docker tag musicops:latest your-registry/musicops:latest

echo "Built musicops:latest"
echo "Run with: docker compose up -d"
