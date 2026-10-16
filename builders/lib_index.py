"""THE index-entry validator — one implementation, used for the entry being published AND for
every entry preserved from the current index.

They used to be two similar-looking code paths in publish.sh; two rule sets that must agree but
are written twice will drift, and the weaker one decides what reaches the release (audit
finding). Everything an index entry must satisfy lives here.
"""

import re

SCHEMA = 2
# The second index (schema 3, `index-v3.json`): per stack a HISTORY, newest first, one entry per
# distinct composition. Which file a publish writes is named by the lhpc_ref manifest.
SCHEMA_V3 = 3
INDEX_NAMES = {"index.json": SCHEMA, "index-v3.json": SCHEMA_V3}
SIDECAR = ".lhpc-build-inputs"
# The controller reads at most this much of an index (binary_install._INDEX_MAX_BYTES): a history
# past it would fail on every box at once, so the publish refuses it instead (it is never cut).
INDEX_V3_MAX_BYTES = 1 << 20
_V3_FIELDS = {"inputs": dict, "published_at": str}
# Fields every index entry carries. The value is the type the field must have.
_FIELDS = {
    "filename": str, "url": str, "sha256": str, "size": int, "built_from": str,
    "components": dict, "lhpc_commit": str, "builder_commit": str, "target": str,
    "os": str, "container_digest": str, "runtime_deps": list, "smoke": dict,
    "extract_to": str,
}
_SMOKE_PASSED = {"mode": "mandatory", "result": "passed"}

# Per-stack top-level path prefixes an artifact may contain. An artifact must not carry files
# outside its OWN stack's trees, or it could overwrite another stack's source/scripts/binaries in
# the shared runtime root when extracted (audit: the broad src|build prefixes allowed cross-stack
# writes). Prefixes end with "/".
STACK_PATHS = {
    "daemon": ("src/loraham-daemon/",),
    "meshtastic": ("build/tools/meshtasticd/", "src/meshtastic-firmware/"),
    "meshcom": ("build/tool-cache/qemu-xtensa/", "src/meshcom-qemu-raspi/",
                "src/meshcom-loraham-bridge/"),
}


def path_allowed(name, isdir, stack):
    """True if a tar member `name` is within `stack`'s allowed extraction scope (or is an ancestor
    directory of an allowed prefix, e.g. the intermediate 'src/'). Unknown stack => nothing allowed."""
    n = name.rstrip("/")
    for p in STACK_PATHS.get(stack, ()):
        pp = p.rstrip("/")
        if n == pp or (n + "/").startswith(p):        # equal to, or under, an allowed prefix
            return True
        if isdir and p.startswith(n + "/"):           # an ancestor dir of an allowed prefix
            return True
    return False


def validate_entry(sid, e):
    """Return an error string, or "" when the entry is publishable.

    `sid` is the stack id the entry is filed under — the content-addressed filename is derived
    from it, so an entry can never be filed under the wrong stack.
    """
    if not re.fullmatch(r"[a-z0-9-]+", str(sid)):
        return f"bad stack id {sid!r}"
    if not isinstance(e, dict):
        return f"entry {sid!r} is not an object"
    # STRICT shape: exactly the known fields, no unknowns (a tampered fragment must not smuggle
    # extra keys past the validator; audit finding).
    extra = set(e) - set(_FIELDS)
    if extra:
        return f"entry {sid!r}: unexpected field(s) {sorted(extra)}"
    for k, t in _FIELDS.items():
        if not isinstance(e.get(k), t):
            return f"entry {sid!r}: field {k!r} missing or not {t.__name__}"
    if not re.fullmatch(r"[0-9a-f]{64}", e["sha256"]):
        return f"entry {sid!r}: malformed sha256"
    if e["filename"] != f"{sid}-{e['sha256']}.tar.zst":
        return f"entry {sid!r}: filename is not the content-addressed form"
    if e["size"] <= 0:
        return f"entry {sid!r}: invalid size"
    if e["target"] != "aarch64-trixie" or e["extract_to"] != "runtime-root":
        return f"entry {sid!r}: unexpected target/extract_to"
    if not e["url"].startswith("https://"):
        return f"entry {sid!r}: non-HTTPS url"
    if not e["url"].endswith("/" + e["filename"]):
        return f"entry {sid!r}: url does not point at its own artifact"
    if not e["components"]:
        return f"entry {sid!r}: empty components map"
    for cid, commit in e["components"].items():
        if not re.fullmatch(r"[a-z0-9-]+", str(cid)) or not isinstance(commit, str) \
                or not re.fullmatch(r"[0-9a-f]{40}", commit):
            return f"entry {sid!r}: bad components entry {cid!r}"
    for k in ("lhpc_commit", "builder_commit"):
        if not re.fullmatch(r"[0-9a-f]{40}", e[k]):
            return f"entry {sid!r}: {k} is not a full commit sha"
    for p in e["runtime_deps"]:
        if not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9.+-]+", p):
            return f"entry {sid!r}: suspicious runtime dep {p!r}"
    # THE publication gate: only a mandatory, passed smoke may live in the index.
    if e["smoke"] != _SMOKE_PASSED:
        return f"entry {sid!r}: smoke gate not satisfied ({e['smoke']!r})"
    return ""


