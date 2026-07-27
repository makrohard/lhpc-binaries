"""Single source of truth for a stack's DERIVED provenance (built_from + component pins).

Used by the TRUSTED publish job to INDEPENDENTLY re-derive what the untrusted build job claimed,
straight from lhpc's manifest (the recipe). The build container writes the fragment, but the
publisher never trusts its provenance: it re-derives here and requires exact equality.
"""

# The component whose pin the operator's `source_commit` overrides, per stack — the "principal"
# source each builder reads first. Every other source component keeps its manifest pin.
_PRINCIPAL = {
    "daemon": "loraham-daemon",
    "meshtastic": "meshtastic",
    "meshcom": "meshcom-qemu",
}


def derive(stack, source_commit=""):
    """Return (built_from, components) for `stack` from the CURRENT lhpc manifest, applying the
    trusted `source_commit` override to the principal component only. Fail closed on anything odd."""
    from lhpc.core.manifest import load_manifest
    comps = {}
    for st in load_manifest():
        if st.id != stack:
            continue
        for c in st.components:
            src = getattr(c, "source", None)
            if src is not None:
                comps[c.id] = src.pin_commit
    if not comps:
        raise SystemExit(f"lib_provenance: no source components for stack {stack!r}")
    principal = _PRINCIPAL.get(stack)
    if principal is None or principal not in comps:
        raise SystemExit(f"lib_provenance: no known principal component for stack {stack!r}")
    built_from = source_commit or comps[principal]
    if source_commit:
        comps[principal] = source_commit
    return built_from, comps
