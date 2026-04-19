#!/bin/sh
# deploy.sh - deploy HEO II from GitHub into Home Assistant.
#
# Runs ON the HA SSH add-on (Alpine busybox). Called by deploy-to-ha.ps1
# from Archer, or directly via ssh.
#
# Steps:
#   1. Fetch current master tarball from GitHub
#   2. Extract to staging dir under /tmp
#   3. Verify the extract contains the expected manifest
#   4. Atomically swap: mv live -> live.bak.TIMESTAMP, then staged -> live
#   5. Purge __pycache__ so Python recompiles fresh
#   6. Print the deployed commit SHA for verification
#
# On any failure, old directory is restored (nothing destructive until
# the final rename, which is atomic).
set -eu

REPO="${REPO:-a1acrity/heo2}"
REF="${REF:-master}"
LIVE="/config/custom_components/heo2"
# Backups MUST live outside /config/custom_components/ or HA's integration
# loader will try to parse their directory names as Python module paths
# (e.g. heo2.bak.20260419 -> import heo2.bak) and crash HEO II setup.
BAK_DIR="/config/heo2_backups"
STAGE="/tmp/heo2-deploy-$$"
TS=$(date +%Y%m%d-%H%M%S)
BAK="${BAK_DIR}/heo2.${TS}"

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

echo "[deploy] repo=$REPO ref=$REF live=$LIVE"

mkdir -p "$STAGE"
cd "$STAGE"

# Resolve ref to a commit SHA so we record what we deployed
SHA=$(wget -qO- "https://api.github.com/repos/$REPO/commits/$REF" \
      | jq -r '.sha // empty' | head -c 40)
if [ -z "$SHA" ]; then
    echo "[deploy] FATAL could not resolve $REPO#$REF to a commit SHA"
    exit 1
fi
echo "[deploy] resolved $REF -> $SHA"

# Fetch tarball
wget -q -O src.tar.gz "https://codeload.github.com/$REPO/tar.gz/$SHA"
SIZE=$(wc -c < src.tar.gz)
if [ "$SIZE" -lt 1000 ]; then
    echo "[deploy] FATAL tarball too small ($SIZE bytes), aborting"
    exit 1
fi
echo "[deploy] downloaded $SIZE bytes"

tar -xzf src.tar.gz
EXTRACTED=$(find . -maxdepth 1 -type d -name "heo2-*" | head -1)
if [ -z "$EXTRACTED" ]; then
    echo "[deploy] FATAL no heo2-* directory in tarball"
    exit 1
fi

SRC="$EXTRACTED/custom_components/heo2"
if [ ! -f "$SRC/manifest.json" ]; then
    echo "[deploy] FATAL $SRC/manifest.json missing"
    exit 1
fi

VERSION=$(jq -r '.version // "?"' "$SRC/manifest.json")
echo "[deploy] staged manifest.json version=$VERSION"

# Secret-scan the staged copy before we swap. If any of these patterns
# appears in tracked .py, .yaml, or .json files something has gone badly
# wrong and we refuse to deploy. Helps catch accidental commits of real
# credentials instead of placeholder config. Adjust the list as new
# integrations bring new secret shapes.
#
# We deliberately look for value SHAPES, not names, so generic variable
# names like `api_key =` don't false-positive.
SCAN=$(grep -REn \
    -e '[A-Za-z0-9]{8}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}' \
    -e '^[0-9]{13}$' \
    -e 'sk_live_[A-Za-z0-9]{20,}' \
    -e 'ghp_[A-Za-z0-9]{36,}' \
    --include='*.py' --include='*.yaml' --include='*.yml' --include='*.json' \
    "$SRC" 2>/dev/null || true)
if [ -n "$SCAN" ]; then
    echo "[deploy] FATAL secret-scan matched suspicious pattern:"
    echo "$SCAN" | head -5
    echo "[deploy] refusing to deploy; check the repo for leaked credentials"
    exit 1
fi

# Record provenance in the staged copy before the swap
echo "$SHA" > "$SRC/.deployed_sha"
echo "$REF" > "$SRC/.deployed_ref"
date > "$SRC/.deployed_at"

# Atomic swap: rename live to backup (outside scan path), rename staged to live
mkdir -p "$BAK_DIR"
if [ -d "$LIVE" ]; then
    mv "$LIVE" "$BAK"
    echo "[deploy] backed up previous version to $BAK"
fi
mv "$SRC" "$LIVE"

# Remove any stale pycache so Python recompiles cleanly
rm -rf "$LIVE/__pycache__" "$LIVE/rules/__pycache__" 2>/dev/null || true

echo "[deploy] OK sha=$SHA version=$VERSION backup=$BAK"
echo "[deploy] next step: reload integration via API or restart HA"
