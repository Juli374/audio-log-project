#!/usr/bin/env python3
"""Draw AudioLog's icons — the Dock icon and the menu bar glyphs.

Run with the project venv:  .venv/bin/python tools/make-icons.py

Writes:
  assets/AppIcon.iconset/*.png  → assets/AppIcon.icns   (Dock / Finder)
  assets/menubar/*.png                                   (menu bar states)

Menu bar glyphs are *template* images: pure black plus alpha. macOS recolours
them for the light or dark menu bar itself, so there is one file per state,
not two. Everything is drawn with CoreGraphics — no image libraries needed.
"""
import os
import subprocess
import sys

from AppKit import (
    NSBezierPath, NSBitmapImageRep, NSCalibratedRGBColorSpace, NSColor,
    NSGradient, NSGraphicsContext,
)
from Foundation import NSMakeRect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
ICONSET = os.path.join(ASSETS, "AppIcon.iconset")
MENUBAR = os.path.join(ASSETS, "menubar")
PNG_TYPE = 4  # NSBitmapImageFileTypePNG

# Brand gradient: indigo → deep violet.
BRAND_TOP = (99, 91, 255)
BRAND_BOTTOM = (46, 26, 138)

# Waveform silhouettes per state. Values are fractions of the glyph height.
WAVES = {
    "idle":       [0.30, 0.58, 1.00, 0.58, 0.30],
    "recording":  [0.55, 0.90, 1.00, 0.90, 0.55],
}


def _canvas(px):
    rep = NSBitmapImageRep.alloc().\
        initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, int(px), int(px), 8, 4, True, False,
            NSCalibratedRGBColorSpace, 0, 0)
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    ctx.setShouldAntialias_(True)
    return rep


def _write(rep, path):
    NSGraphicsContext.restoreGraphicsState()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rep.representationUsingType_properties_(PNG_TYPE, {}).\
        writeToFile_atomically_(path, True)
    return path


def _rgb(r, g, b, a=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(
        r / 255.0, g / 255.0, b / 255.0, a)


def _rounded(rect, radius):
    return NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        rect, radius, radius)


def _bars(cx, cy, height, fractions, weight, color):
    """Centred waveform: rounded vertical bars, tallest in the middle."""
    gap = weight * 0.85
    total = len(fractions) * weight + (len(fractions) - 1) * gap
    x = cx - total / 2
    color.set()
    for f in fractions:
        h = max(height * f, weight)  # never thinner than it is wide
        _rounded(NSMakeRect(x, cy - h / 2, weight, h), weight / 2).fill()
        x += weight + gap


def _dots(cx, cy, size, color):
    """Three dots — the "working on it" state."""
    d = size * 0.22
    gap = d * 0.85
    total = 3 * d + 2 * gap
    x = cx - total / 2
    color.set()
    for _ in range(3):
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(x, cy - d / 2, d, d)).fill()
        x += d + gap


# ── Dock icon ───────────────────────────────────────────────────────────────

def app_icon(px):
    rep = _canvas(px)
    body = _rounded(NSMakeRect(px * 0.055, px * 0.055, px * 0.89, px * 0.89),
                    px * 0.225)
    NSGradient.alloc().initWithStartingColor_endingColor_(
        _rgb(*BRAND_TOP), _rgb(*BRAND_BOTTOM)).drawInBezierPath_angle_(body, -90)

    # Soft highlight across the top half, clipped to the squircle.
    NSGraphicsContext.currentContext().saveGraphicsState()
    body.addClip()
    NSGradient.alloc().initWithStartingColor_endingColor_(
        NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.22),
        NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.0)).\
        drawInRect_angle_(NSMakeRect(0, px * 0.52, px, px * 0.48), -90)
    NSGraphicsContext.currentContext().restoreGraphicsState()

    # Below ~32px five bars turn to mush — drop to three.
    fractions = WAVES["idle"] if px > 32 else [0.45, 1.0, 0.45]
    _bars(px / 2, px / 2, px * 0.62, fractions,
          px * (0.085 if px > 32 else 0.14), NSColor.whiteColor())
    return _write(rep, os.path.join(ICONSET, f"tmp-{px}.png"))


def build_icns():
    sizes = [(16, "16x16"), (32, "16x16@2x"), (32, "32x32"),
             (64, "32x32@2x"), (128, "128x128"), (256, "128x128@2x"),
             (256, "256x256"), (512, "256x256@2x"), (512, "512x512"),
             (1024, "512x512@2x")]
    os.makedirs(ICONSET, exist_ok=True)
    for old in os.listdir(ICONSET):
        os.remove(os.path.join(ICONSET, old))
    drawn = {}
    for px, name in sizes:
        if px not in drawn:
            drawn[px] = app_icon(px)
        target = os.path.join(ICONSET, f"icon_{name}.png")
        with open(drawn[px], "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())
    for tmp in [p for p in os.listdir(ICONSET) if p.startswith("tmp-")]:
        os.remove(os.path.join(ICONSET, tmp))

    icns = os.path.join(ASSETS, "AppIcon.icns")
    subprocess.run(["iconutil", "-c", "icns", ICONSET, "-o", icns], check=True)
    return icns


# ── menu bar glyphs ─────────────────────────────────────────────────────────

def menubar_glyph(state, px=40):
    """One template PNG per state, sized for a 20pt status item on retina."""
    rep = _canvas(px)
    black = NSColor.blackColor()
    cx = cy = px / 2
    if state == "processing":
        _dots(cx, cy, px * 0.62, black)
    else:
        _bars(cx, cy, px * 0.66, WAVES[state], px * 0.105, black)
    return _write(rep, os.path.join(MENUBAR, f"{state}.png"))


def main():
    print("Dock icon:", build_icns())
    for state in ("idle", "recording", "processing"):
        print("menu bar:", menubar_glyph(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
