"""SVG and high-resolution vector icon renderer for the Tkinter interface.

Renders crisp, multi-color anti-aliased vector icons at any target DPI/size
without requiring third-party SVG C-libraries. Uses Pillow for 4x supersampled
anti-aliasing and converts to native tk.PhotoImage objects.
"""

import io
import math
import tkinter as tk
from PIL import Image, ImageDraw

_ICON_CACHE = {}


def get_icon(name: str, size: int = 24, fg: str = None) -> tk.PhotoImage:
    """Get a tk.PhotoImage for the given icon name and size (cached)."""
    cache_key = (name, size, fg)
    if cache_key in _ICON_CACHE:
        return _ICON_CACHE[cache_key]

    img = render_icon_image(name, size, fg=fg)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    photo = tk.PhotoImage(data=buf.getvalue())
    _ICON_CACHE[cache_key] = photo
    return photo


def render_icon_image(name: str, size: int = 24, fg: str = None) -> Image.Image:
    """Render a vector icon into a high-DPI RGBA PIL Image."""
    scale = 4
    canvas_size = size * scale
    img = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    renderer = _RENDERERS.get(name, _draw_fallback)
    renderer(draw, canvas_size, fg=fg)

    # Downsample with Lanczos anti-aliasing for smooth crisp vector edges
    return img.resize((size, size), Image.Resampling.LANCZOS)


# ======================================================================
# Icon Renderers (Canvas coordinate range: 0 .. S)
# ======================================================================
def _draw_microphone(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Vibrant Microphone Icon with Cyan/Blue gradient capsule & stand."""
    cx = S * 0.5
    
    # Outer subtle rounded badge background
    pad = S * 0.05
    draw.rounded_rectangle(
        [pad, pad, S - pad, S - pad],
        radius=S * 0.25,
        fill="#0f172a",
        outline="#1e293b",
        width=int(S * 0.04)
    )

    # Mic Capsule Body
    mic_w = S * 0.26
    mic_h = S * 0.42
    mic_top = S * 0.18
    draw.rounded_rectangle(
        [cx - mic_w / 2, mic_top, cx + mic_w / 2, mic_top + mic_h],
        radius=mic_w / 2,
        fill=fg or "#38bdf8",
        outline="#0284c7",
        width=int(S * 0.03)
    )

    # Grille accent lines
    draw.line([cx - mic_w * 0.35, mic_top + mic_h * 0.35, cx + mic_w * 0.35, mic_top + mic_h * 0.35],
              fill="#0284c7", width=int(S * 0.03))
    draw.line([cx - mic_w * 0.35, mic_top + mic_h * 0.65, cx + mic_w * 0.35, mic_top + mic_h * 0.65],
              fill="#0284c7", width=int(S * 0.03))

    # Arc bracket around capsule
    arc_w = S * 0.48
    arc_top = S * 0.28
    arc_bottom = S * 0.68
    draw.arc(
        [cx - arc_w / 2, arc_top, cx + arc_w / 2, arc_bottom],
        start=0,
        end=180,
        fill="#38bdf8",
        width=int(S * 0.06)
    )

    # Stem and Base Stand
    stem_top = arc_bottom - (arc_bottom - arc_top) / 2
    stem_bottom = S * 0.82
    draw.line([cx, stem_top, cx, stem_bottom], fill="#38bdf8", width=int(S * 0.06))

    base_w = S * 0.44
    draw.line([cx - base_w / 2, stem_bottom, cx + base_w / 2, stem_bottom],
              fill="#38bdf8", width=int(S * 0.07))


def _draw_speaker(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Vibrant Speaker / Playback Icon with sound waves."""
    # Outer subtle rounded badge background
    pad = S * 0.05
    draw.rounded_rectangle(
        [pad, pad, S - pad, S - pad],
        radius=S * 0.25,
        fill="#0f172a",
        outline="#1e293b",
        width=int(S * 0.04)
    )

    # Speaker Body (Cone)
    spk_color = fg or "#a855f7"
    cx_left = S * 0.22
    cy = S * 0.5

    # Speaker box
    draw.rounded_rectangle(
        [cx_left, cy - S * 0.14, cx_left + S * 0.14, cy + S * 0.14],
        radius=S * 0.04,
        fill=spk_color
    )

    # Speaker flare polygon
    flare_points = [
        (cx_left + S * 0.12, cy - S * 0.14),
        (cx_left + S * 0.32, cy - S * 0.30),
        (cx_left + S * 0.32, cy + S * 0.30),
        (cx_left + S * 0.12, cy + S * 0.14)
    ]
    draw.polygon(flare_points, fill=spk_color)

    # Sound Waves
    wave_color = "#c084fc"
    # Wave 1 (inner)
    draw.arc(
        [S * 0.42, cy - S * 0.20, S * 0.62, cy + S * 0.20],
        start=-60, end=60, fill=wave_color, width=int(S * 0.06)
    )
    # Wave 2 (outer)
    draw.arc(
        [S * 0.52, cy - S * 0.34, S * 0.82, cy + S * 0.34],
        start=-60, end=60, fill="#e879f9", width=int(S * 0.06)
    )


def _draw_sparkle(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Vibrant Gold/Amber AI Sparkle Icon for ElevenLabs Scribe."""
    def draw_star(cx, cy, r_out, r_in, fill_col):
        pts = []
        for i in range(8):
            angle = i * (math.pi / 4) - math.pi / 2
            r = r_out if i % 2 == 0 else r_in
            pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
        draw.polygon(pts, fill=fill_col)

    # Main Center Star
    draw_star(S * 0.5, S * 0.48, S * 0.38, S * 0.12, fg or "#fbbf24")
    # Small Top Right Star
    draw_star(S * 0.78, S * 0.24, S * 0.18, S * 0.06, "#fef08a")
    # Small Bottom Left Star
    draw_star(S * 0.24, S * 0.76, S * 0.14, S * 0.05, "#fde047")


def _draw_record(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Vibrant Recording Pulse Circle Icon."""
    cx, cy = S * 0.5, S * 0.5
    r_outer = S * 0.42
    r_inner = S * 0.26

    # Outer subtle halo
    draw.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer],
                 fill="#ff475733", outline="#ff475766", width=int(S * 0.04))
    # Core Red Record Dot
    draw.ellipse([cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner],
                 fill=fg or "#ff4757", outline="#ffffff", width=int(S * 0.04))


