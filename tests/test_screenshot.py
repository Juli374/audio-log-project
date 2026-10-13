"""Tests for the screenshot-with-markup feature.

PNG metadata handling, the system-shortcut mapping, and the event-tap
dispatch for the second shortcut. No windows, no real screencapture.
"""

import os
import struct
import sys
import unittest
import zlib
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_SHIFT = 1 << 17
_CONTROL = 1 << 18
_OPTION = 1 << 19
_COMMAND = 1 << 20
_CAPS_LOCK = 1 << 16


def _chunk(kind: bytes, body: bytes) -> bytes:
    return (struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))


def _png(width: int, height: int, dpi: int | None) -> bytes:
    """A tiny valid RGBA PNG, optionally carrying a pHYs chunk."""
    raw = b"".join(b"\x00" + b"\xff\x00\x00\xff" * width for _ in range(height))
    out = b"\x89PNG\r\n\x1a\n"
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    if dpi is not None:
        per_metre = int(round(dpi / 0.0254))
        out += _chunk(b"pHYs", struct.pack(">IIB", per_metre, per_metre, 1))
    out += _chunk(b"IDAT", zlib.compress(raw))
    out += _chunk(b"IEND", b"")
    return out


class PngMetadataTests(unittest.TestCase):
    def test_size_from_ihdr(self):
        from screenshot import png_size
        self.assertEqual(png_size(_png(7, 3, None)), (7, 3))
        self.assertEqual(png_size(b"not a png"), (0, 0))

    def test_scale_from_phys(self):
        from screenshot import png_scale
        self.assertEqual(png_scale(_png(4, 4, 144)), 2.0)
        self.assertEqual(png_scale(_png(4, 4, 72)), 1.0)
        self.assertEqual(png_scale(_png(4, 4, None)), 1.0)

    def test_stamp_scale_puts_dpi_back(self):
        from screenshot import png_scale, stamp_scale
        bare = _png(6, 4, None)
        self.assertEqual(png_scale(bare), 1.0)
        stamped = stamp_scale(bare, 2.0)
        self.assertEqual(png_scale(stamped), 2.0)
        # 1x shots are left untouched.
        self.assertIs(stamp_scale(bare, 1.0), bare)

    def test_stamp_scale_replaces_an_existing_dpi(self):
        from screenshot import png_scale, stamp_scale
        stamped = stamp_scale(_png(6, 4, 72), 2.0)
        self.assertEqual(png_scale(stamped), 2.0)
        self.assertEqual([k for k, _ in _chunks(stamped)].count(b"pHYs"), 1)

    def test_stamp_scale_keeps_colour_and_pixels(self):
        """The washed-out paste: the canvas's sRGB chunk must survive.

        The editor's PNG comes from WebKit, which tags it sRGB. The old
        NSBitmapImageRep round trip replaced that with gAMA 2.2 (inverted),
        and Chromium-based apps rendered the result faded.
        """
        from screenshot import stamp_scale
        canvas = _webkit_like_png()
        before = dict(_chunks(canvas))
        self.assertIn(b"sRGB", before)

        after = _chunks(stamp_scale(canvas, 2.0))
        kinds = [k for k, _ in after]
        self.assertIn(b"sRGB", kinds)
        self.assertNotIn(b"gAMA", kinds)
        self.assertLess(kinds.index(b"pHYs"), kinds.index(b"IDAT"))
        idat = b"".join(body for k, body in after if k == b"IDAT")
        self.assertEqual(idat, before[b"IDAT"])

    def test_stamp_scale_leaves_broken_input_alone(self):
        from screenshot import stamp_scale
        self.assertEqual(stamp_scale(b"not a png", 2.0), b"not a png")
        cut = _png(6, 4, None)[:40]
        self.assertEqual(stamp_scale(cut, 2.0), cut)


def _chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    out, pos = [], 8
    while pos + 12 <= len(png):
        n = struct.unpack(">I", png[pos:pos + 4])[0]
        out.append((png[pos + 4:pos + 8], png[pos + 8:pos + 8 + n]))
        pos += 12 + n
    return out


