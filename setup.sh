#!/bin/bash
set -e

echo "==> Installing system deps..."
apt-get update -qq && apt-get install -y -qq curl ffmpeg python3-pip

echo "==> Installing yt-dlp..."
pip3 install -q --upgrade yt-dlp

echo "==> Installing bun..."
curl -fsSL https://bun.sh/install | bash
export BUN_INSTALL="$HOME/.bun"
export PATH="$BUN_INSTALL/bin:$PATH"

echo "==> Installing Node dependencies..."
bun install

echo "==> Building app..."
bun run build

echo ""
echo "Done. Run with: bun run start"
echo "App will be at http://0.0.0.0:8004"
