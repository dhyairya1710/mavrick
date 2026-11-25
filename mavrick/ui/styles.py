"""Open Design light linen and monochrome editorial dark tokens for GTK CSS, Tk, and Pango."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gi.repository import Gdk

from ..config import THEME_NAMES, load_theme_setting

LIGHT_COLORS = {
    "linen": "#efe7d2",
    "surface": "#ffffff",
    "inset": "#f5f5f5",
    "border": "#d9d9d9",
    "ink": "#202020",
    "muted": "#595959",
    "accent": "#63fe13",
    "accent_ink": "#0b0605",
    "accent_hover": "#7bff2b",
    "accent_active": "#4edd04",
    "success": "#218c00",
    "success_ink": "#1c7600",
    "warning": "#ff8d00",
    "warning_ink": "#7a4000",
    "danger": "#b42318",
    "danger_active": "#8f1b13",
    "danger_btn": "#b42318",
    "danger_btn_active": "#8f1b13",
    "danger_ink": "#ffffff",
}

DARK_COLORS = {
    "linen": "#141517",
    "surface": "#1c1e22",
    "inset": "#24272d",
    "border": "#32363f",
    "ink": "#f1f2f4",
    "muted": "#a1a7b3",
    "accent": "#eceef2",
    "accent_ink": "#141517",
    "accent_hover": "#ffffff",
    "accent_active": "#d4d7de",
    "success": "#7fb892",
    "success_ink": "#8cc79f",
    "warning": "#d9913b",
    "warning_ink": "#ebb266",
    "danger": "#e26d65",
    "danger_active": "#c9534b",
    "danger_btn": "#b9382e",
    "danger_btn_active": "#982c24",
    "danger_ink": "#ffffff",
}

COLORS = LIGHT_COLORS
THEMES = {"light": LIGHT_COLORS, "dark": DARK_COLORS}
FONT_SANS = "Albert Sans"
FONT_MONO = "JetBrains Mono, monospace"

_CSS_BODY = """
.mavrick-hud { background-color: transparent; }
#main-container {
    background-color: @mv_linen;
    color: @mv_ink;
    border: 1px solid @mv_border;
    border-radius: 18px;
    font-family: 'Albert Sans', sans-serif;
    font-size: 13px;
}
#main-container label { color: inherit; }
#header-bar { padding: 8px 16px; }
#brand-title { font-size: 12px; font-weight: 800; letter-spacing: 1.6px; }
#status-capsule {
    background-color: @mv_surface;
    border: 1px solid @mv_border;
    border-radius: 99px;
    padding: 3px 8px;
}
#status-label, #engine-tag, #hint-label { font-size: 11px; font-weight: 600; }
#main-container #hint-label { color: @mv_muted; }
#entry-box {
    margin: 0 16px 8px;
    padding: 0 8px;
    background-color: @mv_surface;
    border: 1px solid @mv_border;
    border-radius: 12px;
}
#main-container #prompt-glyph { color: @mv_success_ink; font-weight: 700; }
#main-container entry {
    background-image: none;
    background-color: @mv_surface;
    color: @mv_ink;
    border: 1px solid @mv_border;
    border-radius: 8px;
    box-shadow: none;
    caret-color: @mv_ink;
}
#main-container #prompt-entry {
    background-color: transparent;
    border-color: transparent;
    padding: 8px;
    font-size: 16px;
}
#main-container entry:focus, #main-container #prompt-entry:focus {
    box-shadow: inset 0 -2px @mv_success;
}
#main-container button {
    background-image: none;
    background-color: @mv_surface;
    color: @mv_ink;
    border: 1px solid @mv_border;
    border-radius: 8px;
    box-shadow: none;
    text-shadow: none;
    padding: 4px 8px;
    font-size: 12px;
    transition: none;
}
#main-container button:hover { background-color: @mv_inset; }
#main-container button:active { background-color: @mv_border; }
#main-container button:focus, #main-container row:focus {
    outline: 2px solid @mv_success;
    outline-offset: -3px;
}
#main-container button.icon-btn { background-color: transparent; border-color: transparent; }
#main-container button.icon-btn:hover { background-color: @mv_surface; }
#main-container button.chip-btn { border-radius: 99px; }
#main-container button.btn-primary, #main-container button.btn-secondary,
#main-container button.btn-destructive { min-height: 32px; padding: 4px 12px; font-weight: 700; }
#main-container button.btn-primary { background-color: @mv_accent; color: @mv_accent_ink; }
#main-container button.btn-primary:hover { background-color: @mv_accent_hover; }
#main-container button.btn-primary:active { background-color: @mv_accent_active; }
#main-container button.btn-danger { color: @mv_danger; }
#main-container button.btn-destructive {
    background-color: @mv_danger_btn;
    color: @mv_danger_ink;
    border-color: @mv_danger_btn;
}
#main-container button.btn-destructive:hover, #main-container button.btn-destructive:active {
    background-color: @mv_danger_btn_active;
}
#main-container button.btn-destructive:focus { outline-color: @mv_danger_ink; }
#main-container button:disabled {
    background-color: @mv_inset;
    color: @mv_muted;
    border-color: @mv_border;
}
#chips-scroll { margin: 0 0 8px 16px; }
#action-card, #progress-box, #output-scroll, #process-scroll, #command-list, #file-list {
    background-color: @mv_surface;
    border: 1px solid @mv_border;
    border-radius: 12px;
}
#action-card, #progress-box, #output-scroll, #process-scroll {
    margin: 0 16px 8px;
    padding: 8px 12px;
}
#action-card.review-destructive { border-color: @mv_danger; }
#action-card.review-privileged { border-color: @mv_warning; }
#card-title { font-size: 16px; font-weight: 700; }
#main-container #card-impact, #main-container #card-targets { color: @mv_muted; }
#card-targets, .file-path, #details-view {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
}
#main-container .risk-badge {
    background-color: @mv_inset;
    border: 1px solid @mv_border;
    border-radius: 99px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 700;
}
#main-container .badge-read_only { color: @mv_success_ink; }
#main-container .badge-privileged { color: @mv_warning_ink; border-color: @mv_warning; }
#main-container .badge-destructive { color: @mv_danger; border-color: @mv_danger; }
#file-list row + row { border-top: 1px solid @mv_border; }
#main-container .file-problem { color: @mv_danger; }
#main-container #file-list check:checked { background-color: @mv_accent; color: @mv_accent_ink; }
#command-list row:hover { background-color: @mv_inset; }
#command-list row:selected { background-color: @mv_accent; color: @mv_accent_ink; }
#history-bar { margin: 0 16px 8px; }
.history-title { font-size: 12px; font-weight: 700; }
#output-view, #output-view text, #process-scroll treeview {
    background-color: @mv_surface;
    color: @mv_ink;
}
#details-view, #details-view text { background-color: @mv_inset; color: @mv_ink; }
#progress-label { font-weight: 600; }
#footer-bar { border-top: 1px solid @mv_border; padding: 8px 16px; }
#main-container .status-ready, #main-container .status-done,
#main-container .engine-connected { color: @mv_success_ink; }
#main-container .status-review, #main-container .engine-fallback { color: @mv_warning_ink; }
#main-container .status-cancelled { color: @mv_muted; }
#main-container .status-error, #main-container .engine-offline { color: @mv_danger; }
#main-container #instant-preview { color: @mv_success_ink; font-size: 12px; padding-bottom: 6px; }
#main-container #engine-tag {
    padding: 2px 8px;
    border: 1px solid @mv_border;
    border-radius: 99px;
    background-color: @mv_surface;
}
#main-container #engine-tag.engine-instant { color: @mv_success_ink; }
"""


def theme_colors(theme: str = "light") -> dict[str, str]:
    chosen = theme.strip().lower() if isinstance(theme, str) else "light"
    return THEMES.get(chosen, LIGHT_COLORS)


def build_css(theme: str = "light") -> bytes:
    palette = theme_colors(theme)
    definitions = "\n".join(f"@define-color mv_{name} {color};" for name, color in palette.items())
    return (definitions + _CSS_BODY).encode()


LIGHT_CSS = build_css("light")
DARK_CSS = build_css("dark")
MAVRICK_CSS = LIGHT_CSS


def apply_styles(screen: Gdk.Screen, theme: str | None = None) -> str:
    from gi.repository import Gtk

    if theme is None:
        try:
            chosen = load_theme_setting()
        except ValueError:
            chosen = "light"
    else:
        chosen = theme.strip().lower() if isinstance(theme, str) and theme.strip().lower() in THEME_NAMES else "light"
    provider = getattr(screen, "_mavrick_css_provider", None)
    if provider is None:
        provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        screen._mavrick_css_provider = provider
    if getattr(screen, "_mavrick_theme", None) != chosen:
        provider.load_from_data(build_css(chosen))
        screen._mavrick_theme = chosen
    return chosen
