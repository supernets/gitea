#!/bin/bash
# SuperNETs Gitea Deployment - Developed by acidvegas (https://git.supernets.org/supernets/gitea)
# gitea/setup.sh

# Load environment variables
[ -f .env ] && source .env || { echo "Error: .env file not found"; exit 1; }

# Set xtrace, exit on error, & verbose mode (after loading environment variables)
set -xev

# Remove existing docker container if it exists
docker rm -f gitea 2>/dev/null || true

# Generate secret key using the Gitea binary itself
if [ -z "${GITEA_SECRET_KEY}" ]; then
  GITEA_SECRET_KEY=$(docker run --rm gitea/gitea:latest gitea generate secret SECRET_KEY)
  if grep -q "GITEA_SECRET_KEY" .env; then
    sed -i "s|GITEA_SECRET_KEY=.*|GITEA_SECRET_KEY=${GITEA_SECRET_KEY}|g" .env
  else
    echo "GITEA_SECRET_KEY=${GITEA_SECRET_KEY}" >> .env
  fi
fi

# Generate internal token using the Gitea binary itself
if [ -z "${GITEA_INTERNAL_TOKEN}" ]; then
  GITEA_INTERNAL_TOKEN=$(docker run --rm gitea/gitea:latest gitea generate secret INTERNAL_TOKEN)
  if grep -q "GITEA_INTERNAL_TOKEN" .env; then
    sed -i "s|GITEA_INTERNAL_TOKEN=.*|GITEA_INTERNAL_TOKEN=${GITEA_INTERNAL_TOKEN}|g" .env
  else
    echo "GITEA_INTERNAL_TOKEN=${GITEA_INTERNAL_TOKEN}" >> .env
  fi
fi

# Create directories for Gitea data
mkdir -p /opt/container-storage/gitea/data/gitea/conf

# Copy custom templates and assets into GITEA_CUSTOM (/data/gitea/)
cp -r custom/* /opt/container-storage/gitea/data/gitea/

# Copy app.ini to where Gitea actually reads it and fill in secrets
cp app.ini /opt/container-storage/gitea/data/gitea/conf/app.ini
sed -i "s|__SECRET_KEY__|${GITEA_SECRET_KEY}|g"         /opt/container-storage/gitea/data/gitea/conf/app.ini
sed -i "s|__INTERNAL_TOKEN__|${GITEA_INTERNAL_TOKEN}|g" /opt/container-storage/gitea/data/gitea/conf/app.ini

# Run the Gitea container with restart policy
docker run -d --restart unless-stopped --name gitea -e USER_UID=$(id -u) -e USER_GID=$(id -g) -p 127.0.0.1:3000:3000 -p 2222:2222 -v /opt/container-storage/gitea/data:/data gitea/gitea:latest

# Generate a random password for the admin user
ADMIN_PASSWORD=$(openssl rand -base64 12)

# Wait for Gitea to be ready, then create admin user
sleep 5
docker exec --user git gitea gitea admin user create --admin --username acidvegas --password ${ADMIN_PASSWORD} --email acid.vegas@acid.vegas
echo "Admin password: ${ADMIN_PASSWORD}"