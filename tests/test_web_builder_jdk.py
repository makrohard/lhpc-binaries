"""`meshtastic-web.sh` pins a JDK 21 for the dev build only: upstream's sdk-kmp Gradle build needs it,
while stable republishes upstream's `build.tar` and needs no toolchain. The pin is checked like the
Node pin (exact release + sha256, exit 4 on a mismatch) before the upstream recipe runs. The checks
read the script text: a real build needs the network."""
from __future__ import annotations

import re
from pathlib import Path

BUILDER = Path(__file__).resolve().parents[1] / "builders" / "meshtastic-web.sh"


def _const(text, name):
    m = re.search(rf"^{name}=(\S+)$", text, re.M)
    assert m, f"{name} is not set at the top level of {BUILDER.name}"
    return m.group(1)


def _channel_blocks(text):
    """The top-level `if [ "$CHANNEL" = dev ]` block split into its dev and stable parts (the dev
    part holds a nested, indented if/else/fi, so only column-0 `else` / `fi` end a part)."""
    m = re.search(r'^if \[ "\$CHANNEL" = dev \]; then\n(.*?)^else\n(.*?)^fi\n', text, re.M | re.S)
    assert m, f"{BUILDER.name} has no top-level dev/stable split"
    return m.group(1), m.group(2)


def test_the_jdk_is_pinned_by_an_exact_release_and_sha256():
    text = BUILDER.read_text()
    assert re.fullmatch(r"21\.\d+\.\d+(\.\d+)?\+\d+", _const(text, "JDK_VERSION"))
    assert re.fullmatch(r"[0-9a-f]{64}", _const(text, "JDK_SHA256"))


def test_the_dev_build_checks_the_jdk_before_the_upstream_recipe():
    dev, _ = _channel_blocks(BUILDER.read_text())
    check = re.search(r'^\s*echo "\$JDK_SHA256  \$WORK/jdk\.tar\.gz" \| sha256sum -c - .*exit 4; }$', dev, re.M)
    assert check, "the dev build does not verify the JDK tarball's sha256 (exit 4)"
    home = re.search(r"^\s*export JAVA_HOME=/opt/jdk ", dev, re.M)
    assert home, "the dev build does not set JAVA_HOME=/opt/jdk"
    recipe = dev.find('echo "==> Upstream recipe"')
    assert recipe >= 0, "the dev build has no upstream recipe step"
    assert check.start() < home.start() < recipe


def test_the_stable_build_uses_no_java():
    _, stable = _channel_blocks(BUILDER.read_text())
    assert not re.search(r"jdk|java", stable, re.I)
