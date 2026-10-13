"""Images for the menu bar and its menu, drawn at runtime.

Language badges copy the way macOS shows keyboard layouts in its input
menu: a dark rounded tag with the letters cut out of it. They are template
images, so the system tints them for light and dark menus and for the
highlighted row. Everything else uses SF Symbols — the icons the system's
own menus use since macOS 26.
"""

import math

import Quartz
from AppKit import (
    NSAttributedString,
    NSBezierPath,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSImage,
    NSImageSymbolConfiguration,
    NSMakeRect,
)

from hotkey import _KEYCODE_NAMES

# The menu bar glyph, in points; the PNGs in assets/menubar are drawn at 2x.
_STATUS_ICON_SIZE = 20.0


def language_badge(text: str, *, height: float = 15.0, font_size: float = 10.0,
                   filled: bool = True) -> NSImage:
    """A two-letter language tag («РУ», «EN») as a template image.

    `filled` is the input-menu look: letters cut out of a solid tag.
    Outlined is used in the menu bar, where a solid tag would read as the
    system's own keyboard-layout indicator sitting next to it.
    """
    font = NSFont.systemFontOfSize_weight_(font_size, NSFontWeightSemibold)
    label = NSAttributedString.alloc().initWithString_attributes_(text, {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: NSColor.blackColor(),
    })
    text_width = label.size().width
    width = float(max(math.ceil(text_width + 8.0), math.ceil(height * 1.45)))
    radius = 3.5 if filled else 3.0

    def draw(_rect):
        if filled:
            box = NSMakeRect(0.0, 0.0, width, height)
        else:
            box = NSMakeRect(0.5, 0.5, width - 1.0, height - 1.0)
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            box, radius, radius)
        NSColor.blackColor().set()
        # drawAtPoint takes the bottom-left of the line, below the baseline
        # by the descender; centre the capitals, which is all a badge holds.
        baseline = (height - font.capHeight()) / 2.0
        point = ((width - text_width) / 2.0, baseline + font.descender())
        if filled:
            path.fill()
            cg = NSGraphicsContext.currentContext().CGContext()
            Quartz.CGContextSetBlendMode(cg, Quartz.kCGBlendModeDestinationOut)
            label.drawAtPoint_(point)
            Quartz.CGContextSetBlendMode(cg, Quartz.kCGBlendModeNormal)
        else:
            path.setLineWidth_(1.0)
            path.stroke()
            label.drawAtPoint_(point)
        return True

    image = NSImage.imageWithSize_flipped_drawingHandler_(
        (width, height), False, draw)
    image.setTemplate_(True)
    image.setAccessibilityDescription_(text)
    return image


def symbol(name: str, point_size: float = 13.0) -> NSImage | None:
    """An SF Symbol sized for a menu row, as a template image."""
    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if image is None:
        return None
    config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
        point_size, NSFontWeightRegular)
    return image.imageWithSymbolConfiguration_(config) or image


def status_icon(path: str, badge: str | None = None) -> NSImage:
    """The menu bar glyph, with a language tag beside it when given one.

    The tag only appears while dictation is being translated — the state
    that is easy to forget you left on.
    """
    base = NSImage.alloc().initWithContentsOfFile_(path)
    base.setSize_((_STATUS_ICON_SIZE, _STATUS_ICON_SIZE))
    if not badge:
        base.setTemplate_(True)
        return base

    tag = language_badge(badge, height=12.0, font_size=8.5, filled=False)
    tag_w, tag_h = tag.size().width, tag.size().height
    gap = 1.0
    width = _STATUS_ICON_SIZE + gap + tag_w

    def draw(_rect):
        base.drawInRect_(NSMakeRect(0.0, 0.0, _STATUS_ICON_SIZE, _STATUS_ICON_SIZE))
        tag.drawInRect_(NSMakeRect(_STATUS_ICON_SIZE + gap,
                                   (_STATUS_ICON_SIZE - tag_h) / 2.0, tag_w, tag_h))
        return True

    image = NSImage.imageWithSize_flipped_drawingHandler_(
        (width, _STATUS_ICON_SIZE), False, draw)
    image.setTemplate_(True)
    return image


def key_equivalent(keycode: int) -> str:
    """The character NSMenuItem wants for a shortcut key; '' if it has none.

    Only for showing the shortcut beside a menu item: the shortcut itself
    is caught by the event tap whether or not the menu is open.
    """
    name = _KEYCODE_NAMES.get(keycode, "")
    if len(name) == 1:
        return name.lower()
    return {"Space": " ", "Tab": "\t", "Return": "\r"}.get(name, "")
