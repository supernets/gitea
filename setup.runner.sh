#!/bin/bash
# SuperNETs Gitea Deployment - Developed by acidvegas (https://git.supernets.org/supernets/gitea)
# gitea/setup.runner.sh

# Load environment variables
[ -f .env ] && source .env || { echo "Error: .env file not found"; exit 1; }

# Set xtrace, exit on error, & verbose mode (after loading environment variables)
set -xev

# Check for required environment variables
[ -z "${GITEA_INSTANCE_URL}" ] && { echo "Error: GITEA_INSTANCE_URL not set in .env"; exit 1; }
[ -z "${GITEA_RUNNER_TOKEN}" ] && { echo "Error: GITEA_RUNNER_TOKEN not set in .env"; exit 1; }
[ -z "${GITEA_RUNNER_NAME}"  ] && { echo "Error: GITEA_RUNNER_NAME not set in .env";  exit 1; }

# Remove existing docker container if it exists
docker rm -f gitea-runner 2>/dev/null || true

# Create directory for runner data
mkdir -p /opt/containers/gitea-runner

# Generate runner config if it doesn't exist
if [ ! -f /opt/containers/gitea-runner/config.yaml ]; then
		docker run --rm --entrypoint act_runner gitea/act_runner:latest generate-config > /opt/containers/gitea-runner/config.yaml
fi

# Run the runner container (registers automatically via environment variables)
docker run -d --restart unless-stopped --name gitea-runner --network host --memory=2g --cpus=1 -e GITEA_INSTANCE_URL="${GITEA_INSTANCE_URL}" -e GITEA_RUNNER_REGISTRATION_TOKEN="${GITEA_RUNNER_TOKEN}" -e GITEA_RUNNER_NAME="${GITEA_RUNNER_NAME}" -e GITEA_RUNNER_LABELS=ubuntu-latest:docker://ubuntu:latest -v /opt/containers/gitea-runner:/data -v /var/run/docker.sock:/var/run/docker.sock gitea/act_runner:latest