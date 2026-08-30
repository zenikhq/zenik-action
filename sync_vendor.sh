#!/usr/bin/env bash
#
# Vendor the zenik-indexer package INTO this action.
#
# Why a copy, not a pip-install-from-git: this action runs on the CLIENT's CI
# runner, where we deliberately do not expose a token that could read Zenik's
# private repos. So the indexer travels WITH the action, already on disk, and
# run_zenik.py imports it from `vendor/` (added to sys.path). The vendored copy
# is committed to this repo — that is the whole point.
#
# Run this whenever the indexer changes and you want the action to pick it up.
# It is a deliberate, reviewable edit: the diff shows exactly which indexer code
# now ships in the client's CI.
#
# Usage:
#   ./sync_vendor.sh [path-to-zenik-indexer-checkout]
#
# Default source is the sibling checkout ../zenik-indexer.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_REPO="${1:-$HERE/../zenik-indexer}"
SRC_PKG="$SRC_REPO/zenik_indexer"
DEST_PKG="$HERE/vendor/zenik_indexer"

if [ ! -d "$SRC_PKG" ]; then
  echo "[sync_vendor] source package not found: $SRC_PKG" >&2
  echo "[sync_vendor] pass the path to a zenik-indexer checkout as \$1" >&2
  exit 1
fi

echo "[sync_vendor] source: $SRC_PKG"
echo "[sync_vendor] dest:   $DEST_PKG"

# Fresh copy — never a symlink (the client's runner must get a real, self
# contained tree), and never a stale merge of an old vendored copy.
rm -rf "$DEST_PKG"
mkdir -p "$HERE/vendor"
cp -R "$SRC_PKG" "$DEST_PKG"

# Drop caches / test junk that a plain cp may carry along.
find "$DEST_PKG" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$DEST_PKG" -name '*.pyc' -delete 2>/dev/null || true

# Pin the exact source commit so a vendored tree is always traceable to the
# indexer revision it came from.
COMMIT="unknown"
if git -C "$SRC_REPO" rev-parse HEAD >/dev/null 2>&1; then
  COMMIT="$(git -C "$SRC_REPO" rev-parse HEAD)"
fi
{
  echo "# zenik-indexer source pinned into vendor/ by sync_vendor.sh"
  echo "# Regenerate with ./sync_vendor.sh after bumping the indexer."
  echo "source_repo=$SRC_REPO"
  echo "commit=$COMMIT"
  echo "synced_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$HERE/vendor/INDEXER_VERSION"

echo "[sync_vendor] pinned commit: $COMMIT"
echo "[sync_vendor] done. Commit vendor/ to ship it with the action."
