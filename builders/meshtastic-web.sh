#!/usr/bin/env bash
# The Meshtastic web client for one channel, inside the Trixie container. It takes NO ref:
#   dev    = upstream `main` HEAD at run time (`git ls-remote`), built with upstream's own recipe
#            (pnpm install --frozen-lockfile; apps/web: pnpm run build, pnpm run package);
#   stable = upstream's newest release, its `build.tar` asset republished as it is.
# Either way the artifact holds the client as meshtasticd serves it (gunzipped, as LHPC's
# meshtastic-web-assets.sh leaves it) under build/tools/meshtastic-web/web/, nothing else. The lab
# smoke (a browser against meshtasticd) runs in the runtime-test job; it is what admits the entry.
set -euo pipefail

DIST=/out
UPSTREAM=https://github.com/meshtastic/web
CHANNEL="${WEB_CHANNEL:-}"
[ "$CHANNEL" = dev ] || [ "$CHANNEL" = stable ] || { echo "FAIL: channel must be dev or stable (got '$CHANNEL')" >&2; exit 2; }
[ -z "${SOURCE_COMMIT:-}" ] || { echo "FAIL: meshtastic-web takes no source_commit (dev = main HEAD, stable = the newest release)" >&2; exit 2; }

# Node for the dev build: the official linux-arm64 tarball, pinned by version and sha256 (upstream
# builds with Node 24; the Pi never runs it).
NODE_VERSION=v24.21.0
NODE_SHA256=6ad1325edbdb5649c379b75a237147a666c95d4f9ae8d340fef2d1575d289ad2

