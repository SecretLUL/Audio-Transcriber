"""Design tokens and ttk theme.

Tkinter looks dated only because its defaults come from the nineties. With a
custom clam-based theme, consistent colour and spacing values and a handful of
hand-drawn widgets (see widgets.py) it is perfectly possible to build a modern
interface without any additional library.

Every colour and spacing value lives here - no colour literals are allowed
anywhere else in the UI code.
"""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

# ----------------------------------------------------------------------
# Colours
# ----------------------------------------------------------------------
BG = "#16181d"            # window background
BG_DEEP = "#101216"       # header, input fields
CARD = "#1e2128"          # card surface
CARD_HI = "#252932"       # card under the pointer
FIELD = "#171a20"         # input field
BORDER = "#2b3039"        # card border
BORDER_HI = "#3c4453"     # border on focus/hover

TEXT = "#e8eaed"          # body text
TEXT_DIM = "#98a0ad"      # labels
TEXT_MUTE = "#69727f"     # hints, footnotes

ACCENT = "#4f8cff"        # primary colour
ACCENT_HI = "#6ba0ff"
ACCENT_LO = "#3570dd"
REC = "#ff4757"           # recording
REC_HI = "#ff6b78"
OK = "#22d67b"            # success
WARN = "#ffd23f"          # warning
DANGER = "#ff4757"

# Level meter
METER_LOW = "#22d67b"
METER_MID = "#ffd23f"
METER_HIGH = "#ff4757"
METER_OFF_LOW = "#16362a"
METER_OFF_MID = "#3a3418"
METER_OFF_HIGH = "#3d1a20"
METER_BG = "#0e1014"

# Speaker colours in the transcript
SPEAKER_SELF = "#6ba0ff"
SPEAKER_OTHER = "#22d67b"
TIMESTAMP = "#69727f"

# ----------------------------------------------------------------------
# Spacing (4 point grid)
# ----------------------------------------------------------------------
XS, SM, MD, LG, XL, XXL = 4, 8, 12, 16, 24, 32

RADIUS_CARD = 12
RADIUS_CTRL = 8
RADIUS_PILL = 999

_FAMILY = "Segoe UI"
_MONO = "Consolas"

fonts = {}


def _pick_family(root, *candidates):
    available = set(tkfont.families(root))
    for name in candidates:
        if name in available:
            return name
    return candidates[-1]


def apply(root):
    """Set up fonts, ttk styles and global options."""
    family = _pick_family(root, "Segoe UI Variable Text", _FAMILY, "Helvetica")
    display = _pick_family(root, "Segoe UI Variable Display", _FAMILY, "Helvetica")
    mono = _pick_family(root, "Cascadia Mono", _MONO, "Courier")

    fonts.update({
        "display": tkfont.Font(root=root, family=display, size=17, weight="bold"),
        "title": tkfont.Font(root=root, family=family, size=11, weight="bold"),
        "section": tkfont.Font(root=root, family=family, size=8, weight="bold"),
        "body": tkfont.Font(root=root, family=family, size=10),
        "body_bold": tkfont.Font(root=root, family=family, size=10, weight="bold"),
        "small": tkfont.Font(root=root, family=family, size=9),
        "tiny": tkfont.Font(root=root, family=family, size=8),
        "mono": tkfont.Font(root=root, family=mono, size=10),
        "mono_small": tkfont.Font(root=root, family=mono, size=9),
        "button": tkfont.Font(root=root, family=family, size=10, weight="bold"),
    })

    root.configure(bg=BG)

    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(".", background=BG, foreground=TEXT, borderwidth=0,
                    focuscolor=ACCENT, font=fonts["body"])
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=TEXT, font=fonts["body"])
    style.configure("Card.TLabel", background=CARD, foreground=TEXT)
    style.configure("Dim.TLabel", background=CARD, foreground=TEXT_DIM,
                    font=fonts["small"])
    style.configure("Mute.TLabel", background=CARD, foreground=TEXT_MUTE,
                    font=fonts["tiny"])

    # --- Combo box -----------------------------------------------------
    # Important: clam paints the arrow area with 'bordercolor', not with
    # 'background'. Leave the border tone there and you get a bright block on
    # the right edge. Hence bordercolor = field colour, with the visible frame
    # coming from lightcolor/darkcolor.
    for name in ("TCombobox", "Dark.TCombobox"):
        style.configure(name,
                        fieldbackground=FIELD, background=FIELD, foreground=TEXT,
                        arrowcolor=TEXT_DIM, bordercolor=FIELD,
                        lightcolor=BORDER, darkcolor=BORDER,
                        selectbackground=FIELD, selectforeground=TEXT,
                        insertcolor=TEXT, padding=(10, 7), arrowsize=13)
        style.map(name,
                  background=[("readonly", FIELD), ("active", FIELD),
                              ("pressed", FIELD), ("disabled", BG_DEEP)],
                  fieldbackground=[("readonly", FIELD), ("disabled", BG_DEEP)],
                  bordercolor=[("disabled", BG_DEEP)],
                  foreground=[("disabled", TEXT_MUTE)],
                  lightcolor=[("focus", ACCENT), ("hover", BORDER_HI)],
                  darkcolor=[("focus", ACCENT), ("hover", BORDER_HI)],
                  arrowcolor=[("disabled", TEXT_MUTE), ("hover", TEXT)])

    # Drop-down list (a classic Tk listbox, only reachable via the option DB)
    root.option_add("*TCombobox*Listbox.background", CARD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.borderWidth", 0)
    root.option_add("*TCombobox*Listbox.highlightThickness", 0)
    root.option_add("*TCombobox*Listbox.font", fonts["body"])

    # --- Entry ---------------------------------------------------------
    style.configure("Dark.TEntry",
                    fieldbackground=FIELD, foreground=TEXT, insertcolor=ACCENT,
                    bordercolor=FIELD, lightcolor=BORDER, darkcolor=BORDER,
                    padding=(10, 7), selectbackground=ACCENT_LO,
                    selectforeground="#ffffff")
    style.map("Dark.TEntry",
              fieldbackground=[("disabled", BG_DEEP)],
              foreground=[("disabled", TEXT_MUTE)],
              lightcolor=[("focus", ACCENT)],
              darkcolor=[("focus", ACCENT)])

    # --- Scrollbar -----------------------------------------------------
    style.configure("Dark.Vertical.TScrollbar",
                    background=BORDER, troughcolor=CARD, bordercolor=CARD,
                    arrowcolor=CARD, darkcolor=BORDER, lightcolor=BORDER,
                    arrowsize=0, width=10)
    style.map("Dark.Vertical.TScrollbar",
              background=[("active", BORDER_HI), ("pressed", ACCENT)])

    style.layout("Dark.Vertical.TScrollbar", [
        ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": 1, "sticky": "nswe"})]})])

    return style


# ----------------------------------------------------------------------
def round_rect(canvas, x0, y0, x1, y1, radius, **kwargs):
    """A rounded rectangle drawn as a smoothed polygon.

    Tk has no rounded rectangles; a polygon with smooth=True and duplicated
    corner points produces the result without any library.
    """
    radius = max(0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
    points = [
        x0 + radius, y0, x1 - radius, y0, x1, y0,
        x1, y0 + radius, x1, y1 - radius, x1, y1,
        x1 - radius, y1, x0 + radius, y1, x0, y1,
        x0, y1 - radius, x0, y0 + radius, x0, y0,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def mix(color_a, color_b, t):
    """Linear blend of two #rrggbb colours (t = 0 -> a, 1 -> b)."""
    a = tuple(int(color_a[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(color_b[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(
        int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))
