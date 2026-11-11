#!/usr/bin/env bash
# Runs as root inside Ubuntu WSL. System-level installs only.
set -euo pipefail

echo "=== apt update + base deps ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  ca-certificates curl unzip git less groff \
  build-essential

echo "=== AWS CLI v2 ==="
if command -v aws >/dev/null 2>&1; then
  echo "aws already present: $(aws --version)"
else
  tmp="$(mktemp -d)"
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "$tmp/awscliv2.zip"
  unzip -q "$tmp/awscliv2.zip" -d "$tmp"
  "$tmp/aws/install"
  rm -rf "$tmp"
fi

echo "=== Session Manager plugin ==="
if command -v session-manager-plugin >/dev/null 2>&1; then
  echo "session-manager-plugin already present"
else
  tmp="$(mktemp -d)"
  curl -fsSL "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" \
    -o "$tmp/session-manager-plugin.deb"
  dpkg -i "$tmp/session-manager-plugin.deb"
  rm -rf "$tmp"
fi

echo
echo "=== versions ==="
aws --version
session-manager-plugin --version
git --version
echo "ROOT STAGE OK"
