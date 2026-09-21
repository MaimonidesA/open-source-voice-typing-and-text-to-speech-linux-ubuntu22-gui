"""Theme and small widgets for Voice Studio.

Plain Tk on Xwayland on purpose: the window can be a "utility" window that
never takes keyboard focus when it appears, which dictation delivery relies
on, and the recording badge can be an override-redirect window that floats
above everything without ever being focused.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any, Callable

BG = "#0e1016"
SURFACE = "#161922"
SURFACE_2 = "#1f2330"
SURFACE_3 = "#2a2f3d"
BORDER = "#252a36"
TEXT = "#e8eaf0"
MUTED = "#9098aa"
SUBTLE = "#636b7e"
ACCENT = "#7c83ff"
ACCENT_HOVER = "#959bff"
ACCENT_SOFT = "#272b4f"
REC = "#f0524f"
REC_HOVER = "#ff6d6a"
REC_SOFT = "#3b1d23"
READ = "#34d399"
READ_HOVER = "#5fe3b1"
READ_SOFT = "#153329"
WARN = "#f5b041"
WARN_SOFT = "#3a2f18"
EDITOR_BG = "#12151c"
SELECTION = "#34407a"
PASSAGE_BG = "#1b2940"
WORD_BG = "#3a4a8c"


class Fonts:
    def __init__(self, root: tk.Misc) -> None:
        families = set(tkfont.families(root))
        self.family = next(
            (f for f in ("Ubuntu", "Cantarell", "Inter", "Noto Sans", "DejaVu Sans") if f in families),
            "TkDefaultFont",
        )
        mono = next((f for f in ("Ubuntu Mono", "DejaVu Sans Mono") if f in families), "TkFixedFont")
        family = self.family
        self.body = (family, 10)
        self.body_bold = (family, 10, "bold")
        self.small = (family, 9)
        self.small_bold = (family, 9, "bold")
        self.heading = (family, 11, "bold")
        self.title = (family, 13, "bold")
        self.status = (family, 16, "bold")
        self.editor = (family, 12)
        self.transcript = (family, 11)
        self.mono = (mono, 10)


def apply_theme(root: tk.Tk, fonts: Fonts) -> None:
    root.configure(bg=BG)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(
        ".",
        background=BG,
        foreground=TEXT,
        font=fonts.body,
        bordercolor=BORDER,
        lightcolor=SURFACE_2,
        darkcolor=SURFACE_2,
        troughcolor=SURFACE_2,
        focuscolor=ACCENT,
        selectbackground=SELECTION,
        selectforeground=TEXT,
        fieldbackground=SURFACE_2,
        insertcolor=TEXT,
        arrowcolor=MUTED,
    )
    style.configure(
        "TCombobox",
        fieldbackground=SURFACE_2,
        background=SURFACE_2,
        foreground=TEXT,
        arrowcolor=MUTED,
        bordercolor=BORDER,
        lightcolor=SURFACE_2,
        darkcolor=SURFACE_2,
        padding=(8, 4),
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", SURFACE_2), ("disabled", SURFACE)],
        foreground=[("disabled", SUBTLE), ("readonly", TEXT)],
        background=[("active", SURFACE_3), ("disabled", SURFACE)],
        bordercolor=[("focus", ACCENT)],
        arrowcolor=[("disabled", SUBTLE), ("active", TEXT)],
        selectbackground=[("readonly", SURFACE_2)],
        selectforeground=[("readonly", TEXT)],
    )
    style.configure(
        "TEntry",
        fieldbackground=SURFACE_2,
        foreground=TEXT,
        bordercolor=BORDER,
        lightcolor=SURFACE_2,
        darkcolor=SURFACE_2,
        padding=(8, 4),
        insertcolor=TEXT,
    )
    style.map("TEntry", bordercolor=[("focus", ACCENT)], fieldbackground=[("disabled", SURFACE)])
    style.configure(
        "Horizontal.TScale",
        background=ACCENT,
        troughcolor=SURFACE_3,
        bordercolor=SURFACE_3,
        lightcolor=ACCENT,
        darkcolor=ACCENT,
        sliderlength=16,
        sliderthickness=16,
        gripcount=0,
    )
    style.map("Horizontal.TScale", background=[("active", ACCENT_HOVER)])
    style.layout(
        "Slim.Vertical.TScrollbar",
        [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})],
    )
    style.configure(
        "Slim.Vertical.TScrollbar",
        background=SURFACE_3,
        troughcolor=BG,
        bordercolor=BG,
        lightcolor=SURFACE_3,
        darkcolor=SURFACE_3,
        gripcount=0,
        width=10,
    )
    style.map("Slim.Vertical.TScrollbar", background=[("active", SUBTLE)])
    for pattern, value in (
        ("*TCombobox*Listbox.background", SURFACE_2),
        ("*TCombobox*Listbox.foreground", TEXT),
        ("*TCombobox*Listbox.selectBackground", ACCENT_SOFT),
        ("*TCombobox*Listbox.selectForeground", TEXT),
        ("*TCombobox*Listbox.borderWidth", 0),
        ("*Menu.background", SURFACE_2),
        ("*Menu.foreground", TEXT),
        ("*Menu.activeBackground", ACCENT_SOFT),
        ("*Menu.activeForeground", TEXT),
        ("*Menu.borderWidth", 0),
        ("*Menu.relief", "flat"),
    ):
        root.option_add(pattern, value)
    root.option_add("*TCombobox*Listbox.font", fonts.body)
    root.option_add("*Menu.font", fonts.body)


def parent_bg(widget: tk.Misc) -> str:
    try:
        return str(widget.cget("bg"))
    except tk.TclError:
        return BG


def card(parent: tk.Misc, padx: int = 16, pady: int = 14, bg: str = SURFACE) -> tk.Frame:
    return tk.Frame(
        parent,
        bg=bg,
        highlightthickness=1,
        highlightbackground=BORDER,
        highlightcolor=BORDER,
        padx=padx,
        pady=pady,
        bd=0,
    )


def label(parent: tk.Misc, text: str = "", font: Any = None, fg: str = TEXT, **kwargs: Any) -> tk.Label:
    return tk.Label(parent, text=text, font=font, fg=fg, bg=parent_bg(parent), bd=0, **kwargs)


class FlatButton(tk.Label):
    """A flat, hoverable button (a Label, so it never takes keyboard focus)."""

    VARIANTS: dict[str, tuple[str | None, str, str | None]] = {
        "primary": (ACCENT, "#ffffff", ACCENT_HOVER),
        "secondary": (SURFACE_2, TEXT, SURFACE_3),
        "ghost": (None, MUTED, SURFACE_2),
        "danger": (REC, "#ffffff", REC_HOVER),
        "success": (READ, "#08241a", READ_HOVER),
    }

    def __init__(
        self,
        parent: tk.Misc,
        text: str = "",
        command: Callable[[], None] | None = None,
        variant: str = "secondary",
        font: Any = None,
        padx: int = 12,
        pady: int = 6,
        tooltip: str = "",
    ) -> None:
        super().__init__(parent, text=text, font=font, padx=padx, pady=pady, bd=0, cursor="hand2")
        self._command = command
        self._variant = variant
        self._enabled = True
        self._hover = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._paint()
        if tooltip:
            Tooltip(self, tooltip)

    def _colors(self) -> tuple[str, str, str]:
        bg, fg, hover = self.VARIANTS.get(self._variant, self.VARIANTS["secondary"])
        base = bg if bg is not None else parent_bg(self.master)
        return base, fg, hover or base

    def _paint(self) -> None:
        base, fg, hover = self._colors()
        if not self._enabled:
            self.configure(bg=SURFACE if self._variant != "ghost" else base, fg=SUBTLE, cursor="")
            return
        self.configure(bg=hover if self._hover else base, fg=TEXT if self._hover and self._variant == "ghost" else fg, cursor="hand2")

    def _on_enter(self, _event: Any) -> None:
        self._hover = True
        self._paint()

    def _on_leave(self, _event: Any) -> None:
        self._hover = False
        self._paint()

    def _on_release(self, event: Any) -> None:
        if not self._enabled or self._command is None:
            return
        if 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self._command()

    def set_variant(self, variant: str) -> None:
        self._variant = variant
        self._paint()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._paint()

    def set_text(self, text: str) -> None:
        self.configure(text=text)


def draw_icon(canvas: tk.Canvas, icon: str, cx: float, cy: float, size: float, color: str, phase: float = 0.0) -> None:
    """Vector icons, so nothing depends on emoji fonts (color emoji fonts
    can crash Tk's Xft renderer)."""
    s = size
    if icon == "mic":
        w, h = s * 0.26, s * 0.42
        top, bottom = cy - s * 0.30, cy - s * 0.30 + h
        canvas.create_line(cx, top + w / 2, cx, bottom - w / 2, width=w, fill=color, capstyle="round")
        arc_w = s * 0.46
        canvas.create_arc(
            cx - arc_w / 2, cy - s * 0.28, cx + arc_w / 2, cy + s * 0.20,
            start=180, extent=180, style="arc", outline=color, width=max(2, s * 0.06),
        )
        canvas.create_line(cx, cy + s * 0.20, cx, cy + s * 0.32, fill=color, width=max(2, s * 0.06), capstyle="round")
        canvas.create_line(
            cx - s * 0.13, cy + s * 0.33, cx + s * 0.13, cy + s * 0.33,
            fill=color, width=max(2, s * 0.06), capstyle="round",
        )
    elif icon == "play":
        r = s * 0.26
        canvas.create_polygon(cx - r * 0.7, cy - r, cx - r * 0.7, cy + r, cx + r, cy, fill=color, outline=color, joinstyle="round", width=2)
    elif icon == "pause":
        w, h = s * 0.11, s * 0.44
        for dx in (-s * 0.11, s * 0.11):
            canvas.create_line(cx + dx, cy - h / 2, cx + dx, cy + h / 2, fill=color, width=w, capstyle="round")
    elif icon == "stop":
        r = s * 0.19
        canvas.create_rectangle(cx - r, cy - r, cx + r, cy + r, fill=color, outline=color, width=2)
    elif icon == "spinner":
        r = s * 0.24
        canvas.create_arc(
            cx - r, cy - r, cx + r, cy + r,
            start=(phase * 360) % 360, extent=270, style="arc", outline=color, width=max(2, s * 0.07),
        )
    elif icon == "check":
        canvas.create_line(
            cx - s * 0.22, cy, cx - s * 0.06, cy + s * 0.16, cx + s * 0.24, cy - s * 0.18,
            fill=color, width=max(2, s * 0.08), capstyle="round", joinstyle="round",
        )
    elif icon == "cross":
        r = s * 0.18
        width = max(2, s * 0.08)
        canvas.create_line(cx - r, cy - r, cx + r, cy + r, fill=color, width=width, capstyle="round")
        canvas.create_line(cx - r, cy + r, cx + r, cy - r, fill=color, width=width, capstyle="round")
    elif icon == "clipboard":
        w, h = s * 0.36, s * 0.46
        canvas.create_rectangle(cx - w / 2, cy - h / 2 + s * 0.04, cx + w / 2, cy + h / 2, outline=color, width=max(2, s * 0.06))
        canvas.create_line(cx - w / 4, cy - h / 2 + s * 0.04, cx + w / 4, cy - h / 2 + s * 0.04, fill=color, width=max(3, s * 0.1), capstyle="round")


class CircleButton(tk.Canvas):
    """Round icon button with an optional level ring (the pulsing mic)."""

    def __init__(
        self,
        parent: tk.Misc,
        size: int,
        icon: str,
        command: Callable[[], None] | None = None,
        fill: str = SURFACE_2,
        fg: str = TEXT,
        hover: str = SURFACE_3,
        ring_space: int = 10,
        tooltip: str = "",
    ) -> None:
        total = size + ring_space * 2
        super().__init__(parent, width=total, height=total, bg=parent_bg(parent), highlightthickness=0, bd=0, cursor="hand2")
        self.size = size
        self.ring_space = ring_space
        self.icon = icon
        self.fill = fill
        self.fg = fg
        self.hover_fill = hover
        self.ring_color: str | None = None
        self.level = 0.0
        self.phase = 0.0
        self._hover = False
        self._command = command
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._on_release)
        self.redraw()
        if tooltip:
            Tooltip(self, tooltip)

    def _set_hover(self, hover: bool) -> None:
        self._hover = hover
        self.redraw()

    def _on_release(self, event: Any) -> None:
        if self._command is not None and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self._command()

    def set_look(
        self,
        icon: str | None = None,
        fill: str | None = None,
        fg: str | None = None,
        hover: str | None = None,
        ring: str | None | bool = False,
    ) -> None:
        if icon is not None:
            self.icon = icon
        if fill is not None:
            self.fill = fill
        if fg is not None:
            self.fg = fg
        if hover is not None:
            self.hover_fill = hover
        if ring is not False:
            self.ring_color = ring  # type: ignore[assignment]
        self.redraw()

    def set_level(self, level: float, phase: float | None = None) -> None:
        self.level = max(0.0, min(1.0, level))
        if phase is not None:
            self.phase = phase
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        total = self.size + self.ring_space * 2
        c = total / 2
        r = self.size / 2
        if self.ring_color:
            ring_r = r + 2 + self.level * (self.ring_space - 2)
            self.create_oval(c - ring_r, c - ring_r, c + ring_r, c + ring_r, fill=self.ring_color, outline="")
        fill = self.hover_fill if self._hover else self.fill
        self.create_oval(c - r, c - r, c + r, c + r, fill=fill, outline="")
        draw_icon(self, self.icon, c, c, self.size, self.fg, self.phase)


