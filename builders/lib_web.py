"""Pure helpers of the meshtastic-web builder and its lab smoke (no network, no subprocess)."""

import re


def pick_daemon_entry(idx, pin):
    """The meshtasticd to smoke-test a web client against: the first (newest) schema-3 meshtastic
    entry built from the controller's `meshtastic` pin, or None."""
    for e in idx.get("stacks", {}).get("meshtastic", []):
        if e["components"].get("meshtastic") == pin:
            return e
    return None


def peeled_commit(ls_remote, tag):
    """The commit a release tag names, from `git ls-remote <repo> refs/tags/<tag>*` output: the
    peeled `^{}` line for an annotated tag, else the tag's own line. None when the tag is absent."""
    rows = dict(reversed(line.split("\t", 1)) for line in ls_remote.splitlines() if "\t" in line)
    for ref in (f"refs/tags/{tag}^{{}}", f"refs/tags/{tag}"):
        sha = rows.get(ref, "")
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    return None


def main_bundle(index_html):
    """The client's main bundle: the module script index.html loads (the file the broken-client
    control truncates). None when there is none."""
    m = re.search(r'<script[^>]*type="module"[^>]*src="/?([^"]+)"', index_html)
    return m.group(1) if m else None
