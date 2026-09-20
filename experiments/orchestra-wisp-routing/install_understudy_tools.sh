#!/bin/sh
set -eu

pin="c70c889e5167a6e7a5f94f901411d42108c4ef2e"
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
target="$repo_root/.understudy/vendor/understudy-agent-tools"

if [ -e "$target" ]; then
  actual=$(git -C "$target" rev-parse HEAD 2>/dev/null || true)
  if [ "$actual" != "$pin" ]; then
    echo "Refusing to replace an existing checkout at $target" >&2
    echo "Expected $pin, found ${actual:-non-git content}." >&2
    exit 1
  fi
  if [ -x "$target/dist/bin.js" ] && [ -d "$target/node_modules" ]; then
    echo "Understudy CLI already ready at $target/dist/bin.js"
    exit 0
  fi
else
  mkdir -p "$repo_root/.understudy/vendor"
  git clone https://github.com/UnderstudyLabs/understudy-agent-tools.git "$target"
  git -C "$target" checkout --detach "$pin"
fi

npm --prefix "$target" ci --ignore-scripts --no-audit --no-fund \
  --cache "$repo_root/.understudy/npm-cache"
npm --prefix "$target" run build
echo "Understudy CLI ready at $target/dist/bin.js"
