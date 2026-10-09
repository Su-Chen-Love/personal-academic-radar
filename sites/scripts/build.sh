#!/usr/bin/env bash
set -euo pipefail

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
dist_root="$project_root/dist"

rm -rf "$dist_root"
mkdir -p "$dist_root/server" "$dist_root/.openai"
cd "$project_root"
node scripts/build-ui.mjs
cp "$project_root/.openai/hosting.json" "$dist_root/.openai/hosting.json"
cp -R "$project_root/drizzle" "$dist_root/drizzle"

echo "Built $dist_root"
