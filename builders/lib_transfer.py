#!/usr/bin/env python3
"""Print the transfer line that ENDED a failed `lhpc build`, else nothing.

    python3 lib_transfer.py <captured-lhpc-build-output-file>

`build_stack` (lib-build.sh) repeats `lhpc build` only when this prints a line: a network or server
failure (a name that does not resolve, a refused or reset connection, a timeout, an HTTP 5xx) was
the error the failing step ENDED with. A transfer the build recovered from earlier, followed by any
other failure, is not one: only the decisive line decides. An HTTP 4xx is never a transfer.

The text judged is the failing step's own log, named by the last `[failed] build <component>
(rc N, log <path>)` line `lhpc build` prints (the controller's TYPED_BUILD_FAILURE), else the
`lhpc build` output without those lines. Its decisive line is its last error line; two wrapper
lines that only restate the error above them (`meson setup failed`, `Git command failed`) hand the
decision to the error line above, at most twice.

The patterns start from the controller release lane's `_DECISIVE` and `_TRANSPORT`
(testlab/lhpc_testlab/release.py) and add what the builder meets and the lane does not know:
PlatformIO's `status code '5xx'` and meson's `file:line:col: ERROR:` prefix. A shape missing here
fails safe: no transfer, no repeat, the build fails as it would have.

Exit status is 0 whether or not a transfer decided; only a usage error is nonzero.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TYPED_BUILD_FAILURE = re.compile(r"\[failed\] build (\S+) \(rc \d+, log (\S+)\)")
ERROR_LINE = re.compile(r"^\s*(?:\S+:\d+:\d+: )?(?:ERR\b|ERROR:|error:|fatal:|curl: \(\d+\)"
                        r"|\w*(?:Exception|Error):)|download failed")
WRAPPER = re.compile(r"meson setup failed|Git command failed")
TRANSPORT = re.compile(
    r"Could not resolve host|Failed to connect|Connection (?:refused|reset|timed out)|timed out"
    r"|Temporary failure in name resolution|Name or service not known|Network is unreachable"
    r"|ConnectionError|ReadTimeoutError|ConnectTimeoutError|urlopen error \[Errno"
    r"|(?:returned error|HTTP [Ee]rror):? 5\d\d|status code '5\d\d'"
    r"|curl: \((?:5|6|7|28|35|52|56)\)")
WRAPPER_STEPS = 2


def step_text(output: str) -> str:
    """The failing step's own log when `lhpc build` named one that can be read, else its output."""
    found = list(TYPED_BUILD_FAILURE.finditer(output))
    if found:
        try:
            return Path(found[-1].group(2)).read_text(encoding="utf-8", errors="replace")[-20000:]
        except OSError:
            pass
    return "\n".join(ln for ln in output.splitlines() if not TYPED_BUILD_FAILURE.search(ln))


def decisive_transfer(text: str) -> str:
    """The deciding error line when it is a transfer failure, else ""."""
    errors = [ln.strip() for ln in text.splitlines() if ERROR_LINE.search(ln)]
    if not errors:
        return ""
    at = len(errors) - 1
    for _ in range(WRAPPER_STEPS):
        if at > 0 and WRAPPER.search(errors[at]):
            at -= 1
    line = errors[at]
    return " ".join(line.split())[:300] if TRANSPORT.search(line) else ""


def main(argv: list) -> int:
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    line = decisive_transfer(step_text(Path(argv[0]).read_text(errors="replace")))
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
