#!/usr/bin/env bash
# Vendor zenik_indexer into this action.
#
# WHY VENDOR: the action needs the indexer at run time on the client's runner.
# `pip install git+https://github.com/zenikhq/zenik-indexer` would require a
# Zenik credential in client CI to read a private repo — exactly the thing the
# architecture avoids. GitHub already fetches this action repo for `uses:`, so
# shipping the indexer inside it costs one fetch and no extra credential.
set -euo pipefail
SRC="${1:-../zenik-indexer/zenik_indexer}"
DEST="$(dirname "$0")/vendor/zenik_indexer"

if [ ! -d "$SRC" ]; then
  echo "source not found: $SRC" >&2
  echo "usage: ./sync_vendor.sh [path/to/zenik-indexer/zenik_indexer]" >&2
  exit 1
fi

rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
cp -R "$SRC" "$DEST"
find "$DEST" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
echo "vendored $(find "$DEST" -name '*.py' | wc -l | tr -d ' ') files into $DEST"
