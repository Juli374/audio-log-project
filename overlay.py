"""Floating overlay with native macOS styling — SF Symbols, pulsing dot, blur."""

from AppKit import (
    NSAnimationContext,
    NSApplication,
    NSColor,
    NSEvent,
    NSFont,
    NSImage,
    NSImageSymbolConfiguration,
    NSImageView,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSTextField,
    NSView,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Quartz import CABasicAnimation

from utils import get_logger

log = get_logger(__name__)

_H = 40
_OFFSET_Y = 24
_CORNER = 12
_PAD_L = 14
_PAD_R = 14
_DOT_SIZE = 10
_ICON_SIZE = 16
_GAP = 8
_FADE = 0.15
_MIN_W = 140

# Collection behavior
_CAN_JOIN_ALL_SPACES = 1 << 0
_FULL_SCREEN_AUXILIARY = 1 << 8
_IGNORES_CYCLE = 1 << 6

# Theme colors: (bg, text)
_BG_LIGHT = NSColor.colorWithRed_green_blue_alpha_(0.1, 0.1, 0.12, 0.92)   # dark bg for light mode
_BG_DARK = NSColor.colorWithRed_green_blue_alpha_(0.95, 0.95, 0.97, 0.92)  # light bg for dark mode
_TEXT_LIGHT = NSColor.colorWithRed_green_blue_alpha_(1, 1, 1, 0.9)          # white text for light mode
_TEXT_DARK = NSColor.colorWithRed_green_blue_alpha_(0.08, 0.08, 0.1, 0.9)   # dark text for dark mode

# Text height for vertical centering
_TEXT_H = 18


def _is_dark_mode():
    """Check if macOS is in dark mode."""
    app = NSApplication.sharedApplication()
    appearance = app.effectiveAppearance()
    name = appearance.bestMatchFromAppearancesWithNames_(["NSAppearanceNameAqua", "NSAppearanceNameDarkAqua"])
    return name == "NSAppearanceNameDarkAqua"

# State colors
_COLOR_REC = NSColor.colorWithRed_green_blue_alpha_(1.0, 0.25, 0.25, 1.0)
_COLOR_PROC = NSColor.colorWithRed_green_blue_alpha_(0.35, 0.6, 1.0, 1.0)
_COLOR_DONE = NSColor.colorWithRed_green_blue_alpha_(0.3, 0.85, 0.45, 1.0)
_COLOR_ERR = NSColor.colorWithRed_green_blue_alpha_(1.0, 0.35, 0.35, 1.0)

# SF Symbols
_SYM_MIC = "mic.fill"
_SYM_PROC = "waveform"
_SYM_DONE = "checkmark.circle.fill"
_SYM_ERR = "xmark.circle.fill"

# State config: (symbol_name, color, pulse_dot)
_STATES = {
    "record":  (_SYM_MIC,  _COLOR_REC,  True),
    "process": (_SYM_PROC, _COLOR_PROC, False),
    "done":    (_SYM_DONE, _COLOR_DONE, False),
    "error":   (_SYM_ERR,  _COLOR_ERR,  False),
}


def _sf_image(name, color, size=_ICON_SIZE):
    """Load an SF Symbol with the given color."""
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if img is None:
        return None
    config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
        size, 0.4  # medium weight
    )
    color_config = NSImageSymbolConfiguration.configurationWithHierarchicalColor_(color)
    combined = config.configurationByApplyingConfiguration_(color_config)
    return img.imageWithSymbolConfiguration_(combined)


def _create_panel():
    w = 240
    style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
    p = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, w, _H), style, 2, False,
    )
    p.setLevel_(25)
    p.setOpaque_(False)
    p.setBackgroundColor_(NSColor.clearColor())
    p.setIgnoresMouseEvents_(True)
    p.setHasShadow_(True)
    p.setFloatingPanel_(True)
    p.setWorksWhenModal_(True)
    p.setHidesOnDeactivate_(False)
    p.setCollectionBehavior_(
        _CAN_JOIN_ALL_SPACES | _FULL_SCREEN_AUXILIARY | _IGNORES_CYCLE
    )
    p.setAlphaValue_(0.0)

    # Container with dark rounded background
    container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, _H))
    container.setWantsLayer_(True)
    c_layer = container.layer()
    c_layer.setCornerRadius_(_CORNER)
    c_layer.setMasksToBounds_(True)
    c_layer.setBackgroundColor_(_BG_LIGHT.CGColor())

    # Pulsing dot (recording indicator)
    dot_x = _PAD_L
    dot_y = (_H - _DOT_SIZE) / 2.0
    dot_view = NSView.alloc().initWithFrame_(
        NSMakeRect(dot_x, dot_y, _DOT_SIZE, _DOT_SIZE)
    )
    dot_view.setWantsLayer_(True)
    dot_layer = dot_view.layer()
    dot_layer.setCornerRadius_(_DOT_SIZE / 2)
    dot_layer.setBackgroundColor_(_COLOR_REC.CGColor())
    container.addSubview_(dot_view)

    # SF Symbol icon
    icon_x = dot_x + _DOT_SIZE + _GAP
    icon_y = (_H - _ICON_SIZE) / 2.0
    icon_view = NSImageView.alloc().initWithFrame_(
        NSMakeRect(icon_x, icon_y, _ICON_SIZE, _ICON_SIZE)
    )
    icon_view.setImageScaling_(2)  # proportionallyUpOrDown
    container.addSubview_(icon_view)

    # Text label — vertically centered
    text_x = icon_x + _ICON_SIZE + _GAP
    text_w = w - text_x - _PAD_R
    text_y = (_H - _TEXT_H) / 2.0
    label = NSTextField.alloc().initWithFrame_(
        NSMakeRect(text_x, text_y, text_w, _TEXT_H)
    )
    label.setEditable_(False)
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(1, 1, 1, 0.9))
    label.setFont_(NSFont.systemFontOfSize_weight_(13, 0.3))
    label.setAlignment_(0)  # left
    label.setLineBreakMode_(5)  # truncate tail
    container.addSubview_(label)

    p.setContentView_(container)
    return p, container, dot_view, icon_view, label