def _webkit_like_png() -> bytes:
    """Encode an sRGB bitmap with ImageIO, the way WebKit's toDataURL does."""
    import Foundation
    import Quartz
    w = h = 8
    cs = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
    ctx = Quartz.CGBitmapContextCreate(
        None, w, h, 8, w * 4, cs, Quartz.kCGImageAlphaPremultipliedLast)
    Quartz.CGContextSetRGBFillColor(ctx, 1.0, 0.23, 0.19, 1.0)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, w, h))
    data = Foundation.NSMutableData.data()
    dest = Quartz.CGImageDestinationCreateWithData(data, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(
        dest, Quartz.CGBitmapContextCreateImage(ctx), None)
    Quartz.CGImageDestinationFinalize(dest)
    return bytes(data)


class SystemShortcutMappingTests(unittest.TestCase):
    def test_screenshot_keys_map_to_symbolic_ids(self):
        from system_hotkeys import system_shortcut_id
        self.assertEqual(system_shortcut_id(21, _SHIFT | _COMMAND), 30)
        self.assertEqual(system_shortcut_id(21, _SHIFT | _COMMAND | _CONTROL), 31)
        self.assertEqual(system_shortcut_id(20, _SHIFT | _COMMAND), 28)
        self.assertEqual(system_shortcut_id(23, _SHIFT | _COMMAND), 184)

    def test_other_combinations_are_not_system_shortcuts(self):
        from system_hotkeys import system_shortcut_id
        self.assertIsNone(system_shortcut_id(2, _OPTION))           # ⌥D
        self.assertIsNone(system_shortcut_id(21, _OPTION | _SHIFT))  # ⌥⇧4

    def test_claim_and_release_talk_to_the_window_server(self):
        from system_hotkeys import SystemShortcuts
        lib = MagicMock()
        lib.CGSIsSymbolicHotKeyEnabled.return_value = True
        lib.CGSSetSymbolicHotKeyEnabled.return_value = 0
        s = SystemShortcuts()
        s._lib = lib
        s.claim(21, _SHIFT | _COMMAND)
        lib.CGSSetSymbolicHotKeyEnabled.assert_called_once_with(30, False)
        self.assertEqual(s.claimed, 30)
        s.claim(2, _OPTION)   # not a system key → the old claim is released
        lib.CGSSetSymbolicHotKeyEnabled.assert_called_with(30, True)
        self.assertIsNone(s.claimed)

    def test_already_disabled_shortcut_is_left_alone(self):
        from system_hotkeys import SystemShortcuts
        lib = MagicMock()
        lib.CGSIsSymbolicHotKeyEnabled.return_value = False
        s = SystemShortcuts()
        s._lib = lib
        s.claim(21, _SHIFT | _COMMAND)
        lib.CGSSetSymbolicHotKeyEnabled.assert_not_called()
        self.assertIsNone(s.claimed)
        s.release()
        lib.CGSSetSymbolicHotKeyEnabled.assert_not_called()


class TapDispatchTests(unittest.TestCase):
    """The one event tap serves both shortcuts."""

    def _listener(self, translate=None, screenshot=None, enabled=True):
        from config import Config
        from hotkey import HotkeyListener
        config = Config()
        config.screenshot_enabled = enabled
        return HotkeyListener(
            config=config,
            on_activate=MagicMock(), on_deactivate=MagicMock(),
            on_translate_toggle=translate, on_screenshot=screenshot)

    def _press(self, hk, keycode, flags):
        import hotkey
        event = object()
        with patch.object(hotkey, "CGEventGetIntegerValueField",
                          return_value=keycode), \
             patch.object(hotkey, "CGEventGetFlags", return_value=flags), \
             patch.object(hotkey.AppHelper, "callAfter",
                          side_effect=lambda f, *a: f(*a)):
            return hk._tap_callback(None, hotkey.kCGEventKeyDown, event, None), event

    def test_default_screenshot_shortcut_fires_and_is_swallowed(self):
        shot, tr = MagicMock(), MagicMock()
        hk = self._listener(translate=tr, screenshot=shot)
        result, _ = self._press(hk, 21, _SHIFT | _COMMAND)
        self.assertIsNone(result)
        shot.assert_called_once()
        tr.assert_not_called()

    def test_caps_lock_and_fn_do_not_break_the_match(self):
        shot = MagicMock()
        hk = self._listener(screenshot=shot)
        result, _ = self._press(hk, 21, _SHIFT | _COMMAND | _CAPS_LOCK | (1 << 23))
        self.assertIsNone(result)
        shot.assert_called_once()

    def test_extra_modifier_is_a_different_shortcut(self):
        shot = MagicMock()
        hk = self._listener(screenshot=shot)
        result, event = self._press(hk, 21, _SHIFT | _COMMAND | _CONTROL)
        self.assertIs(result, event)
        shot.assert_not_called()

    def test_translate_shortcut_still_works(self):
        shot, tr = MagicMock(), MagicMock()
        hk = self._listener(translate=tr, screenshot=shot)
        result, _ = self._press(hk, 2, _OPTION)
        self.assertIsNone(result)
        tr.assert_called_once()
        shot.assert_not_called()

    def test_disabled_screenshot_shortcut_passes_through(self):
        shot = MagicMock()
        hk = self._listener(screenshot=shot, enabled=False)
        result, event = self._press(hk, 21, _SHIFT | _COMMAND)
        self.assertIs(result, event)
        shot.assert_not_called()

    def test_plain_key_passes_through(self):
        shot = MagicMock()
        hk = self._listener(screenshot=shot)
        result, event = self._press(hk, 21, 0)
        self.assertIs(result, event)
        shot.assert_not_called()

    def test_system_shortcut_is_claimed_only_while_tap_is_armed(self):
        hk = self._listener(screenshot=MagicMock())
        hk._system_shortcuts = MagicMock()
        hk._tap = None
        hk._sync_system_shortcut()
        hk._system_shortcuts.release.assert_called_once()
        hk._system_shortcuts.claim.assert_not_called()

        hk._tap = object()
        hk._sync_system_shortcut()
        hk._system_shortcuts.claim.assert_called_once_with(21, _SHIFT | _COMMAND)

        hk.set_screenshot_shortcut(21, _OPTION | _SHIFT, True)
        hk._system_shortcuts.claim.assert_called_with(21, _OPTION | _SHIFT)

        hk.set_screenshot_shortcut(21, _OPTION | _SHIFT, False)
        self.assertEqual(hk._system_shortcuts.release.call_count, 2)


if __name__ == "__main__":
    unittest.main()
