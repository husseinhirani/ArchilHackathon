#!/usr/bin/env bash
# Installs the stego toolchain into a Debian-based image. Used by the Archil
# template sandbox and by docker/Dockerfile. Expects /opt/arena/tools.tar.gz.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  binwalk libimage-exiftool-perl steghide ruby file xxd pngcheck imagemagick curl ca-certificates >/dev/null
gem install --no-document zsteg >/dev/null

# stegseek: .deb from GitHub releases (best effort - not fatal if egress blocks it)
ARCH=$(dpkg --print-architecture)
if [ "$ARCH" = "amd64" ]; then
  curl -fsSL -o /tmp/stegseek.deb https://github.com/RickdeJager/stegseek/releases/download/v0.6/stegseek_0.6-1.deb \
    && apt-get install -y -qq /tmp/stegseek.deb >/dev/null || echo "stegseek install skipped"
fi

pip install --no-cache-dir -q numpy pillow cryptography

mkdir -p /opt/arena /work
tar -xzf /opt/arena/tools.tar.gz -C /opt/arena
cat > /etc/profile.d/arena.sh <<'EOF'
export ARENA_TOOLS=/opt/arena
export PYTHONPATH=/opt/arena
EOF
python3 -c "import sys; sys.path.insert(0,'/opt/arena'); import arena_schemes; print('arena_schemes ok')"
for t in zsteg binwalk exiftool steghide stegseek pngcheck; do command -v $t >/dev/null && echo "ok $t" || echo "MISSING $t"; done
