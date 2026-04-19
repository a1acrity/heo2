#!/bin/sh
# rollback.sh - restore the most recent backup from /config/heo2_backups
#
# Usage on HA:  sh /tmp/rollback.sh
# Usage from Archer: .\rollback-ha.ps1
set -eu

LIVE="/config/custom_components/heo2"
BAK_DIR="/config/heo2_backups"

if [ ! -d "$BAK_DIR" ]; then
    echo "[rollback] no backup dir at $BAK_DIR, aborting"
    exit 1
fi

LATEST=$(ls -1t "$BAK_DIR" 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
    echo "[rollback] no backups found in $BAK_DIR, aborting"
    exit 1
fi

SRC="$BAK_DIR/$LATEST"
if [ ! -f "$SRC/manifest.json" ]; then
    echo "[rollback] FATAL $SRC looks malformed (no manifest.json)"
    exit 1
fi

echo "[rollback] restoring $SRC -> $LIVE"
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p "$BAK_DIR"
if [ -d "$LIVE" ]; then
    mv "$LIVE" "$BAK_DIR/heo2.pre-rollback.$TS"
    echo "[rollback] saved current live to $BAK_DIR/heo2.pre-rollback.$TS"
fi

# Copy rather than move so the backup itself is preserved for repeat rollbacks
cp -a "$SRC" "$LIVE"
rm -rf "$LIVE/__pycache__" "$LIVE/rules/__pycache__" 2>/dev/null || true

SHA=$(cat "$LIVE/.deployed_sha" 2>/dev/null || echo unknown)
REF=$(cat "$LIVE/.deployed_ref" 2>/dev/null || echo unknown)
echo "[rollback] OK restored ref=$REF sha=$SHA"
echo "[rollback] next step: restart HA or reload the config entry"
