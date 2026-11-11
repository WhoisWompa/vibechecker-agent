#!/usr/bin/env bash
# Runs as the normal (non-root) user inside Ubuntu WSL.
# Miniconda + repo clone, both owned by the user.
set -euo pipefail

echo "=== Miniconda ==="
if [ -d "$HOME/miniconda3" ]; then
  echo "miniconda3 already at $HOME/miniconda3"
else
  tmp="$(mktemp -d)"
  curl -fsSL "https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh" \
    -o "$tmp/miniconda.sh"
  bash "$tmp/miniconda.sh" -b -p "$HOME/miniconda3"
  rm -rf "$tmp"
fi

# shellcheck disable=SC1091
source "$HOME/miniconda3/etc/profile.d/conda.sh"
conda init bash >/dev/null

echo "=== clone Orca_VLN (dev) ==="
# Cloned inside the Linux filesystem, NOT /mnt/c -- avoids OneDrive sync and
# the ~10x filesystem penalty on cross-OS paths.
if [ -d "$HOME/Orca_VLN/.git" ]; then
  echo "repo already at $HOME/Orca_VLN"
  git -C "$HOME/Orca_VLN" fetch --all --prune
else
  git clone --branch dev https://github.com/openverse-orca/Orca_VLN.git "$HOME/Orca_VLN"
fi

echo
echo "=== versions ==="
conda --version
python --version 2>/dev/null || true
echo "repo HEAD: $(git -C "$HOME/Orca_VLN" rev-parse --short HEAD) on $(git -C "$HOME/Orca_VLN" rev-parse --abbrev-ref HEAD)"
echo "USER STAGE OK"
