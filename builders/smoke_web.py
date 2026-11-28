"""The meshtastic-web smoke: a real config exchange between the web client and a meshtasticd.

    smoke_web.py <client-url> <device-host:port> <meshtasticd-log> [timeout-s]

Headless Chromium opens the client meshtasticd serves, adds an HTTP(S) connection to that daemon
through the client's own dialog (the steps upstream's e2e suite uses, e2e/pages/ConnectionPage.ts:
the first-run wizard marked as skipped, then "Add Connection" on a release client or "Connect a
radio" -> "Add connection" on upstream main), and must then render the node's long name together
with "Connected", as visible text or as an accessible name (upstream main shows the device status
as the label of an icon button). The long name is not known to the client in advance:
it is read from the daemon's own log ("[NodeInfo] Send owner !<num>/<long>/<short>"), so it can
only appear on the page if the want-config / node-info exchange really happened.

Exit 0 = PASS; 10 = FAIL (any step not done before the deadline). The browser is the system's
Chromium (SMOKE_BROWSER, default /usr/bin/chromium); nothing is downloaded.
"""

import os
import re
import sys
import time

OWNER_RE = re.compile(r"Send owner ![0-9a-f]+/([^/\r\n]+)/")
# The page's button that opens the connection dialog: a release client's "Add Connection", or
# upstream main's "Connect a radio" (it opens the Connections drawer first).
ENTRY = re.compile(r"^(add connection|connect a radio)$", re.I)
# The flag the client sets once a user finished or skipped its first-run wizard (upstream main sends
# a fresh browser there); upstream's own e2e sets it the same way, and a release client ignores it.
WELCOME_DISMISSED = "localStorage.setItem('meshtastic.welcomeDismissed', '1')"


def long_name(log_text):
    """The node's long name as the daemon announced it, or None before it did."""
    m = OWNER_RE.search(log_text)
    return m.group(1) if m else None


def shows_connected(name, text, aria):
    """True when the page shows the long name together with "Connected", either in its visible
    text or in its accessible names (the aria snapshot), each judged on its own."""
    return any(name in part and "Connected" in part for part in (text, aria))


def _read(path):
    try:
        with open(path, errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def set_https(switch, read, changed_within, is_https_within):
    """Set the dialog's HTTPS switch the way a user does. Upstream defaults a local address
    (127.0.0.1 included) to HTTP as soon as the browser answers its Local Network Access query,
    but only while the user has not flipped the switch. So flip it first (from then on the default
    can no longer change it), let the switch move off the value `read` before it (a stale read
    must not decide), then flip back if that left HTTP. Returns the clicks made."""
    switch.click()
    changed_within(read, 2000)          # no change in 2 s: the default moved it first (still judged)
    if is_https_within(2000):
        return 1
    switch.click()
    return 2


def run(url, host, log_path, timeout):
    from playwright.sync_api import expect, sync_playwright

    deadline = time.monotonic() + timeout
    left_ms = lambda: max(1, int((deadline - time.monotonic()) * 1000))  # noqa: E731
    step = "start the browser"
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("SMOKE_BROWSER", "/usr/bin/chromium"),
            headless=True, args=["--ignore-certificate-errors", "--no-sandbox"])
        try:
            page = browser.new_page(ignore_https_errors=True)
            page.set_default_timeout(left_ms())
            page.add_init_script(WELCOME_DISMISSED)
            step = "open the client"
            page.goto(url, wait_until="load")
            step = "open the connection dialog"
            page.get_by_role("button", name=ENTRY).first.click()
            dialog = page.get_by_role("dialog")
            add = dialog.get_by_role("button", name="Add connection", exact=True)
            tab = dialog.get_by_role("tab", name="HTTP")
            add.or_(tab).first.wait_for()
            if add.is_visible():                # upstream main: the drawer lists saved connections first
                add.click()
            step = "fill the HTTP connection"
            tab.click()
            dialog.locator("#name-http").fill("LHPC smoke")
            dialog.locator("#url").fill(host)
            switch = dialog.get_by_role("switch")             # meshtasticd's webserver is HTTPS-only

            def is_https_within(ms):
                try:
                    expect(switch).to_have_attribute("aria-checked", "true", timeout=ms)
                    return True
                except AssertionError:
                    return False

            def changed_within(value, ms):
                try:
                    expect(switch).not_to_have_attribute("aria-checked", value or "", timeout=ms)
                except AssertionError:
                    pass
            read = switch.get_attribute("aria-checked")
            clicks = set_https(switch, read, changed_within, is_https_within)
            if not is_https_within(2000):
                return f"the HTTPS switch did not stay on (read {read!r}, {clicks} click(s))"
            print(f"SMOKE: HTTPS switch read {read!r} before the first click; {clicks} click(s)",
                  flush=True)
            step = "test the connection"
            dialog.get_by_role("button", name="Test connection").click()
            save = dialog.get_by_role("button", name="Save connection")
            while not save.is_enabled():            # enabled only once the device answered
                if time.monotonic() > deadline:
                    return ("the device never answered the client's connection test (HTTPS switch "
                            f"now {switch.get_attribute('aria-checked')!r})")
                page.wait_for_timeout(250)
            step = "save the connection"
            save.click()
            step = "wait for the node's long name as connected"
            name = None
            while time.monotonic() < deadline:
                name = name or long_name(_read(log_path))
                body = page.locator("body")
                text, aria = body.inner_text(timeout=left_ms()), body.aria_snapshot(timeout=left_ms())
                if name and shows_connected(name, text, aria):
                    seen = "visible text" if shows_connected(name, text, "") else "accessible names"
                    print(f"SMOKE: {name!r} shown as connected in the page's {seen}", flush=True)
                    return ""
                page.wait_for_timeout(500)
            if not name:
                return "the daemon never announced its owner (no 'Send owner' line in its log)"
            return f"the client did not render the node's long name {name!r} as connected"
        except Exception as exc:                    # name the step: a bare locator timeout says too little
            first = str(exc).splitlines()[0] if str(exc) else ""
            raise RuntimeError(f"at '{step}': {type(exc).__name__}: {first}") from exc
        finally:
            browser.close()


def main(argv):
    url, host, log_path = argv[1], argv[2], argv[3]
    timeout = float(argv[4]) if len(argv) > 4 else 120.0
    t0 = time.monotonic()
    try:
        err = run(url, host, log_path, timeout)
    except Exception as exc:                        # a missing element, a crash, a timeout: FAIL
        err = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
    took = time.monotonic() - t0
    if err:
        print(f"SMOKE: FAIL after {took:.0f} s: {err}", file=sys.stderr)
        return 10
    print(f"SMOKE: PASS (the web client rendered the node's long name over the HTTP API in {took:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
