"""The smoke's decisions, without a browser. It sets the client's HTTPS switch as a user does: one
flip makes the choice the user's (upstream's later local-address default no longer applies); a
second flip only if the first left HTTP. It opens the connection dialog from either client's entry
button, and it judges "connected" on the visible text or the accessible names. The browser itself
runs only in the lab smoke; these drive the decisions with a fake switch and page texts."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "smoke_web_https", Path(__file__).resolve().parents[1] / "builders" / "smoke_web.py")
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)


class Switch:
    """`stale` = the first read after a click still shows the old value (React not rendered yet)."""

    def __init__(self, checked, default_flips_before_click=False, stale=False):
        self.checked, self.flip, self.stale, self.clicks = checked, default_flips_before_click, stale, 0
        self.shown = checked

    def click(self):
        if self.flip:                     # upstream's default lands between our read and our click
            self.checked, self.flip = False, False
        self.checked = not self.checked
        self.clicks += 1
        if not self.stale:
            self.shown = self.checked

    def changed_within(self, value, ms):
        self.shown = self.checked         # the render lands while we wait for a change

    def is_https_within(self, ms):
        return self.shown


@pytest.mark.parametrize("checked, flip, stale, clicks", [
    (False, False, False, 1),             # the default already chose HTTP: one flip -> HTTPS
    (True, False, False, 2),              # HTTPS untouched: off, then on (the user's choice now)
    (True, True, False, 1),               # the default flips to HTTP just before our click
    (True, False, True, 2),               # a stale read right after the click still says HTTPS
])
def test_the_switch_ends_on_https_as_the_users_choice(checked, flip, stale, clicks):
    s = Switch(checked, flip, stale)
    read = "true" if checked else "false"
    assert sw.set_https(s, read, s.changed_within, s.is_https_within) == clicks
    assert s.checked and s.clicks == clicks


@pytest.mark.parametrize("label", ["Add Connection", "Connect a radio", "add connection"])
def test_the_entry_button_of_a_release_client_and_of_upstream_main_opens_the_dialog(label):
    assert sw.ENTRY.fullmatch(label)


@pytest.mark.parametrize("label", ["Add connection to a group", "Please add connection", "Connect", "Close connections"])
def test_other_buttons_are_not_the_entry(label):
    assert not sw.ENTRY.search(label)


NAME = "Meshtastic 1a2b"


@pytest.mark.parametrize("text, aria, shown", [
    (f"{NAME}\nConnected", "", True),                                  # a release client: visible text
    ("Messages", f'- button "Device {NAME}, Connected"', True),         # upstream main: an icon's label only
    (f"{NAME}\nDisconnected", f'- button "Device {NAME}, Disconnected"', False),
    (f"{NAME}\nConnecting", f'- button "Device {NAME}, Connecting"', False),
    ("Connected", '- button "Device Other node, Connected"', False),    # someone else's name
    (NAME, '- status "Connected"', False),                              # name and status in different parts
])
def test_connected_needs_the_long_name_and_connected_in_the_same_part(text, aria, shown):
    assert sw.shows_connected(NAME, text, aria) is shown