def _draw_stop(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Stop Recording Square Icon."""
    pad = S * 0.24
    draw.rounded_rectangle(
        [pad, pad, S - pad, S - pad],
        radius=S * 0.08,
        fill=fg or "#e11d48",
        outline="#ffffff",
        width=int(S * 0.04)
    )


def _draw_globe(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Globe / Language Icon."""
    cx, cy = S * 0.5, S * 0.5
    r = S * 0.38
    color = fg or "#38bdf8"
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=int(S * 0.06))
    draw.line([cx - r, cy, cx + r, cy], fill=color, width=int(S * 0.05))
    draw.ellipse([cx - r * 0.5, cy - r, cx + r * 0.5, cy + r], outline=color, width=int(S * 0.05))


def _draw_brain(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """AI Model / Brain / Neural CPU Icon."""
    cx, cy = S * 0.5, S * 0.5
    color = fg or "#818cf8"
    
    # Outer chip / frame
    r = S * 0.34
    draw.rounded_rectangle([cx - r, cy - r, cx + r, cy + r], radius=S * 0.12,
                           fill="#1e1b4b", outline=color, width=int(S * 0.06))
    
    # Inner neural nodes
    draw.ellipse([cx - S * 0.18, cy - S * 0.18, cx - S * 0.04, cy - S * 0.04], fill="#a5b4fc")
    draw.ellipse([cx + S * 0.04, cy - S * 0.18, cx + S * 0.18, cy - S * 0.04], fill="#a5b4fc")
    draw.ellipse([cx - S * 0.07, cy + S * 0.04, cx + S * 0.07, cy + S * 0.18], fill="#c7d2fe")

    # Connectors
    draw.line([cx - S * 0.11, cy - S * 0.11, cx, cy + S * 0.11], fill="#818cf8", width=int(S * 0.04))
    draw.line([cx + S * 0.11, cy - S * 0.11, cx, cy + S * 0.11], fill="#818cf8", width=int(S * 0.04))


def _draw_lock(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Security Lock Icon for API Key."""
    cx = S * 0.5
    color = fg or "#fbbf24"

    # Shackle (top arc)
    shackle_w = S * 0.32
    top = S * 0.18
    draw.arc([cx - shackle_w / 2, top, cx + shackle_w / 2, top + S * 0.38],
             start=180, end=360, fill=color, width=int(S * 0.07))

    # Lock Body
    body_top = S * 0.44
    body_w = S * 0.48
    body_h = S * 0.38
    draw.rounded_rectangle(
        [cx - body_w / 2, body_top, cx + body_w / 2, body_top + body_h],
        radius=S * 0.08,
        fill=color,
        outline="#b45309",
        width=int(S * 0.03)
    )

    # Keyhole
    draw.ellipse([cx - S * 0.06, body_top + S * 0.10, cx + S * 0.06, body_top + S * 0.22],
                 fill="#78350f")


def _draw_eye(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Eye Icon for Show Password."""
    cx, cy = S * 0.5, S * 0.5
    color = fg or "#94a3b8"

    # Eye outline
    draw.arc([cx - S * 0.40, cy - S * 0.28, cx + S * 0.40, cy + S * 0.28],
             start=0, end=360, fill=color, width=int(S * 0.06))
    # Pupil
    draw.ellipse([cx - S * 0.14, cy - S * 0.14, cx + S * 0.14, cy + S * 0.14],
                 fill=color)


def _draw_eye_off(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Eye Off Icon for Hide Password."""
    _draw_eye(draw, S, fg)
    color = fg or "#f43f5e"
    draw.line([S * 0.15, S * 0.85, S * 0.85, S * 0.15], fill=color, width=int(S * 0.08))


def _draw_app_logo(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Audio AI Recorder App Brand Emblem."""
    # Outer Rounded Hexagon / Card Badge
    pad = S * 0.04
    draw.rounded_rectangle(
        [pad, pad, S - pad, S - pad],
        radius=S * 0.28,
        fill="#1e1b4b",
        outline="#4f46e5",
        width=int(S * 0.05)
    )

    # Sound Wave bars background
    bars = [0.3, 0.5, 0.85, 0.6, 0.9, 0.45, 0.3]
    cx = S * 0.5
    spacing = S * 0.09
    start_x = cx - (len(bars) - 1) * spacing / 2

    for i, h in enumerate(bars):
        x = start_x + i * spacing
        bar_h = S * 0.5 * h
        color = "#818cf8" if i % 2 == 0 else "#38bdf8"
        draw.line([x, S * 0.5 - bar_h / 2, x, S * 0.5 + bar_h / 2],
                  fill=color, width=int(S * 0.05))


def _draw_transcript(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Transcript Document & Text Icon."""
    pad_x = S * 0.20
    pad_y = S * 0.15
    color = fg or "#a78bfa"

    # Document Page
    draw.rounded_rectangle(
        [pad_x, pad_y, S - pad_x, S - pad_y],
        radius=S * 0.08,
        fill="#2e1065",
        outline=color,
        width=int(S * 0.05)
    )

    # Text Lines
    line_x0 = pad_x + S * 0.10
    line_x1 = S - pad_x - S * 0.10
    for y_rel in [0.32, 0.48, 0.64]:
        draw.line([line_x0, S * y_rel, line_x1, S * y_rel],
                  fill="#c4b5fd", width=int(S * 0.05))


def _draw_copy(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Clipboard / Copy Icon."""
    color = fg or "#94a3b8"
    # Back sheet
    draw.rounded_rectangle([S * 0.30, S * 0.30, S * 0.80, S * 0.85],
                           radius=S * 0.06, fill=None, outline=color, width=int(S * 0.05))
    # Front sheet
    draw.rounded_rectangle([S * 0.18, S * 0.15, S * 0.68, S * 0.70],
                           radius=S * 0.06, fill="#1e293b", outline="#38bdf8", width=int(S * 0.05))


def _draw_trash(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Trash / Clear Icon."""
    color = fg or "#f43f5e"
    # Bin body
    draw.rounded_rectangle([S * 0.25, S * 0.35, S * 0.75, S * 0.85],
                           radius=S * 0.06, fill="#4c0519", outline=color, width=int(S * 0.05))
    # Lid
    draw.line([S * 0.18, S * 0.30, S * 0.82, S * 0.30], fill=color, width=int(S * 0.06))
    draw.line([S * 0.38, S * 0.22, S * 0.62, S * 0.22], fill=color, width=int(S * 0.06))


def _draw_warning(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Warning Triangle Icon."""
    color = fg or "#f59e0b"
    cx = S * 0.5
    top = (cx, S * 0.15)
    bottom_left = (S * 0.12, S * 0.85)
    bottom_right = (S * 0.88, S * 0.85)

    draw.polygon([top, bottom_right, bottom_left], fill="#451a03", outline=color, width=int(S * 0.05))
    # Exclamation mark
    draw.line([cx, S * 0.38, cx, S * 0.60], fill=color, width=int(S * 0.07))
    draw.ellipse([cx - S * 0.04, S * 0.70, cx + S * 0.04, S * 0.78], fill=color)


def _draw_check(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Success Checkmark Icon."""
    color = fg or "#10b981"
    draw.ellipse([S * 0.12, S * 0.12, S * 0.88, S * 0.88], fill="#064e3b", outline=color, width=int(S * 0.05))
    pts = [(S * 0.28, S * 0.50), (S * 0.44, S * 0.66), (S * 0.72, S * 0.34)]
    draw.line(pts, fill=color, width=int(S * 0.07))


def _draw_fallback(draw: ImageDraw.ImageDraw, S: float, fg: str = None):
    """Fallback circle renderer."""
    draw.ellipse([S * 0.2, S * 0.2, S * 0.8, S * 0.8], fill=fg or "#64748b")


_RENDERERS = {
    "microphone": _draw_microphone,
    "speaker": _draw_speaker,
    "sparkle": _draw_sparkle,
    "record": _draw_record,
    "stop": _draw_stop,
    "globe": _draw_globe,
    "brain": _draw_brain,
    "lock": _draw_lock,
    "eye": _draw_eye,
    "eye_off": _draw_eye_off,
    "app_logo": _draw_app_logo,
    "transcript": _draw_transcript,
    "copy": _draw_copy,
    "trash": _draw_trash,
    "warning": _draw_warning,
    "check": _draw_check,
}