class Switch(tk.Frame):
    """A toggle switch bound to a BooleanVar."""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        variable: tk.BooleanVar,
        command: Callable[[], None] | None = None,
        font: Any = None,
        hint: str = "",
        hint_font: Any = None,
    ) -> None:
        bg = parent_bg(parent)
        super().__init__(parent, bg=bg)
        self.variable = variable
        self._command = command
        self._enabled = True
        self.canvas = tk.Canvas(self, width=38, height=22, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.canvas.grid(row=0, column=0, rowspan=2 if hint else 1, sticky="n", pady=(1, 0))
        self.text_label = tk.Label(self, text=text, font=font, fg=TEXT, bg=bg, anchor="w", justify="left", cursor="hand2")
        self.text_label.grid(row=0, column=1, sticky="w", padx=(10, 0))
        if hint:
            tk.Label(self, text=hint, font=hint_font, fg=SUBTLE, bg=bg, anchor="w", justify="left", wraplength=520).grid(
                row=1, column=1, sticky="w", padx=(10, 0)
            )
        self.columnconfigure(1, weight=1)
        for widget in (self.canvas, self.text_label):
            widget.bind("<ButtonRelease-1>", self._toggle)
        variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _toggle(self, _event: Any = None) -> None:
        if not self._enabled:
            return
        self.variable.set(not bool(self.variable.get()))
        if self._command is not None:
            self._command()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.text_label.configure(fg=TEXT if enabled else SUBTLE)
        self._draw()

    def _draw(self) -> None:
        on = bool(self.variable.get())
        self.canvas.delete("all")
        track = (ACCENT if on else SURFACE_3) if self._enabled else SURFACE_2
        self.canvas.create_line(11, 11, 27, 11, width=20, fill=track, capstyle="round")
        knob_x = 27 if on else 11
        knob = "#ffffff" if on else (MUTED if self._enabled else SUBTLE)
        self.canvas.create_oval(knob_x - 7, 4, knob_x + 7, 18, fill=knob, outline="")


class Segmented(tk.Frame):
    """A row of mutually exclusive options (tabs, modes)."""

    def __init__(
        self,
        parent: tk.Misc,
        options: list[tuple[str, str]],
        command: Callable[[str], None] | None = None,
        value: str | None = None,
        font: Any = None,
        padx: int = 14,
        pady: int = 6,
        selected_bg: str = ACCENT_SOFT,
        selected_fg: str = TEXT,
    ) -> None:
        super().__init__(parent, bg=SURFACE_2, padx=3, pady=3)
        self._command = command
        self._labels: dict[str, tk.Label] = {}
        self._value = value
        self._selected_bg = selected_bg
        self._selected_fg = selected_fg
        for index, (key, text) in enumerate(options):
            item = tk.Label(self, text=text, font=font, padx=padx, pady=pady, bd=0, cursor="hand2")
            item.grid(row=0, column=index, padx=(0 if index == 0 else 2, 0))
            item.bind("<ButtonRelease-1>", lambda _e, k=key: self._select(k))
            item.bind("<Enter>", lambda _e, k=key: self._hover(k, True))
            item.bind("<Leave>", lambda _e, k=key: self._hover(k, False))
            self._labels[key] = item
        self._paint()

    def _select(self, key: str) -> None:
        self.set(key)
        if self._command is not None:
            self._command(key)

    def _hover(self, key: str, hover: bool) -> None:
        if key == self._value:
            return
        self._labels[key].configure(fg=TEXT if hover else MUTED, bg=SURFACE_3 if hover else SURFACE_2)

    def set(self, key: str | None) -> None:
        self._value = key
        self._paint()

    def get(self) -> str | None:
        return self._value

    def set_accent(self, selected_bg: str, selected_fg: str = TEXT) -> None:
        self._selected_bg = selected_bg
        self._selected_fg = selected_fg
        self._paint()

    def _paint(self) -> None:
        for key, item in self._labels.items():
            if key == self._value:
                item.configure(bg=self._selected_bg, fg=self._selected_fg)
            else:
                item.configure(bg=SURFACE_2, fg=MUTED)


class ScrollFrame(tk.Frame):
    """Vertically scrolling container; put children in .body."""

    def __init__(self, parent: tk.Misc, bg: str = BG) -> None:
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview, style="Slim.Vertical.TScrollbar")
        self.body = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        self.bind("<Enter>", lambda _e: self._bind_wheel(True))
        self.bind("<Leave>", lambda _e: self._bind_wheel(False))

    def _on_scroll(self, first: str, last: str) -> None:
        self.bar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.bar.pack_forget()
        elif not self.bar.winfo_ismapped():
            self.bar.pack(side="right", fill="y")

    def _bind_wheel(self, on: bool) -> None:
        for sequence in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            if on:
                self.bind_all(sequence, self._on_wheel, add="+")
            else:
                self.unbind_all(sequence)

    def _on_wheel(self, event: Any) -> None:
        try:
            if event.widget.winfo_class() in {"Text", "Listbox"}:
                return
        except Exception:
            pass
        if getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0:
            self.canvas.yview_scroll(-3, "units")
        else:
            self.canvas.yview_scroll(3, "units")


