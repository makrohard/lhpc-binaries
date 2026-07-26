#!/usr/bin/env bash
# Host-side publish (runs on the Ubuntu runner, has gh + node). Uploads the built
# tarball(s) to the ROLLING release and merges the per-stack fragment into index.json
# (preserving the OTHER stacks' entries). Never touches the git tree.
set -euo pipefail

REL="binaries"                       # the single rolling release tag
REPO="${GITHUB_REPOSITORY}"

shopt -s nullglob
tarballs=(dist/*.tar.zst)
[ ${#tarballs[@]} -gt 0 ] || { echo "no dist/*.tar.zst to publish"; exit 0; }

# Ensure the rolling release exists.
gh release view "$REL" >/dev/null 2>&1 || \
  gh release create "$REL" --title "lhpc binaries (rolling)" \
    --notes "Latest per-stack aarch64/Trixie binaries. Consumed via index.json (sha256-verified)." --latest=false

# Upload/overwrite the tarball(s).
for t in "${tarballs[@]}"; do
  gh release upload "$REL" "$t" --clobber
done

# Merge fragments into index.json AND regenerate SHA256SUMS — both preserving the OTHER stacks.
# SHA256SUMS is derived from the merged index (not local dist/), so a single-stack publish never
# drops the other stacks' checksums; index.json stays the authoritative source of truth for both.
python3 - "$REPO" "$REL" <<'PY'
import json, glob, os, sys, urllib.request
repo, rel = sys.argv[1], sys.argv[2]
idx = {}
# Start from the existing published index.json so untouched stacks persist.
url = f"https://github.com/{repo}/releases/download/{rel}/index.json"
try:
    with urllib.request.urlopen(url, timeout=15) as r:
        idx = json.load(r)
except Exception:
    idx = {}
for f in glob.glob("dist/*.frag.json"):
    d = json.load(open(f))
    stack = d.pop("stack")
    d["url"] = f"https://github.com/{repo}/releases/download/{rel}/{stack}.tar.zst"
    idx[stack] = d
os.makedirs("dist", exist_ok=True)
json.dump(idx, open("dist/index.json", "w"), indent=2, sort_keys=True)
# SHA256SUMS across ALL stacks in the merged index (standard `sha256sum -c` format).
with open("dist/SHA256SUMS", "w") as fh:
    for stack in sorted(idx):
        fh.write(f"{idx[stack]['sha256']}  {stack}.tar.zst\n")
print("index.json:", json.dumps(idx, indent=2))
PY
gh release upload "$REL" dist/index.json dist/SHA256SUMS --clobber
echo "published to release '$REL'"
