"""Thin translucent wave indicator across the top of the screen.

Replaces the old floating text overlay. The recording state is conveyed as
a ~4px wave line right under the menu bar:

- recording → animated sine wave (the only moving state)
- process   → flat line, same color family (blue)
- done/error → flat line, colored, auto-hides
- idle      → hidden

All states at 30% opacity, click-through, on all Spaces. No text, no icon,
no popup — stays out of the user's way. Live status (timers, durations)
belongs in the menu bar title, not here.
"""

import math
import threading

import objc
from AppKit import (
    NSBezierPath,
    NSColor,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSView,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from PyObjCTools import AppHelper

from utils import get_logger

log = get_logger(__name__)

# Geometry
_H = 4                    # indicator strip height, px
_OVERALL_ALPHA = 0.55     # ~55% opacity — noticeable but unobtrusive

# Animation
_FPS = 30
_WAVE_SPEED = 6.0         # phase advance per second (rad/s-ish)
_WAVE_AMPL = 1.3          # peak px from centerline (line is _H tall)
_WAVE_PERIOD = 70.0       # px per full cycle

# Window level — above most but below menu bar
_WINDOW_LEVEL = 25

# Collection behavior: visible on all Spaces, in full-screen, not in ⌘-tab cycle
_CAN_JOIN_ALL_SPACES = 1 << 0
_FULL_SCREEN_AUXILIARY = 1 << 8
_IGNORES_CYCLE = 1 << 6

# State colors
_COLOR_REC = NSColor.colorWithRed_green_blue_alpha_(1.0, 0.25, 0.25, 1.0)
_COLOR_PROC = NSColor.colorWithRed_green_blue_alpha_(0.35, 0.6, 1.0, 1.0)
_COLOR_DONE = NSColor.colorWithRed_green_blue_alpha_(0.3, 0.85, 0.45, 1.0)
_COLOR_ERR = NSColor.colorWithRed_green_blue_alpha_(1.0, 0.35, 0.35, 1.0)

_STATE_COLORS = {
    "record": _COLOR_REC,
    "process": _COLOR_PROC,
    "done": _COLOR_DONE,
    "error": _COLOR_ERR,
}


class _WaveView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(_WaveView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._phase = 0.0
        self._color = _COLOR_REC
        self._animate = False
        return self

    def setState_animate_(self, color, animate):
        self._color = color
        self._animate = bool(animate)
        self.setNeedsDisplay_(True)

    def advancePhase(self):
        # Called from the animation timer thread via callAfter
        if not self._animate:
            return
        self._phase += _WAVE_SPEED / _FPS
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        NSColor.clearColor().set()
        NSBezierPath.fillRect_(rect)

        w = float(self.frame().size.width)
        mid = _H / 2.0

        self._color.set()
        path = NSBezierPath.bezierPath()
        path.setLineWidth_(1.5)

        if self._animate and _WAVE_PERIOD > 0:
            path.moveToPoint_(
                (0.0, mid + _WAVE_AMPL * math.sin(self._phase)))
            x = 1.0
            while x <= w:
                y = mid + _WAVE_AMPL * math.sin(
                    self._phase + x * 2.0 * math.pi / _WAVE_PERIOD)
                path.lineToPoint_((x, y))
                x += 1.0
        else:
            path.moveToPoint_((0.0, mid))
            path.lineToPoint_((w, mid))

        path.stroke()


class Overlay:
    """Thin top-of-screen wave indicator.

    Keeps the legacy `show(text, state)` / `hide()` API so callers don't
    have to change — but `text` is ignored (state alone drives the visual).
    """

    def __init__(self) -> None:
        self._panel = None
        self._view = None
        self._timer_stop = threading.Event()
        self._timer_thread: threading.Thread | None = None

    def build(self) -> None:
        screen = NSScreen.mainScreen()
        if not screen:
            log.warning("No main screen — overlay disabled")
            return

        vf = screen.visibleFrame()  # excludes menu bar and dock
        # Place the strip at the very top of the visible area (= just below
        # the menu bar). Height extends downward into app space.
        y = vf.origin.y + vf.size.height - _H
        panel_rect = NSMakeRect(vf.origin.x, y, vf.size.width, _H)

        style = (NSWindowStyleMaskBorderless
                 | NSWindowStyleMaskNonactivatingPanel)
        self._panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            panel_rect, style, 2, False,
        )
        self._panel.setLevel_(_WINDOW_LEVEL)
        self._panel.setOpaque_(False)
        self._panel.setBackgroundColor_(NSColor.clearColor())
        self._panel.setIgnoresMouseEvents_(True)
        self._panel.setHasShadow_(False)
        self._panel.setFloatingPanel_(True)
        self._panel.setHidesOnDeactivate_(False)
        self._panel.setAlphaValue_(0.0)  # invisible until show()
        self._panel.setCollectionBehavior_(
            _CAN_JOIN_ALL_SPACES | _FULL_SCREEN_AUXILIARY | _IGNORES_CYCLE)

        self._view = _WaveView.alloc().initWithFrame_(
            NSMakeRect(0, 0, vf.size.width, _H))
        self._panel.setContentView_(self._view)
        log.info("Wave overlay created (%d×%d at y=%d)",
                 int(vf.size.width), _H, int(y))

    def show(self, text: str | None = None, state: str = "record") -> None:
        """Show the wave line in the given state. `text` is ignored."""
        if self._panel is None:
            return
        color = _STATE_COLORS.get(state, _COLOR_REC)
        animate = (state == "record")
        if self._view is not None:
            self._view.setState_animate_(color, animate)
        self._panel.setAlphaValue_(_OVERALL_ALPHA)
        self._panel.orderFrontRegardless()
        if animate:
            self._start_timer()
        else:
            self._stop_timer()

    def hide(self) -> None:
        if self._panel is None:
            return
        self._stop_timer()
        self._panel.setAlphaValue_(0.0)

    # ── internal: animation driver ──

    def _start_timer(self) -> None:
        if self._timer_thread is not None and self._timer_thread.is_alive():
            return
        self._timer_stop.clear()

        def loop():
            period = 1.0 / _FPS
            while not self._timer_stop.wait(period):
                view = self._view
                if view is None:
                    return
                AppHelper.callAfter(view.advancePhase)

        self._timer_thread = threading.Thread(
            target=loop, daemon=True, name="wave-overlay")
        self._timer_thread.start()

    def _stop_timer(self) -> None:
        self._timer_stop.set()
        self._timer_thread = None
