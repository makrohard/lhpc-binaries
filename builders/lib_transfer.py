#!/usr/bin/env python3
"""Print the transfer line that ENDED a failed `lhpc build`, else nothing.

    python3 lib_transfer.py <captured-lhpc-build-output-file>

`build_stack` (lib-build.sh) repeats `lhpc build` only when this prints a line: a network or server
failure (a name that does not resolve, a refused or reset connection, a timeout, an HTTP 5xx) was
the error the failing step ENDED with. A transfer the build recovered from earlier, followed by any
other failure, is not one: only the decisive line decides. An HTTP status in the deciding line decides first: 5xx is one, a 4xx never is.

The text judged is the failing step's own log, named by the last `[failed] build <component>
(rc N, log <path>)` line `lhpc build` prints (the controller's TYPED_BUILD_FAILURE), else the
`lhpc build` output without those lines. Its decisive line is its last error line; two wrapper
lines that only restate the error above them (`meson setup failed`, `Git command failed`) hand the
decision to the line above, at most twice. Default deny: the deciding line is the first line from the
end that is not blank, not one of those wrappers, not meson's log pointer and not lhpc's own last
line (`[progress] longest quiet N s`); when that line is not a
transfer error (a compile or link failure, a make/ninja/CMake end line, any output not known here),
nothing is repeated, whatever transfer line came earlier.

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
    r"|curl: \((?:5|6|7|28|35|52|56)\)")
# An HTTP status in the deciding line decides first: 5xx is a transfer failure, anything else
# (a 4xx) never is, whatever else the line holds (a URL can contain "ReadTimeoutError").
HTTP_STATUS = re.compile(r"(?:returned error|HTTP [Ee]rror):? (\d{3})|status code '(\d{3})'")
WRAPPER_STEPS = 2
# What may follow the deciding line besides blank lines and the wrappers, each a whole line: meson's
# log pointer, and the line lhpc itself writes last in every build step's log since v0.11.10
# (lhpc/core/jobs.py `progress.quiet_line`: "[progress] longest quiet 7.8 s"). lhpc's other
# trailer, `[TIMED OUT ... job was KILLED ...]`, is not here: a killed step is no transfer.
TRAILER = re.compile(r"A full log can be found at .*|\[progress\] longest quiet \d+(?:\.\d+)? s")
# gcc/clang: `file:line[:col]: [fatal ]error:` (lowercase). Meson's `file:l:c: ERROR:` is not one.
COMPILER = re.compile(r"\S+:\d+(?::\d+)?: (?:fatal )?error:")


def step_text(output: str) -> str:
    """The failing step's own log when `lhpc build` named one that can be read, else its output."""
    found = list(TYPED_BUILD_FAILURE.finditer(output))
    if found:
        try:
            # Whole: the decisive line is the LAST error, wherever it is; a tail window could cut it.
            return Path(found[-1].group(2)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return "\n".join(ln for ln in output.splitlines() if not TYPED_BUILD_FAILURE.search(ln))


def decisive_transfer(text: str) -> str:
    """The deciding line when it is a transfer failure, else "". Default deny: read from the end,
    only blank lines, meson's "A full log can be found at", lhpc's "[progress] longest quiet N s" and
    at most two wrapper lines may come
    after the deciding line; the first other line decides, and anything but a transfer error there
    (a compile or link failure, a make/ninja/CMake end line, any unknown output) means no repeat."""
    wrappers = 0
    for raw in reversed(text.splitlines()):
        line = raw.strip()
        if not line or TRAILER.fullmatch(line):
            continue
        if WRAPPER.search(line) and wrappers < WRAPPER_STEPS:
            wrappers += 1
            continue
        return " ".join(line.split())[:300] if is_transfer(line) else ""
    return ""


def is_transfer(line: str) -> bool:
    """An error line that a network or server failure ended. A compiler diagnostic never is, whatever
    it names; an HTTP status decides before any transport word (5xx is one, a 4xx never is)."""
    if not ERROR_LINE.search(line) or COMPILER.match(line):
        return False
    status = HTTP_STATUS.search(line)
    if status:
        return (status.group(1) or status.group(2)).startswith("5")
    return bool(TRANSPORT.search(line))


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