class Overlay:
    """Call build() once on main thread, then show/hide directly."""

    def __init__(self) -> None:
        self._panel = None
        self._container = None
        self._dot = None
        self._icon = None
        self._label = None
        self._pulsing = False

    def build(self) -> None:
        self._panel, self._container, \
            self._dot, self._icon, self._label = _create_panel()
        log.info("Overlay panel created")

    def show(self, text: str, state: str = "record") -> None:
        """Show overlay near cursor. state: record/process/done/error."""
        if not self._panel:
            return

        # Apply theme
        dark = _is_dark_mode()
        bg_color = _BG_DARK if dark else _BG_LIGHT
        text_color = _TEXT_DARK if dark else _TEXT_LIGHT
        self._container.layer().setBackgroundColor_(bg_color.CGColor())
        self._label.setTextColor_(text_color)

        sym_name, color, pulse = _STATES.get(state, _STATES["record"])

        # Update dot color
        self._dot.layer().setBackgroundColor_(color.CGColor())
        if pulse and not self._pulsing:
            self._start_pulse()
        elif not pulse and self._pulsing:
            self._stop_pulse()

        # Update SF Symbol icon
        sf_img = _sf_image(sym_name, color)
        if sf_img:
            self._icon.setImage_(sf_img)

        # Update text (strip leading emoji if present)
        clean = text
        if clean and not clean[0].isascii():
            parts = clean.split(None, 1)
            if len(parts) == 2:
                clean = parts[1]
        self._label.setStringValue_(clean)

        # Auto-size width
        text_start = _PAD_L + _DOT_SIZE + _GAP + _ICON_SIZE + _GAP
        text_size = self._label.attributedStringValue().size()
        w = max(_MIN_W, int(text_start + text_size.width + _PAD_R + 8))

        # Resize everything
        text_y = (_H - _TEXT_H) / 2.0
        self._panel.setContentSize_((w, _H))
        self._container.setFrame_(NSMakeRect(0, 0, w, _H))
        self._label.setFrame_(NSMakeRect(text_start, text_y, w - text_start - _PAD_R, _TEXT_H))

        # Position near cursor
        mouse = NSEvent.mouseLocation()
        screen = NSScreen.mainScreen()
        if screen:
            sf = screen.frame()
            x = max(sf.origin.x, min(mouse.x - w / 2, sf.origin.x + sf.size.width - w))
            y = max(sf.origin.y, mouse.y - _H - _OFFSET_Y)
            self._panel.setFrameOrigin_((x, y))

        self._panel.orderFrontRegardless()
        self._fade(1.0)

    def hide(self) -> None:
        if self._panel:
            self._stop_pulse()
            self._fade(0.0)

    def _fade(self, alpha):
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(_FADE)
        self._panel.animator().setAlphaValue_(alpha)
        NSAnimationContext.endGrouping()

    def _start_pulse(self):
        self._pulsing = True
        layer = self._dot.layer()

        opacity = CABasicAnimation.animationWithKeyPath_("opacity")
        opacity.setFromValue_(1.0)
        opacity.setToValue_(0.3)
        opacity.setDuration_(0.8)
        opacity.setAutoreverses_(True)
        opacity.setRepeatCount_(float("inf"))
        layer.addAnimation_forKey_(opacity, "pulse_opacity")

        scale = CABasicAnimation.animationWithKeyPath_("transform.scale")
        scale.setFromValue_(1.0)
        scale.setToValue_(1.3)
        scale.setDuration_(0.8)
        scale.setAutoreverses_(True)
        scale.setRepeatCount_(float("inf"))
        layer.addAnimation_forKey_(scale, "pulse_scale")

    def _stop_pulse(self):
        if self._pulsing:
            self._dot.layer().removeAllAnimations()
            self._pulsing = False