def load_index(raw_obj):
    """Validate a decoded current index and return (index, error).

    STRICT: only a schema-2 index with a stacks table is accepted. There is no v1 migration —
    the published index has been schema 2 since the first release, and a "looks like v1" rule
    accepted shapes nobody could positively identify (an empty object, or any mapping whose
    values merely carry string `filename`/`sha256` fields) and then replaced the index with an
    empty one, erasing every preserved stack. A missing index.json asset is handled by the
    caller; anything present but unrecognised is a hard failure.
    """
    if not isinstance(raw_obj, dict):
        return None, "index is not an object"
    if raw_obj.get("schema") != SCHEMA:
        return None, f"unknown index schema {raw_obj.get('schema')!r}"
    if not isinstance(raw_obj.get("stacks"), dict):
        return None, "index has no stacks table"
    return raw_obj, ""


def index_name(index_url, repo, rel):
    """The asset name `index_url` points at in this repo's release `rel`, or None when it names
    anything but one of INDEX_NAMES there (a publish never writes a file it cannot identify)."""
    base = f"https://github.com/{repo}/releases/download/{rel}/"
    name = index_url[len(base):] if str(index_url).startswith(base) else ""
    return name if name in INDEX_NAMES else None


def inputs_map(recording, sidecar_digests):
    """Return ({component id: sidecar sha256}, error). `recording` = the stack's components whose
    manifest entry records build inputs (trusted); `sidecar_digests` = the sha256 of every
    `.lhpc-build-inputs` member inside the artifact. One sidecar per stack is all a builder packs
    today, so more than one recording component is refused rather than guessed at."""
    if len(recording) > 1:
        return None, f"more than one component records build inputs {sorted(recording)}"
    if len(sidecar_digests) != len(recording):
        return None, (f"{len(sidecar_digests)} {SIDECAR} member(s) in the artifact, "
                      f"{len(recording)} component(s) record build inputs")
    return dict(zip(recording, sidecar_digests)), ""


def validate_entry_v3(sid, e):
    """A schema-3 entry: a valid schema-2 entry plus `inputs` and `published_at`."""
    if not isinstance(e, dict):
        return f"entry {sid!r} is not an object"
    err = validate_entry(sid, {k: v for k, v in e.items() if k not in _V3_FIELDS})
    if err:
        return err
    for k, t in _V3_FIELDS.items():
        if not isinstance(e.get(k), t):
            return f"entry {sid!r}: field {k!r} missing or not {t.__name__}"
    for cid, digest in e["inputs"].items():
        if cid not in e["components"] or not isinstance(digest, str) \
                or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return f"entry {sid!r}: bad inputs entry {cid!r}"
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", e["published_at"]):
        return f"entry {sid!r}: published_at is not UTC YYYY-MM-DDTHH:MM:SSZ"
    return ""


def load_index_v3(raw_obj):
    """Validate a decoded index-v3.json and return (index, error): schema 3, a stacks table of
    lists, every entry valid. Anything else is a hard failure, as for schema 2."""
    if not isinstance(raw_obj, dict) or set(raw_obj) != {"schema", "stacks"}:
        return None, "index-v3 is not an object of exactly schema + stacks"
    if raw_obj["schema"] != SCHEMA_V3:
        return None, f"unknown index schema {raw_obj['schema']!r}"
    if not isinstance(raw_obj["stacks"], dict):
        return None, "index has no stacks table"
    for sid, entries in sorted(raw_obj["stacks"].items()):
        if not isinstance(entries, list):
            return None, f"stack {sid!r} is not a list"
        for e in entries:
            err = validate_entry_v3(sid, e)
            if err:
                return None, err
    return raw_obj, ""


def composition(e):
    """An entry's composition key: what a box compares before it downloads."""
    return (e["target"], sorted(e["components"].items()), sorted(e["inputs"].items()))


def v3_size_error(text):
    """"" when the serialized index-v3 fits what a controller reads, else the refusal."""
    n = len(text.encode())
    return "" if n <= INDEX_V3_MAX_BYTES else (
        f"index-v3.json would be {n} bytes, past the {INDEX_V3_MAX_BYTES} a controller reads")


def merge_v3(idx, sid, entry):
    """Put `entry` first in its stack's history. An older entry of the SAME composition (a rebuild)
    is replaced; every other composition is kept, however many publications follow it."""
    key = composition(entry)
    idx["stacks"][sid] = [entry] + [e for e in idx["stacks"].get(sid, []) if composition(e) != key]
    return idx
