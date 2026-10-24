"""The smoke sets the client's HTTPS switch as a user does: one flip makes the choice the user's
(upstream's later local-address default no longer applies); a second flip only if the first left HTTP.
The browser itself runs only in the lab smoke; these drive the decision with a fake switch."""
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
