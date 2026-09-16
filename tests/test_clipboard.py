"""Tests for clipboard preservation.

Three features share the one system clipboard: dictation pastes text,
translation copies the selection, and the screenshot editor puts a picture
there. These check that the picture survives the other two.

Everything runs on a private NSPasteboard, so the developer's own clipboard
is never touched.
"""

import os
import struct
import sys
import time
import unittest
import zlib
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import AppKit
import Foundation


def _png(width: int = 4, height: int = 3) -> bytes:
    """A tiny valid PNG, standing in for a screenshot."""
    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _nsdata(raw: bytes):
    return Foundation.NSData.dataWithBytes_length_(raw, len(raw))


class ClipboardTestCase(unittest.TestCase):
    """Gives each test its own pasteboard, and its own `output.NSPasteboard`."""

    def setUp(self):
        self.pb = AppKit.NSPasteboard.pasteboardWithUniqueName()
        self.addCleanup(self.pb.releaseGlobally)

    def put_png(self, png=None):
        png = png or _png()
        self.pb.clearContents()
        self.pb.setData_forType_(_nsdata(png), AppKit.NSPasteboardTypePNG)
        return png

    def put_text(self, text="hello"):
        self.pb.clearContents()
        self.pb.setString_forType_(text, AppKit.NSPasteboardTypeString)
        return text

    def png_on_board(self):
        data = self.pb.dataForType_(AppKit.NSPasteboardTypePNG)
        return bytes(data) if data is not None else None


class SnapshotTests(ClipboardTestCase):
    def test_empty_pasteboard_snapshots_to_none(self):
        from output import snapshot_pasteboard
        self.pb.clearContents()
        self.assertIsNone(snapshot_pasteboard(self.pb))

    def test_picture_survives_a_round_trip(self):
        from output import restore_pasteboard, snapshot_pasteboard
        png = self.put_png()
        kept = snapshot_pasteboard(self.pb)

        self.put_text("something else entirely")
        self.assertIsNone(self.png_on_board())

        restore_pasteboard(self.pb, kept)
        self.assertEqual(self.png_on_board(), png)
        self.assertIsNone(self.pb.stringForType_(AppKit.NSPasteboardTypeString))

    def test_every_flavour_comes_back(self):
        from output import restore_pasteboard, snapshot_pasteboard
        self.pb.clearContents()
        self.pb.setString_forType_("rich", AppKit.NSPasteboardTypeString)
        self.pb.setData_forType_(_nsdata(b"{\\rtf1 rich}"),
                                 AppKit.NSPasteboardTypeRTF)
        kept = snapshot_pasteboard(self.pb)

        self.put_png()
        restore_pasteboard(self.pb, kept)

        self.assertEqual(self.pb.stringForType_(AppKit.NSPasteboardTypeString),
                         "rich")
        self.assertIsNotNone(self.pb.dataForType_(AppKit.NSPasteboardTypeRTF))

    def test_offers_text_tells_a_picture_from_a_quote(self):
        from output import offers_text, snapshot_pasteboard
        self.put_png()
        self.assertFalse(offers_text(snapshot_pasteboard(self.pb)))
        self.put_text()
        self.assertTrue(offers_text(snapshot_pasteboard(self.pb)))


class PasteTests(ClipboardTestCase):
    """paste_text() borrows a picture; it still takes text outright."""

    def setUp(self):
        super().setUp()
        board = MagicMock()
        board.generalPasteboard.return_value = self.pb
        self.enterContext(patch("output.NSPasteboard", board))
        self.enterContext(patch("output.is_trusted", return_value=True))
        self.enterContext(patch("output._post_cmd_v"))
        self.enterContext(patch("output._RESTORE_DELAY", 0.05))

    def _settle(self):
        for t in __import__("threading").enumerate():
            if t.name == "clipboard-restore":
                t.join(timeout=3)

    def test_screenshot_comes_back_after_dictation(self):
        from output import paste_text
        png = self.put_png()
        paste_text("расшифровка")
        # The text has to be on the clipboard at the moment of the ⌘V…
        self._settle()
        # …and the picture back on it once the paste has landed.
        self.assertEqual(self.png_on_board(), png)

    def test_text_is_replaced_exactly_as_before(self):
        from output import paste_text
        self.put_text("what I copied earlier")
        paste_text("расшифровка")
        self._settle()
        self.assertEqual(self.pb.stringForType_(AppKit.NSPasteboardTypeString),
                         "расшифровка")

    def test_a_copy_made_meanwhile_wins(self):
        from output import paste_text
        self.put_png()
        with patch("output._post_cmd_v",
                   side_effect=lambda: self.put_text("copied by hand")):
            paste_text("расшифровка")
        self._settle()
        self.assertEqual(self.pb.stringForType_(AppKit.NSPasteboardTypeString),
                         "copied by hand")
        self.assertIsNone(self.png_on_board())

    def test_nothing_is_restored_when_the_paste_cannot_happen(self):
        from output import paste_text
        self.put_png()
        with patch("output.is_trusted", return_value=False), \
             patch("output.request_accessibility"):
            paste_text("расшифровка")
        self._settle()
        # No Accessibility means no ⌘V, so the text is the only copy there is.
        self.assertEqual(self.pb.stringForType_(AppKit.NSPasteboardTypeString),
                         "расшифровка")


class TranslateTests(ClipboardTestCase):
    """⌥D copies the selection; the picture must come back afterwards."""

    def test_grab_selected_text_restores_a_picture(self):
        import translate_popup
        png = self.put_png()

        def fake_copy():
            self.put_text("the selection")

        # The patch has to outlive the call: the restore runs on a delayed
        # thread that asks for generalPasteboard() again when it wakes.
        with patch("translate_popup.NSPasteboard") as board, \
             patch("translate_popup._wait_for_modifier_release", return_value=0.0), \
             patch("translate_popup._post_cmd_c", side_effect=fake_copy):
            board.generalPasteboard.return_value = self.pb
            text = translate_popup.grab_selected_text()
            self.assertEqual(text, "the selection")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and self.png_on_board() is None:
                time.sleep(0.05)
            self.assertEqual(self.png_on_board(), png)


if __name__ == "__main__":
    unittest.main()
