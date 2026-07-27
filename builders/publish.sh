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
import hashlib, json, os, subprocess, sys, tarfile
sys.path.insert(0, os.environ.get("BUILDERS_DIR", "builders"))
from lib_index import validate_entry            # THE one entry validator (used again at merge)

frag_path, out = sys.argv[1], sys.argv[2]
d = json.load(open(frag_path))
if not isinstance(d.get("stack"), str):
    sys.exit("FAIL: fragment has no stack id")
# A fragment IS an index entry plus its stack id and minus the url (added at merge time):
# validate it with EXACTLY the rules every preserved entry must satisfy — one rule set, one
# implementation (two similar validators drift, and the weaker one decides; audit finding).
probe = {k: v for k, v in d.items() if k != "stack"}
probe["url"] = "https://example.invalid/" + str(d.get("filename", ""))
err = validate_entry(d["stack"], probe)
if err:
    sys.exit("FAIL: " + err)

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
MAX_MEMBERS = 20000
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
zstd = subprocess.Popen(["zstd", "-dc", tar_path], stdout=subprocess.PIPE)
seen_names, members, total = set(), 0, 0
try:
    with tarfile.open(fileobj=zstd.stdout, mode="r|") as tf:
        for m in tf:
            members += 1
            if members > MAX_MEMBERS:
                sys.exit(f"FAIL: archive exceeds {MAX_MEMBERS} members")
            if not (m.isreg() or m.isdir()):
                sys.exit(f"FAIL: non-regular tar member {m.name!r} ({m.type!r})")
            # LITERAL prefix strip: `lstrip("./")` removes a CHARACTER SET, turning
            # "../../src/evil" into "src/evil" and erasing the traversal we mean to catch.
            raw = m.name
            name = raw[2:] if raw.startswith("./") else raw
            if name in ("", "."):
                continue                      # the archive root itself ("." / "./")
            if raw.startswith("/") or ".." in raw.split("/") or ".." in name.split("/"):
                sys.exit(f"FAIL: escaping tar member {raw!r}")
            if name in seen_names:
                sys.exit(f"FAIL: duplicate tar member {raw!r}")
            seen_names.add(name)
            if name and name.split("/")[0] not in ("src", "build"):
                sys.exit(f"FAIL: unexpected top-level member {raw!r}")
            if m.isreg():
                if m.size > MAX_MEMBER_BYTES:
                    sys.exit(f"FAIL: member {raw!r} expands to {m.size} bytes")
                total += m.size
                if total > MAX_TOTAL_BYTES:
                    sys.exit(f"FAIL: archive expands beyond {MAX_TOTAL_BYTES} bytes")
finally:
    if zstd.stdout:
        zstd.stdout.close()
    zstd.wait()
if zstd.returncode != 0:
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
import json, os, subprocess, sys
sys.path.insert(0, os.environ.get("BUILDERS_DIR", "builders"))
from lib_index import SCHEMA, load_index, validate_entry

repo, rel, frag_path = sys.argv[1], sys.argv[2], sys.argv[3]
frag = json.load(open(frag_path))
stack = frag.pop("stack")
frag["url"] = "https://github.com/%s/releases/download/%s/%s" % (repo, rel, frag["filename"])

# Read the CURRENT index through the authenticated API. ONLY a positively identified missing
# index counts as empty — any network/JSON/schema error is fatal (a transient failure must
# never wipe the other stacks; that was a live audit finding).
names = subprocess.run(
    ["gh", "release", "view", rel, "--json", "assets", "--jq", ".assets[].name"],
    capture_output=True, text=True)
if names.returncode != 0:
    sys.exit("FAIL: cannot list release assets: " + names.stderr.strip())
if "index.json" in names.stdout.split():
    got = subprocess.run(
        ["gh", "api", "repos/%s/releases/tags/%s" % (repo, rel)],
        capture_output=True, text=True)
    if got.returncode != 0:
        sys.exit("FAIL: cannot read release via API")
    asset_id = next(a["id"] for a in json.loads(got.stdout)["assets"]
                    if a["name"] == "index.json")
    raw = subprocess.run(
        ["gh", "api", "-H", "Accept: application/octet-stream",
         "repos/%s/releases/assets/%s" % (repo, asset_id)], capture_output=True)
    if raw.returncode != 0:
        sys.exit("FAIL: cannot download current index.json")
    idx, err = load_index(json.loads(raw.stdout))   # malformed JSON => hard fail (exception)
    if err:
        sys.exit("FAIL: " + err + " -- refusing to publish")
    if not idx["stacks"]:
        print("legacy v1 index -- starting a fresh schema-2 index", file=sys.stderr)
else:
    idx = {"schema": SCHEMA, "stacks": {}}

# Revalidate every PRESERVED entry with the SAME rules the new one passes: a corrupted
# neighbour must not ride along into the new index.
for sid, e in sorted(idx["stacks"].items()):
    err = validate_entry(sid, e)
    if err:
        sys.exit("FAIL: existing " + err + " -- refusing to publish")
err = validate_entry(stack, frag)
if err:
    sys.exit("FAIL: " + err)

idx["stacks"][stack] = frag
json.dump(idx, open("dist-index.json", "w"), indent=2, sort_keys=True)
with open("dist-SHA256SUMS", "w") as fh:
    for sid in sorted(idx["stacks"]):
        e = idx["stacks"][sid]
        fh.write(e["sha256"] + "  " + e["filename"] + "\n")
print(json.dumps(idx, indent=2))
PY
mv dist-index.json index.json
mv dist-SHA256SUMS SHA256SUMS
gh release upload "$REL" SHA256SUMS --clobber
gh release upload "$REL" index.json --clobber      # the pointer switch — LAST
echo "published ${FNAME} to release '$REL' (index switched)"