WORK="$(mktemp -d)"
TAR="$WORK/build.tar"
if [ "$CHANNEL" = dev ]; then
  WEB_COMMIT="$(git ls-remote "$UPSTREAM" refs/heads/main | cut -f1)"
  [[ "$WEB_COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "FAIL: could not resolve $UPSTREAM main" >&2; exit 4; }
  echo "==> dev: $UPSTREAM main = $WEB_COMMIT"
  git init -q "$WORK/web"
  git -C "$WORK/web" fetch -q --depth 1 "$UPSTREAM" "$WEB_COMMIT"
  git -C "$WORK/web" -c advice.detachedHead=false checkout -q FETCH_HEAD
  [ "$(git -C "$WORK/web" rev-parse HEAD)" = "$WEB_COMMIT" ] || { echo "FAIL: checkout is not $WEB_COMMIT" >&2; exit 4; }

  echo "==> Node $NODE_VERSION (sha256-pinned)"
  curl -fsSL --retry 3 -o "$WORK/node.tar.xz" \
    "https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-linux-arm64.tar.xz"
  echo "$NODE_SHA256  $WORK/node.tar.xz" | sha256sum -c - >/dev/null || { echo "FAIL: Node tarball sha256 mismatch" >&2; exit 4; }
  mkdir -p /opt/node && tar -xJf "$WORK/node.tar.xz" -C /opt/node --strip-components=1
  export PATH="/opt/node/bin:$PATH" COREPACK_ENABLE_DOWNLOAD_PROMPT=0
  corepack enable                                   # pnpm at the version upstream's packageManager names

  echo "==> Upstream recipe"
  (cd "$WORK/web" && pnpm install --frozen-lockfile)
  (cd "$WORK/web/apps/web" && pnpm run build && pnpm run package)
  cp "$WORK/web/apps/web/dist/build.tar" "$TAR"
  LATEST="$(curl -fsSL "https://api.github.com/repos/meshtastic/web/releases/latest" \
            | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')"
  [[ "$LATEST" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "FAIL: unexpected release tag '$LATEST'" >&2; exit 4; }
  WEB_VERSION="nightly-${LATEST}-${WEB_COMMIT:0:12}"   # upstream's own nightly label
else
  WEB_VERSION="$(curl -fsSL "https://api.github.com/repos/meshtastic/web/releases/latest" \
                 | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')"
  [[ "$WEB_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "FAIL: unexpected release tag '$WEB_VERSION'" >&2; exit 4; }
  WEB_COMMIT="$(git ls-remote "$UPSTREAM" "refs/tags/$WEB_VERSION*" \
                | python3 -c 'import sys; sys.path.insert(0, "/builders")
from lib_web import peeled_commit
print(peeled_commit(sys.stdin.read(), sys.argv[1]) or "")' "$WEB_VERSION")"
  [ -n "$WEB_COMMIT" ] || { echo "FAIL: tag $WEB_VERSION does not resolve to a commit" >&2; exit 4; }
  echo "==> stable: $WEB_VERSION = $WEB_COMMIT"
  curl -fsSL --retry 3 -o "$TAR" "$UPSTREAM/releases/download/$WEB_VERSION/build.tar"
fi

echo "==> Stage the served tree"
STAGE="$WORK/stage"
SERVED="$STAGE/build/tools/meshtastic-web/web"
mkdir -p "$SERVED"
tar -xf "$TAR" -C "$SERVED"
gunzip -r "$SERVED" 2>/dev/null || true
[ -f "$SERVED/index.html" ] || { echo "FAIL: the client has no index.html" >&2; exit 5; }
BUNDLE="$(python3 -c 'import sys; sys.path.insert(0, "/builders")
from lib_web import main_bundle
print(main_bundle(open(sys.argv[1]).read()) or "")' "$SERVED/index.html")"
if [ -z "$BUNDLE" ] || [ ! -s "$SERVED/$BUNDLE" ]; then
  echo "FAIL: index.html loads no main bundle present in the tree" >&2; exit 5
fi
echo "served tree: $(find "$SERVED" -type f | wc -l) files, main bundle $BUNDLE"

echo "==> Pack + fragment"
MTD_PIN="$("$PY" -c 'import sys; sys.path.insert(0, "/builders")
from lib_provenance import derive
print(derive("meshtastic")[1]["meshtastic"])')"
tar --zstd -C "$STAGE" -cf "$DIST/.web.tmp" .
SHA="$(sha256sum "$DIST/.web.tmp" | cut -d' ' -f1)"
FNAME="meshtastic-web-$SHA.tar.zst"
mv "$DIST/.web.tmp" "$DIST/$FNAME"
export FNAME SHA WEB_COMMIT MTD_PIN CHANNEL WEB_VERSION
python3 - "$DIST/meshtastic-web.frag.json" "$DIST/$FNAME" <<'PYFRAG'
import json, os, sys
env = os.environ
json.dump({
    "stack": "meshtastic-web", "filename": env["FNAME"], "sha256": env["SHA"],
    "size": os.path.getsize(sys.argv[2]), "built_from": env["WEB_COMMIT"],
    "components": {"meshtastic-web": env["WEB_COMMIT"], "meshtastic": env["MTD_PIN"]},
    "lhpc_commit": env["LHPC_COMMIT"], "builder_commit": env.get("BUILDER_COMMIT", "unknown"),
    "target": "aarch64-trixie", "os": "trixie",
    "container_digest": env.get("CONTAINER_DIGEST", "unknown"), "runtime_deps": [],
    # Mandatory: the browser smoke in the runtime-test job; publish needs that job to pass.
    "smoke": {"mode": "mandatory", "result": "passed"}, "extract_to": "runtime-root",
    "channel": env["CHANNEL"], "web_version": env["WEB_VERSION"],
}, open(sys.argv[1], "w"), indent=2)
PYFRAG
cat > "$DIST/meshtastic-web.provenance.txt" <<PROV
meshtastic-web $CHANNEL $WEB_VERSION
web commit:        $WEB_COMMIT ($UPSTREAM)
smoke-tested with: meshtasticd $MTD_PIN (the lhpc_ref manifest pin)
lhpc recipe:       $LHPC_COMMIT
PROV
echo "PACKED $FNAME  channel=$CHANNEL web=$WEB_COMMIT version=$WEB_VERSION"
