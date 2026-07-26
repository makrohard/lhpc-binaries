#!/usr/bin/env bash
# TRUSTED publish stage (host-side, the ONLY job with contents: write). Receives the build
# artifact directory as $1, NEVER executes anything from it, and publishes:
#   1. the CONTENT-ADDRESSED tarball  <stack>-<sha256>.tar.zst   (immutable — never clobbered)
#   2. the provenance text            <stack>-<sha256>.provenance.txt
#   3. SHA256SUMS + index.json LAST   (the pointer switch)
# under the single rolling release, serialized across ALL stacks by the workflow's global
# `publish` concurrency group. Old assets are retained for in-flight readers.
set -euo pipefail

OUT="${1:?artifact dir}"
REL="binaries"
REPO="${GITHUB_REPOSITORY}"

shopt -s nullglob
frags=("$OUT"/*.frag.json)
[ ${#frags[@]} -eq 1 ] || { echo "FAIL: expected exactly one fragment, got ${#frags[@]}" >&2; exit 2; }
FRAG="${frags[0]}"

echo "==> Validate the fragment + artifact (nothing from the build job is trusted)"
python3 - "$FRAG" "$OUT" <<'PY'
import hashlib, json, os, re, subprocess, sys, tarfile
frag_path, out = sys.argv[1], sys.argv[2]
d = json.load(open(frag_path))

REQ = {"stack": str, "filename": str, "sha256": str, "size": int, "built_from": str,
       "components": dict, "lhpc_commit": str, "builder_commit": str, "target": str,
       "os": str, "container_digest": str, "runtime_deps": list, "smoke": dict,
       "extract_to": str}
for k, t in REQ.items():
    if not isinstance(d.get(k), t):
        sys.exit(f"FAIL: fragment field {k!r} missing or wrong type")
if not re.fullmatch(r"[a-z0-9-]+", d["stack"]):
    sys.exit("FAIL: bad stack id")
if d["extract_to"] != "runtime-root" or d["target"] != "aarch64-trixie":
    sys.exit("FAIL: unexpected target/extract_to")
if not re.fullmatch(r"[0-9a-f]{64}", d["sha256"]):
    sys.exit("FAIL: bad sha256 format")
if d["filename"] != f"{d['stack']}-{d['sha256']}.tar.zst":
    sys.exit("FAIL: filename is not the content-addressed form")
for cid, commit in d["components"].items():
    if not re.fullmatch(r"[a-z0-9-]+", cid) or not re.fullmatch(r"[0-9a-f]{40}", str(commit)):
        sys.exit(f"FAIL: bad components entry {cid!r}")
if not re.fullmatch(r"[0-9a-f]{40}", d["lhpc_commit"]):
    sys.exit("FAIL: bad lhpc_commit")
for p in d["runtime_deps"]:
    if not re.fullmatch(r"[A-Za-z0-9.+-]+", p):
        sys.exit(f"FAIL: suspicious runtime dep {p!r}")
# THE publication gate: only a mandatory, passed smoke may enter the release.
if d["smoke"] != {"mode": "mandatory", "result": "passed"}:
    sys.exit(f"FAIL: smoke gate not satisfied: {d['smoke']!r} — diagnostic builds are never published")

tar_path = os.path.join(out, d["filename"])
if not os.path.isfile(tar_path):
    sys.exit(f"FAIL: artifact {d['filename']} missing")
# RECOMPUTE hash + size — the fragment's numbers are claims, not facts.
h = hashlib.sha256()
with open(tar_path, "rb") as fh:
    for chunk in iter(lambda: fh.read(1 << 20), b""):
        h.update(chunk)
if h.hexdigest() != d["sha256"]:
    sys.exit("FAIL: recomputed sha256 differs from fragment")
if os.path.getsize(tar_path) != d["size"]:
    sys.exit("FAIL: recomputed size differs from fragment")

# Member validation: regular files/dirs only, relative, no '..', sane top-level prefixes.
zstd = subprocess.Popen(["zstd", "-dc", tar_path], stdout=subprocess.PIPE)
with tarfile.open(fileobj=zstd.stdout, mode="r|") as tf:
    for m in tf:
        if not (m.isreg() or m.isdir()):
            sys.exit(f"FAIL: non-regular tar member {m.name!r} ({m.type!r})")
        name = m.name.lstrip("./")
        if name.startswith("/") or ".." in name.split("/"):
            sys.exit(f"FAIL: escaping tar member {m.name!r}")
        if name and name.split("/")[0] not in ("src", "build"):
            sys.exit(f"FAIL: unexpected top-level member {m.name!r}")
if zstd.wait() != 0:
    sys.exit("FAIL: zstd decompression failed")
prov = os.path.join(out, f"{d['stack']}.provenance.txt")
if not os.path.isfile(prov):
    sys.exit("FAIL: provenance file missing")
print(f"VALIDATED {d['filename']} ({d['size']} bytes) smoke={d['smoke']}")
PY

STACK="$(python3 -c "import json,sys; print(json.load(open('$FRAG'))['stack'])")"
FNAME="$(python3 -c "import json,sys; print(json.load(open('$FRAG'))['filename'])")"
SHA="$(python3 -c "import json,sys; print(json.load(open('$FRAG'))['sha256'])")"
PROV_NAME="${STACK}-${SHA}.provenance.txt"
cp "$OUT/${STACK}.provenance.txt" "$OUT/$PROV_NAME"

echo "==> Ensure the rolling release exists"
gh release view "$REL" >/dev/null 2>&1 || \
  gh release create "$REL" --title "lhpc binaries (rolling)" \
    --notes "Per-stack aarch64/Trixie binaries. Consumed via index.json (schema 2, sha256-verified, content-addressed assets)." \
    --latest=false

echo "==> Upload the immutable asset (skip when the identical digest already exists)"
if gh release view "$REL" --json assets --jq '.assets[].name' | grep -qx "$FNAME"; then
  echo "asset $FNAME already published (content-addressed — identical by name)"
else
  gh release upload "$REL" "$OUT/$FNAME"
fi
gh release upload "$REL" "$OUT/$PROV_NAME" --clobber

echo "==> Verify the uploaded asset digest (download-back, hash)"
VDIR="$(mktemp -d)"
gh release download "$REL" --pattern "$FNAME" --dir "$VDIR"
echo "$SHA  $VDIR/$FNAME" | sha256sum -c - >/dev/null || { echo "FAIL: uploaded asset digest mismatch" >&2; exit 4; }
echo "uploaded asset verified"

echo "==> Merge into index.json (authenticated read, fail-hard) + SHA256SUMS; index uploaded LAST"
python3 - "$REPO" "$REL" "$FRAG" <<'PY'
import json, subprocess, sys
repo, rel, frag_path = sys.argv[1], sys.argv[2], sys.argv[3]
frag = json.load(open(frag_path))
stack = frag.pop("stack")
frag["url"] = f"https://github.com/{repo}/releases/download/{rel}/{frag['filename']}"

# Read the CURRENT index through the authenticated API. ONLY a positively identified missing
# index counts as empty — any network/JSON/schema error is fatal (a transient failure must
# never wipe the other stacks; that was a live audit finding).
names = subprocess.run(
    ["gh", "release", "view", rel, "--json", "assets", "--jq", ".assets[].name"],
    capture_output=True, text=True)
if names.returncode != 0:
    sys.exit(f"FAIL: cannot list release assets: {names.stderr.strip()}")
have_index = "index.json" in names.stdout.split()
if have_index:
    got = subprocess.run(
        ["gh", "api", f"repos/{repo}/releases/tags/{rel}"], capture_output=True, text=True)
    if got.returncode != 0:
        sys.exit("FAIL: cannot read release via API")
    asset_id = next(a["id"] for a in json.loads(got.stdout)["assets"] if a["name"] == "index.json")
    raw = subprocess.run(
        ["gh", "api", "-H", "Accept: application/octet-stream",
         f"repos/{repo}/releases/assets/{asset_id}"], capture_output=True)
    if raw.returncode != 0:
        sys.exit("FAIL: cannot download current index.json")
    idx = json.loads(raw.stdout)          # malformed JSON => hard fail (exception)
    if idx.get("schema") != 2:
        # One-time migration from the legacy v1 index: start a fresh v2 index. The old
        # fixed-name assets remain downloadable but are no longer indexed (superseded).
        print("legacy index detected — starting a fresh schema-2 index", file=sys.stderr)
        idx = {"schema": 2, "stacks": {}}
    for sid, e in idx["stacks"].items():
        for k in ("filename", "sha256", "size", "components", "url", "smoke"):
            if k not in e:
                sys.exit(f"FAIL: existing index entry {sid!r} invalid — refusing to proceed")
else:
    idx = {"schema": 2, "stacks": {}}

idx["stacks"][stack] = frag
json.dump(idx, open("dist-index.json", "w"), indent=2, sort_keys=True)
with open("dist-SHA256SUMS", "w") as fh:
    for sid in sorted(idx["stacks"]):
        e = idx["stacks"][sid]
        fh.write(f"{e['sha256']}  {e['filename']}\n")
print(json.dumps(idx, indent=2))
PY
mv dist-index.json index.json
mv dist-SHA256SUMS SHA256SUMS
gh release upload "$REL" SHA256SUMS --clobber
gh release upload "$REL" index.json --clobber      # the pointer switch — LAST
echo "published ${FNAME} to release '$REL' (index switched)"