class Tooltip:
    def __init__(self, widget: tk.Misc, text: str) -> None:
        self.widget = widget
        self.text = text
        self._tip: tk.Toplevel | None = None
        self._after: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: Any) -> None:
        self._after = self.widget.after(600, self._show)

    def _show(self) -> None:
        if self._tip is not None:
            return
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(tip, text=self.text, bg=SURFACE_3, fg=TEXT, padx=8, pady=4, justify="left", wraplength=320).pack()
        self._tip = tip

    def _hide(self, _event: Any = None) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def form_row(parent: tk.Misc, row: int, title: str, widget: tk.Misc, fonts: Fonts, hint: str = "") -> None:
    """Label on the left, control on the right, optional hint below."""
    tk.Label(parent, text=title, font=fonts.body, fg=MUTED, bg=parent_bg(parent), anchor="w").grid(
        row=row * 2, column=0, sticky="w", pady=(6, 0), padx=(0, 16)
    )
    widget.grid(row=row * 2, column=1, sticky="we", pady=(6, 0))
    if hint:
        tk.Label(
            parent, text=hint, font=fonts.small, fg=SUBTLE, bg=parent_bg(parent), anchor="w", justify="left", wraplength=460
        ).grid(row=row * 2 + 1, column=1, sticky="w", pady=(2, 0))
    parent.columnconfigure(0, minsize=210)
    parent.columnconfigure(1, weight=1)
