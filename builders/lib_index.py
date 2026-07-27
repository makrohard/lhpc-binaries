"""THE index-entry validator — one implementation, used for the entry being published AND for
every entry preserved from the current index.

They used to be two similar-looking code paths in publish.sh; two rule sets that must agree but
are written twice will drift, and the weaker one decides what reaches the release (audit
finding). Everything an index entry must satisfy lives here.
"""

import re

SCHEMA = 2
# Fields every index entry carries. The value is the type the field must have.
_FIELDS = {
    "filename": str, "url": str, "sha256": str, "size": int, "built_from": str,
    "components": dict, "lhpc_commit": str, "builder_commit": str, "target": str,
    "os": str, "container_digest": str, "runtime_deps": list, "smoke": dict,
    "extract_to": str,
}
_SMOKE_PASSED = {"mode": "mandatory", "result": "passed"}


def validate_entry(sid, e):
    """Return an error string, or "" when the entry is publishable.

    `sid` is the stack id the entry is filed under — the content-addressed filename is derived
    from it, so an entry can never be filed under the wrong stack.
    """
    if not re.fullmatch(r"[a-z0-9-]+", str(sid)):
        return f"bad stack id {sid!r}"
    if not isinstance(e, dict):
        return f"entry {sid!r} is not an object"
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
        if not re.fullmatch(r"[a-z0-9-]+", str(cid)) or not re.fullmatch(r"[0-9a-f]{40}",
                                                                        str(commit)):
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

    A v1 index is migrated ONLY when it is positively identified as one: a mapping whose every
    value looks like a v1 stack entry. Anything else unrecognised is an error — a transient or
    unexpected shape must never be silently rewritten into an empty index, which would drop
    every other stack's published entry.
    """
    if not isinstance(raw_obj, dict):
        return None, "index is not an object"
    schema = raw_obj.get("schema")
    if schema == SCHEMA:
        if not isinstance(raw_obj.get("stacks"), dict):
            return None, "index has no stacks table"
        return raw_obj, ""
    if schema is not None:
        return None, f"unknown index schema {schema!r}"
    if "stacks" in raw_obj:
        return None, "index has no schema but carries a stacks table — unrecognised"
    if not raw_obj:
        return {"schema": SCHEMA, "stacks": {}}, ""
    for sid, v in raw_obj.items():
        if not (re.fullmatch(r"[a-z0-9-]+", str(sid)) and isinstance(v, dict)
                and isinstance(v.get("filename"), str) and isinstance(v.get("sha256"), str)):
            return None, "index matches neither schema 2 nor the v1 layout"
    return {"schema": SCHEMA, "stacks": {}}, ""      # identified v1: start a fresh v2 index
