#!/usr/bin/env python3
"""VNDB -> LinguaGacha glossary builder.

A small, dependency-free Tkinter front end that searches visual novels on
VNDB, pulls their character cast, and turns that into a glossary file that
LinguaGacha accepts:

    [{"src": "茜ヶ崎 空", "dst": "Akanegasaki Sora", "info": "Female, Main character, Romanization: Sora Akanegasaki"}, ...]

Run it with::

    python vndb_glossary.py

Only the Python standard library is required.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from typing import Any, Callable, Sequence

import glossary_core as core
from glossary_core import ERROR, INFO, LEVEL_LABELS, WARNING, BuildOptions, Glossary, Term
from vndb_client import VndbClient, VndbError, parse_vn_id

APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / "settings.json"

PRESET_LANG_LABELS = {
    "zh": "Chinese (zh)",
    "en": "English (en)",
    "user": "User-defined (user)",
}

ON_MARK = "\u2713"


# ==========================================================================
# settings
# ==========================================================================


def load_settings() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "lingua_gacha_root": "",
        "preset_lang": core.DEFAULT_PRESET_LANG,
        "last_dir": str(APP_DIR),
        "endpoint": "https://api.vndb.org/kana",
        "token": "",
        "include_aliases": True,
        "alias_min_len": 2,
        "only_japanese_original": True,
        "strip_spaces": False,
        "prefill_dst": True,
        "include_traits": True,
        "max_traits": 4,
        "include_spoiler_info": True,
        "include_reading": False,
        "export_shape": core.SHAPE_LIST,
        "skip_untranslated": True,
    }
    if SETTINGS_PATH.is_file():
        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                defaults.update({k: v for k, v in stored.items() if k in defaults})
        except (OSError, json.JSONDecodeError):
            pass
    return defaults


def save_settings(settings: dict[str, Any]) -> None:
    try:
        SETTINGS_PATH.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


# ==========================================================================
# reusable editable treeview
# ==========================================================================


class EditableTree(ttk.Frame):
    """A Treeview whose text columns can be edited in place."""

    def __init__(
        self,
        parent: tk.Misc,
        columns: Sequence[tuple[str, str, int, str]],
        *,
        on_commit: Callable[[str, str, str], None],
        toggle_column: str | None = None,
        editable_columns: Sequence[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.on_commit = on_commit
        self.toggle_column = toggle_column
        self.editable_columns = set(editable_columns or [c[0] for c in columns])
        self._editor: ttk.Entry | None = None
        self._editing: tuple[str, str] | None = None
        self._cancel = False

        self.tree = ttk.Treeview(
            self, columns=[c[0] for c in columns], show="headings", selectmode="extended"
        )
        for cid, text, width, anchor in columns:
            self.tree.heading(cid, text=text, command=lambda c=cid: self.sort_by(c))
            self.tree.column(
                cid, width=width, minwidth=40, anchor=anchor,
                stretch=cid != toggle_column if toggle_column else True,
            )

        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self._sort_column: str | None = None
        self._sort_reverse = False

        self.tree.bind("<Double-1>", self._begin_edit)
        self.tree.bind("<Return>", self._begin_edit)
        self.tree.bind("<Button-1>", self._maybe_toggle)
        self.tree.bind("<Escape>", lambda _e: self.close_editor())

    # -- sorting -----------------------------------------------------
    def sort_by(self, column: str) -> None:
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column, self._sort_reverse = column, False
        self.update_sort_indicators()

    def update_sort_indicators(self) -> None:
        for column in self.tree["columns"]:
            base = self.tree.heading(column, "text").lstrip("\u25b2\u25bc ")
            self.tree.heading(column, text=base)
        if self._sort_column:
            arrow = "\u25bc" if self._sort_reverse else "\u25b2"
            text = self.tree.heading(self._sort_column, "text")
            self.tree.heading(self._sort_column, text=f"{arrow} {text}")

    # -- helpers -----------------------------------------------------
    def get(self, iid: str, column: str) -> str:
        try:
            return self.tree.set(iid, column)
        except tk.TclError:
            return ""

    def close_editor(self) -> None:
        editor, self._editor = self._editor, None
        self._editing = None
        self._cancel = False
        if editor is not None:
            editor.destroy()

    def _maybe_toggle(self, event: tk.Event) -> str | None:
        if self.toggle_column is None or self._editor is not None:
            return None
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.tree.identify_column(event.x) != self.toggle_column:
            return None
        iid = self.tree.identify_row(event.y)
        if not iid:
            return None
        self.on_commit(iid, "enabled", "")
        return "break"

    def _begin_edit(self, event: tk.Event) -> str | None:
        if self._editor is not None:
            return None
        iid = self.tree.identify_row(event.y)
        column_id = self.tree.identify_column(event.x)
        if event.type == "<Return>":
            selection = self.tree.selection()
            if not selection:
                return None
            iid = selection[0]
            column_id = None
            columns = self.tree["columns"]
            for position, candidate in enumerate(columns, start=1):
                bbox = self.tree.bbox(iid, f"#{position}")
                if bbox and bbox[1] <= event.y <= bbox[1] + bbox[3]:
                    column_id = f"#{position}"
                    break
            if column_id is None:
                column_id = "#1"
        if not iid or not column_id:
            return None

        cid = self.tree["columns"][int(column_id[1:]) - 1]
        if cid not in self.editable_columns:
            return None

        bbox = self.tree.bbox(iid, column_id)
        if not bbox:
            return None
        x, y, width, height = bbox
        var = tk.StringVar(value=self.get(iid, cid))
        editor = ttk.Entry(self.tree, textvariable=var)
        editor.place(x=x, y=y, width=width, height=height)
        editor.focus_set()
        editor.select_range(0, "end")
        self._editor, self._editing = editor, (iid, cid)

        editor.bind("<Return>", self._commit)
        editor.bind("<KP_Enter>", self._commit)
        editor.bind("<Escape>", self._cancel_edit)
        editor.bind("<FocusOut>", self._commit)
        return "break"

    def _commit(self, _event: tk.Event | None = None) -> None:
        if self._editor is None or self._editing is None:
            return
        iid, cid = self._editing
        value = self._editor.get()
        self.close_editor()
        if self._cancel:
            return
        self.on_commit(iid, cid, value)

    def _cancel_edit(self, _event: tk.Event) -> str:
        self._cancel = True
        self.close_editor()
        return "break"


# ==========================================================================
# main window
# ==========================================================================


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.settings = load_settings()

        self.title("VNDB Glossary Builder  \u00b7  LinguaGacha Glossary Builder")
        self.geometry("1220x860")
        self.minsize(960, 640)

        self.glossary = Glossary()
        self.vn_results: list[dict[str, Any]] = []
        self.vn_iids: dict[str, dict[str, Any]] = {}
        self.selected_vn: dict[str, Any] | None = None
        self.issues: list[core.Issue] = []
        self.busy = False
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self._closed = False
        self._build_mode = "append"
        self._result_handlers: dict[str, Callable[[Any], None]] = {
            "search": self._on_search_done,
            "build": self._on_build_done,
        }

        self._configure_style()
        self._build_menu()
        self._build_ui()
        self._bind_keys()
        self.after(80, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------
    def _configure_style(self) -> None:
        style = ttk.Style(self)
        if os.name == "nt" and "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=22, font=("Segoe UI", 9))
        style.configure("Headline.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Dim.TLabel", foreground="#666666")
        self.option_add("*Font", ("Segoe UI", 9))
        style.configure(".", font=("Segoe UI", 9))

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Import Glossary\u2026", accelerator="Ctrl+O",
                               command=self.action_import)
        file_menu.add_command(label="Merge Import\u2026", command=self.action_import_merge)
        file_menu.add_separator()
        file_menu.add_command(label="Export JSON\u2026", accelerator="Ctrl+S",
                               command=self.action_export_json)
        file_menu.add_command(label="Export XLSX\u2026", accelerator="Ctrl+E",
                               command=self.action_export_xlsx)
        file_menu.add_separator()
        file_menu.add_command(label="Save as LinguaGacha Preset\u2026",
                               command=self.action_save_preset)
        file_menu.add_command(label="Load LinguaGacha Preset\u2026",
                               command=self.action_load_preset)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Validate Glossary", accelerator="F5",
                               command=self.action_validate)
        tools_menu.add_command(label="Remove Duplicate Sources", command=self.action_dedupe)
        tools_menu.add_command(label="Add Blank Row", command=lambda: self.action_add_row(""))
        tools_menu.add_separator()
        tools_menu.add_command(label="Enable All", command=lambda: self.action_set_enabled(True))
        tools_menu.add_command(label="Disable All", command=lambda: self.action_set_enabled(False))
        menubar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Open VNDB API Docs", command=self.action_api_docs)
        help_menu.add_command(label="How to Use", command=self.action_help)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.configure(menu=menubar)

    def _bind_keys(self) -> None:
        self.bind("<Control-o>", lambda _e: self.action_import())
        self.bind("<Control-s>", lambda _e: self.action_export_json())
        self.bind("<Control-e>", lambda _e: self.action_export_xlsx())
        self.bind("<Control-f>", lambda _e: self.filter_entry.focus_set())
        self.bind("<F5>", lambda _e: self.action_validate())
        self.bind("<Delete>", lambda _e: self.action_delete_selected())

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        paned.add(self._build_vn_frame(paned), weight=0)

        self.middle_tabs = ttk.Notebook(paned)
        self.middle_tabs.add(self._build_glossary_frame(self.middle_tabs), text="Glossary")
        self.middle_tabs.add(self._build_character_frame(self.middle_tabs), text="Characters")
        paned.add(self.middle_tabs, weight=1)

        paned.add(self._build_log_frame(paned), weight=0)

        status = ttk.Frame(self)
        status.pack(fill="x", side="bottom", padx=10, pady=(2, 6))
        self.status_var = tk.StringVar(
            value="Ready. Search for a VN above, select it, then click Fetch Characters."
        )
        ttk.Label(status, textvariable=self.status_var, anchor="w").pack(side="left")
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=140)
        self.progress.pack(side="right", padx=(8, 0))

    # -- VN panel ----------------------------------------------------
    def _build_vn_frame(self, parent: tk.Misc) -> ttk.Frame:
        outer = ttk.Labelframe(parent, text=" 1 \u00b7 Find a visual novel on VNDB ", padding=6)
        inner = ttk.Frame(outer)
        inner.pack(fill="both", expand=True)

        search_bar = ttk.Frame(inner)
        search_bar.pack(fill="x")
        ttk.Label(search_bar, text="Search or vndbid:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_bar, textvariable=self.search_var, width=42)
        self.search_entry.pack(side="left", padx=(2, 6))
        self.search_entry.bind("<Return>", lambda _e: self.action_search())
        ttk.Button(search_bar, text="Search VNs", command=self.action_search).pack(side="left")
        ttk.Button(search_bar, text="Load by vndbid", command=self.action_search_by_id).pack(side="left", padx=4)
        ttk.Button(search_bar, text="Open on vndb.org", command=self.action_open_vndb).pack(side="left")

        splitter = ttk.PanedWindow(inner, orient="horizontal")
        splitter.pack(fill="both", expand=True, pady=(6, 0))

        left = ttk.Frame(splitter)
        ttk.Label(left, text="Visual novels", style="Headline.TLabel").pack(anchor="w", pady=(0, 2))
        self.vn_tree = ttk.Treeview(
            left, columns=("id", "title", "alttitle", "released", "olang"),
            show="headings", selectmode="browse", height=7,
        )
        for cid, text, width in (
            ("id", "ID", 55),
            ("title", "Title", 250),
            ("alttitle", "Original", 150),
            ("released", "Released", 85),
            ("olang", "Lang", 50),
        ):
            self.vn_tree.heading(cid, text=text)
            self.vn_tree.column(cid, width=width, anchor="w")
        self.vn_tree.tag_configure("odd", background="#f5f7fa")
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.vn_tree.yview)
        self.vn_tree.configure(yscrollcommand=vsb.set)
        self.vn_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.vn_tree.bind("<<TreeviewSelect>>", self._on_vn_selected)
        self.vn_tree.bind("<Double-1>", lambda _e: self.action_build("append"))
        splitter.add(left, weight=3)

        right = ttk.Frame(splitter)
        ttk.Label(right, text="Build options", style="Headline.TLabel").pack(anchor="w", pady=(0, 2))

        options = ttk.Labelframe(right, text="Characters", padding=6)
        options.pack(fill="both", expand=True)
        self.opt_aliases = tk.BooleanVar(value=self.settings["include_aliases"])
        self.opt_alias_min = tk.IntVar(value=self.settings["alias_min_len"])
        self.opt_jp_only = tk.BooleanVar(value=self.settings["only_japanese_original"])
        self.opt_strip = tk.BooleanVar(value=self.settings["strip_spaces"])
        self.opt_prefill = tk.BooleanVar(value=self.settings["prefill_dst"])
        ttk.Checkbutton(options, text="Include aliases / nicknames",
                        variable=self.opt_aliases).pack(anchor="w")
        row = ttk.Frame(options)
        row.pack(anchor="w", fill="x")
        ttk.Label(row, text="Min alias length:").pack(side="left")
        ttk.Spinbox(row, from_=1, to=12, width=4, textvariable=self.opt_alias_min).pack(side="left")
        ttk.Checkbutton(options, text="Only Japanese source names",
                        variable=self.opt_jp_only).pack(anchor="w")
        ttk.Checkbutton(options, text="Strip spaces from source names",
                        variable=self.opt_strip).pack(anchor="w")
        ttk.Checkbutton(options, text="Fill dst with romanized name",
                        variable=self.opt_prefill).pack(anchor="w")

        traits = ttk.Labelframe(right, text="Info column", padding=6)
        traits.pack(fill="x", pady=(6, 0))
        self.opt_traits = tk.BooleanVar(value=self.settings["include_traits"])
        self.opt_trait_max = tk.IntVar(value=self.settings["max_traits"])
        self.opt_spoiler = tk.BooleanVar(value=self.settings["include_spoiler_info"])
        self.opt_reading = tk.BooleanVar(value=self.settings["include_reading"])
        ttk.Checkbutton(traits, text="Write character traits",
                        variable=self.opt_traits).pack(anchor="w")
        row2 = ttk.Frame(traits)
        row2.pack(anchor="w", fill="x")
        ttk.Label(row2, text="Max traits:").pack(side="left")
        ttk.Spinbox(row2, from_=0, to=20, width=4, textvariable=self.opt_trait_max).pack(side="left")
        ttk.Checkbutton(traits, text="Flag spoilered characters",
                        variable=self.opt_spoiler).pack(anchor="w")
        ttk.Checkbutton(traits, text="Also repeat romanization in info",
                        variable=self.opt_reading).pack(anchor="w")

        buttons = ttk.Frame(right)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Fetch Characters \u2192 Append",
                   command=lambda: self.action_build("append")).pack(fill="x")
        ttk.Button(buttons, text="Fetch Characters \u2192 Replace All",
                   command=lambda: self.action_build("replace")).pack(fill="x", pady=(4, 0))
        self.vn_label = ttk.Label(right, text="No visual novel selected",
                                  style="Dim.TLabel", wraplength=330, justify="left")
        self.vn_label.pack(anchor="w", pady=(8, 0))
        splitter.add(right, weight=2)

        return outer

    # -- glossary panel ----------------------------------------------
    def _build_glossary_frame(self, parent: tk.Misc) -> ttk.Frame:
        outer = ttk.Frame(parent, padding=6)

        ttk.Label(
            outer,
            text=" 2 · Glossary — double-click a cell to edit. Click the first column to "
                 "enable or disable a row. Only enabled rows are exported.",
            style="Dim.TLabel",
        ).pack(anchor="w", pady=(0, 4))

        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        for text, command in (
            ("Import\u2026", self.action_import),
            ("Merge Import", self.action_import_merge),
            ("Export JSON", self.action_export_json),
            ("Export XLSX", self.action_export_xlsx),
            ("Save Preset", self.action_save_preset),
            ("Load Preset", self.action_load_preset),
        ):
            ttk.Button(bar, text=text, command=command).pack(side="left", padx=(0, 4))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(bar, text="Add Row", command=lambda: self.action_add_row("")).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="Delete Selected", command=self.action_delete_selected).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="Dedupe", command=self.action_dedupe).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="Validate", command=self.action_validate).pack(side="left")

        filters = ttk.Frame(outer)
        filters.pack(fill="x", pady=(6, 4))
        ttk.Label(filters, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_entry = ttk.Entry(filters, textvariable=self.filter_var, width=32)
        self.filter_entry.pack(side="left", padx=(2, 8))
        self.filter_var.trace_add("write", lambda *_: self._refresh_tree())
        self.only_untranslated_var = tk.BooleanVar()
        ttk.Checkbutton(filters, text="Show untranslated only", variable=self.only_untranslated_var,
                        command=self._refresh_tree).pack(side="left")
        self.skip_untranslated = tk.BooleanVar(value=self.settings["skip_untranslated"])
        ttk.Checkbutton(filters, text="Skip untranslated on export",
                        variable=self.skip_untranslated,
                        command=self._update_status).pack(side="left", padx=12)
        ttk.Label(filters, text="Export format:", style="Dim.TLabel").pack(side="left")
        self.shape_var = tk.StringVar(value=self.settings.get("export_shape", core.SHAPE_LIST))
        shape_box = ttk.Combobox(
            filters, textvariable=self.shape_var, state="readonly", width=20,
            values=[core.SHAPE_LIST, core.SHAPE_DICT],
        )
        shape_box.pack(side="left")
        shape_box.bind("<<ComboboxSelected>>", lambda _e: self._update_status())
        self.count_var = tk.StringVar(value="0 terms")
        ttk.Label(filters, textvariable=self.count_var, style="Dim.TLabel").pack(side="right")

        self.glossary_tree = EditableTree(
            outer,
            (
                ("on", "\u2713", 34, "center"),
                ("src", "Source (src)", 220, "w"),
                ("dst", "Translation (dst)", 220, "w"),
                ("info", "Description (info)", 420, "w"),
                ("origin", "Source ID", 80, "w"),
            ),
            on_commit=self._on_cell_commit,
            toggle_column="on",
            editable_columns=("src", "dst", "info"),
        )
        self.glossary_tree.pack(fill="both", expand=True, pady=(4, 0))
        tree = self.glossary_tree.tree
        tree.tag_configure("odd", background="#f5f7fa")
        tree.tag_configure("error", background="#ffd9d9")
        tree.tag_configure("warning", background="#fff0cd")
        tree.tag_configure("off", foreground="#9a9a9a")

        return outer

    # -- character wiki view -------------------------------------------
    def _build_character_frame(self, parent: tk.Misc) -> ttk.Frame:
        outer = ttk.Frame(parent, padding=6)
        ttk.Label(
            outer,
            text=" Characters as listed on the VNDB character page, grouped by role.",
            style="Dim.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        self.char_text = tk.Text(
            outer, wrap="word", state="disabled",
            font=("Segoe UI", 10), background="#ffffff", relief="flat",
            padx=10, pady=8,
        )
        scroll = ttk.Scrollbar(outer, orient="vertical", command=self.char_text.yview)
        self.char_text.configure(yscrollcommand=scroll.set)
        self.char_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

        self.char_text.tag_configure("h_section", font=("Segoe UI", 13, "bold"), foreground="#3366aa", spacing1=10, spacing3=4)
        self.char_text.tag_configure("h_char", font=("Segoe UI", 11, "bold"), foreground="#1a1a1a", spacing1=8)
        self.char_text.tag_configure("original", foreground="#555555")
        self.char_text.tag_configure("label", font=("Segoe UI", 9, "bold"), foreground="#444444")
        self.char_text.tag_configure("value", foreground="#333333")
        self.char_text.tag_configure("desc", foreground="#222222", lmargin1=8, lmargin2=8)
        self.char_text.tag_configure("placeholder", foreground="#888888")
        return outer

    def render_characters(self, vn: dict[str, Any], characters: list[dict[str, Any]]) -> None:
        self.char_text.configure(state="normal")
        self.char_text.delete("1.0", "end")

        if not characters:
            self.char_text.insert("end", "No characters fetched yet. Fetch a VN's characters to see them here.\n", "placeholder")
            self.char_text.configure(state="disabled")
            return

        grouped: dict[str, list[dict[str, Any]]] = {"Protagonist": [], "Main characters": [], "Side characters": [], "Appears": [], "Other": []}
        role_titles = {"main": "Protagonist", "primary": "Main characters", "side": "Side characters", "appears": "Appears"}

        for c in characters:
            role = ""
            spoiler = 0
            for v in c.get("vns") or []:
                if v.get("id") == vn.get("id"):
                    role = v.get("role") or ""
                    spoiler = int(v.get("spoiler") or 0)
                    break
            grouped.setdefault(role_titles.get(role, "Other"), []).append({**c, "_spoiler": spoiler})

        role_order = ["Protagonist", "Main characters", "Side characters", "Appears", "Other"]
        for section in role_order:
            chars = grouped.get(section) or []
            if not chars:
                continue
            self.char_text.insert("end", f"{section}\n", "h_section")
            for c in chars:
                self._render_character(c, vn.get("id", ""))
            self.char_text.insert("end", "\n")

        self.char_text.configure(state="disabled")

    def _render_character(self, c: dict[str, Any], vn_id: str) -> None:
        t = self.char_text
        name = c.get("name", "?")
        original = c.get("original") or ""
        spoiler = int(c.get("_spoiler", 0) or 0)
        spoiler_note = "  [Spoiler]" if spoiler >= 2 else ""
        t.insert("end", f"{name}\n", "h_char")
        if original:
            t.insert("end", f"{original}\n", "original")
        aliases = [a for a in (c.get("aliases") or []) if a]
        if aliases:
            t.insert("end", "Aliases: ", "label")
            t.insert("end", ", ".join(aliases) + "\n", "value")

        measures = []
        if c.get("height"): measures.append(f"Height: {c['height']}cm")
        bwh = []
        if c.get("bust"): bwh.append(str(c["bust"]))
        if c.get("waist"): bwh.append(str(c["waist"]))
        if c.get("hips"): bwh.append(str(c["hips"]))
        if bwh: measures.append("Bust-Waist-Hips: " + "-".join(bwh) + "cm")
        if c.get("cup"): measures.append(f"{c['cup']} cup")
        if c.get("weight"): measures.append(f"Weight: {c['weight']}kg")
        if measures:
            t.insert("end", "Measurements: ", "label")
            t.insert("end", ", ".join(measures) + "\n", "value")
        if c.get("birthday"):
            b = c["birthday"]
            t.insert("end", "Birthday: ", "label")
            t.insert("end", f"{b[1]}/{b[0]}" if isinstance(b, (list, tuple)) and len(b) >= 2 else str(b), "value")
            t.insert("end", "\n")
        if c.get("blood_type"):
            t.insert("end", "Blood type: ", "label")
            t.insert("end", f"{c['blood_type'].upper()}\n", "value")
        if c.get("age"):
            t.insert("end", "Age: ", "label")
            t.insert("end", f"{c['age']}\n", "value")
        if spoiler_note:
            t.insert("end", spoiler_note.strip() + "\n", "original")

        traits_by_group: dict[str, list[str]] = {}
        for tr in c.get("traits") or []:
            g = (tr.get("group_name") or "Other").strip()
            traits_by_group.setdefault(g, [])
            n = (tr.get("name") or "").strip()
            if n and n not in traits_by_group[g]:
                traits_by_group[g].append(n)
        for g in sorted(traits_by_group):
            t.insert("end", f"{g}: ", "label")
            t.insert("end", ", ".join(traits_by_group[g]) + "\n", "value")

        desc = (c.get("description") or "").strip()
        if desc:
            t.insert("end", "\n")
            self._insert_cleaned(desc)
        t.insert("end", "\n")

    def _insert_cleaned(self, text: str) -> None:
        # Strip VNDB formatting codes like [url=...]...[/url], [b]...[/b], [quote]...
        import re as _re
        text = _re.sub(r"\[/?url(=[^\]]*)?\]", "", text)
        text = _re.sub(r"\[/?(b|i|u|s|spoiler|quote|raw|code|noparse|center|t\d+)\]", "", text)
        text = _re.sub(r"\r\n", "\n", text)
        text = _re.sub(r"\n{3,}", "\n\n", text).strip()
        self.char_text.insert("end", text + "\n", "desc")

    # -- log panel ---------------------------------------------------
    def _build_log_frame(self, parent: tk.Misc) -> ttk.Frame:
        outer = ttk.Labelframe(parent, text=" 3 \u00b7 Validation / log ", padding=6)
        self.log_text = tk.Text(outer, height=7, wrap="word", state="disabled",
                                font=("Consolas", 9), background="#fbfbfb", relief="flat")
        scroll = ttk.Scrollbar(outer, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.log_text.tag_configure("error", foreground="#b00020")
        self.log_text.tag_configure("warning", foreground="#9a6400")
        self.log_text.tag_configure("info", foreground="#0a5ca8")
        self.log_text.tag_configure("good", foreground="#1b6e28")
        return outer

    # ------------------------------------------------------------------
    # logging / status
    # ------------------------------------------------------------------
    def log(self, message: str, level: str = "info") -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n", level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _update_status(self, message: str | None = None) -> None:
        if message is not None:
            self.status_var.set(message)
            return
        stats = self.glossary.stats()
        shape = self.shape_var.get()
        terms = self.glossary.exportable(require_dst=self.skip_untranslated.get())
        suffix = "" if shape == core.SHAPE_LIST else " (current format drops info)"
        self.status_var.set(
            f"{stats['total']} terms \u00b7 {stats['enabled']} enabled \u00b7 "
            f"{stats['untranslated']} untranslated \u00b7 exporting {len(terms)}{suffix}"
        )

    # ------------------------------------------------------------------
    # async plumbing
    # ------------------------------------------------------------------
    def _client(self) -> VndbClient:
        return VndbClient(
            endpoint=self.settings.get("endpoint") or "https://api.vndb.org/kana",
            token=self.settings.get("token", ""),
        )

    def _run_async(self, label: str, work: Callable[[Callable[[str], None]], Any]) -> None:
        if self.busy:
            self.log("The previous request has not finished yet.", "warning")
            return
        self.busy = True
        self.progress.start(12)
        self._update_status(f"{label}\u2026")

        def report(message: str) -> None:
            self.queue.put(("progress", message))

        def target() -> None:
            try:
                result = work(report)
            except VndbError as exc:
                self.queue.put(("error", str(exc)))
            except Exception as exc:  # noqa: BLE001 - surface everything to the UI
                self.queue.put(("error", f"{exc}\n{traceback.format_exc(limit=3)}"))
            else:
                self.queue.put(("done", result))

        threading.Thread(target=target, daemon=True).start()

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "progress":
                    self._update_status(payload)
                elif kind == "error":
                    self._finish()
                    self._update_status("Request failed")
                    self.log(f"[Error] {payload}", "error")
                    messagebox.showerror("Request failed", str(payload).splitlines()[0])
                elif kind == "done":
                    self._finish()
                    handler, result = payload
                    self._result_handlers[handler](result)
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _finish(self) -> None:
        self.busy = False
        self.progress.stop()

    # ------------------------------------------------------------------
    # VN actions
    # ------------------------------------------------------------------
    def action_search(self) -> None:
        text = self.search_var.get().strip()
        if not text:
            self.log("Enter something to search for.", "warning")
            return

        def work(report: Callable[[str], None]) -> tuple[str, Any]:
            report(f"Searching for \"{text}\"\u2026")
            return "search", self._client().search_vns(text)

        self._run_async("Searching VNDB", work)

    def action_search_by_id(self) -> None:
        vn_id = parse_vn_id(self.search_var.get())
        if not vn_id:
            messagebox.showwarning(
                "vndbid required",
                "Enter a vndbid such as v17 or https://vndb.org/v17",
            )
            return
        self.search_var.set(vn_id)
        self.action_search()

    def _on_search_done(self, results: list[dict[str, Any]]) -> None:
        self.vn_results = results
        self.vn_iids.clear()
        for item in self.vn_tree.get_children():
            self.vn_tree.delete(item)
        for index, vn in enumerate(results):
            iid = f"v{index}"
            self.vn_iids[iid] = vn
            self.vn_tree.insert(
                "", "end", iid=iid,
                values=(
                    vn.get("id", ""),
                    vn.get("title", ""),
                    vn.get("alttitle") or "",
                    vn.get("released") or "TBA",
                    vn.get("olang", ""),
                ),
                tags=("odd",) if index % 2 else (),
            )
        self.log(f"Found {len(results)} visual novels.", "good")
        self._update_status(
            f"Found {len(results)} visual novels. Double-click one to fetch its characters."
        )
        if results:
            first = self.vn_tree.get_children()[0]
            self.vn_tree.selection_set(first)
            self.vn_tree.focus(first)

    def _on_vn_selected(self, _event: tk.Event | None = None) -> None:
        selection = self.vn_tree.selection()
        if not selection:
            return
        self.selected_vn = self.vn_iids.get(selection[0])
        vn = self.selected_vn
        if not vn:
            return
        titles = vn.get("titles") or []
        main = next((t for t in titles if t.get("main")), None)
        languages = ", ".join(vn.get("languages") or [])
        self.vn_label.configure(
            text=f"Selected: {vn.get('title')}  [{vn.get('id')}]\n"
                 f"Original title: {(main or {}).get('title') or vn.get('alttitle') or '-'}\n"
                 f"Languages: {languages or '-'}"
        )

    def action_open_vndb(self) -> None:
        if not self.selected_vn:
            messagebox.showinfo("Nothing selected", "Select a visual novel in the list first.")
            return
        webbrowser.open(f"https://vndb.org/{self.selected_vn['id']}")

    def action_api_docs(self) -> None:
        webbrowser.open("https://api.vndb.org/kana")

    # -- building ----------------------------------------------------
    def _build_options(self) -> BuildOptions:
        def as_int(var: tk.IntVar, fallback: int) -> int:
            try:
                return int(var.get())
            except (ValueError, tk.TclError):
                return fallback

        return BuildOptions(
            include_aliases=bool(self.opt_aliases.get()),
            alias_min_len=as_int(self.opt_alias_min, 2),
            only_japanese_original=bool(self.opt_jp_only.get()),
            strip_spaces=bool(self.opt_strip.get()),
            prefill_dst=bool(self.opt_prefill.get()),
            include_traits=bool(self.opt_traits.get()),
            max_traits=as_int(self.opt_trait_max, 4),
            include_spoiler_info=bool(self.opt_spoiler.get()),
            include_reading=bool(self.opt_reading.get()),
        )

    def action_build(self, mode: str) -> None:
        if not self.selected_vn:
            messagebox.showinfo("Nothing selected", "Select a visual novel in the list first.")
            return
        vn = dict(self.selected_vn)
        options = self._build_options()
        self._build_mode = mode

        def work(report: Callable[[str], None]) -> tuple[str, Any]:
            client = self._client()
            report(f"Fetching characters for {vn['id']}\u2026")
            characters = client.vn_characters(
                vn["id"], progress=lambda n: report(f"Read {n} characters\u2026")
            )
            report("Building terms\u2026")
            terms, stats = core.terms_from_vn(vn, characters, options)
            return "build", (vn, terms, stats, mode, characters)

        self._run_async("Fetching characters", work)

    def _on_build_done(self, payload: tuple[dict[str, Any], list[Term], dict[str, int], str, list[dict]]) -> None:
        vn, terms, stats, mode, characters = payload
        if mode == "replace":
            self.glossary.replace(terms)
            self.log(f"Replaced the glossary with characters from {vn['title']}: {len(terms)} terms.",
                     "good")
        else:
            added, duplicates = self.glossary.add_many(terms)
            self.log(
                f"Appended {added} terms from {vn['title']} [{vn['id']}] "
                f"({duplicates} duplicates skipped, {stats['characters']} characters total).",
                "good",
            )
        self.glossary.dedupe()
        self._refresh_tree()
        self.action_validate(quiet=True)
        self._update_status()
        self.render_characters(vn, characters)

    # ------------------------------------------------------------------
    # glossary table
    # ------------------------------------------------------------------
    def _visible_indices(self) -> list[int]:
        needle = core.normalize_key(self.filter_var.get())
        only_untranslated = bool(self.only_untranslated_var.get())
        indices = []
        for index, term in enumerate(self.glossary):
            if not term.enabled:
                # Disabled rows only survive a plain, unfiltered view.
                if needle or only_untranslated:
                    continue
                indices.append(index)
                continue
            if only_untranslated and term.dst.strip():
                continue
            if needle:
                haystack = core.normalize_key(f"{term.src}{term.dst}{term.info}")
                if needle not in haystack:
                    continue
            indices.append(index)
        return indices

    def _refresh_tree(self) -> None:
        tree = self.glossary_tree.tree
        selected = set()
        for iid in tree.selection():
            try:
                selected.add(self.glossary[tree.index(iid)].src)
            except (tk.TclError, IndexError):
                pass
        top = tree.yview()[0] if tree.get_children() else 0.0

        tree.delete(*tree.get_children())
        issues_by_index: dict[int, str] = {}
        for issue in self.issues:
            if issue.index is None:
                continue
            if issues_by_index.get(issue.index) != ERROR:
                issues_by_index[issue.index] = issue.level

        seen_iids: set[str] = set()
        for position, index in enumerate(self._visible_indices()):
            term = self.glossary[index]
            iid = f"r{index}"
            if iid in seen_iids:
                continue
            seen_iids.add(iid)
            tags: list[str] = []
            level = issues_by_index.get(index)
            if level:
                tags.append(level)
            elif position % 2:
                tags.append("odd")
            if not term.enabled:
                tags.append("off")
            tree.insert(
                "", "end", iid=iid,
                values=(
                    ON_MARK if term.enabled else "",
                    term.src,
                    term.dst,
                    term.info,
                    term.note or "",
                ),
                tags=tuple(tags),
            )
            if term.src in selected:
                tree.selection_add(iid)
        if top:
            tree.yview_moveto(top)
        self.glossary_tree.update_sort_indicators()
        self.count_var.set(f"{len(self.glossary)} terms")
        self._update_status()

    def _on_cell_commit(self, iid: str, column: str, value: str) -> None:
        tree = self.glossary_tree.tree
        if iid not in tree.get_children(""):
            try:
                iid = tree.get_children()[int(iid[1:])]
            except (ValueError, IndexError, tk.TclError):
                return
        index = tree.index(iid)
        if index is None:
            return
        try:
            term = self.glossary[index]
        except IndexError:
            return

        if column in ("enabled", "on"):
            self.glossary.set_field(index, "enabled", not term.enabled)
        else:
            value = value.strip()
            if value == getattr(term, column):
                return
            self.glossary.set_field(index, column, value)
        self._refresh_tree()

    # -- row operations ----------------------------------------------
    def action_add_row(self, src: str) -> None:
        position = self.glossary.add(Term(src=src))
        if position is None:
            messagebox.showinfo("Duplicate", "That source term is already in the glossary.")
            return
        self._refresh_tree()
        tree = self.glossary_tree.tree
        for iid in tree.get_children():
            if tree.index(iid) == position:
                tree.selection_set(iid)
                tree.see(iid)
                break

    def action_delete_selected(self) -> None:
        tree = self.glossary_tree.tree
        indices = sorted((tree.index(iid) for iid in tree.selection()), reverse=True)
        if not indices:
            return
        for index in indices:
            self.glossary.remove(index)
        self.log(f"Deleted {len(indices)} terms.", "info")
        self._refresh_tree()

    def action_dedupe(self) -> None:
        removed = self.glossary.dedupe()
        self.log(f"Removed {removed} duplicate or empty source terms.", "info")
        self._refresh_tree()

    def action_set_enabled(self, enabled: bool) -> None:
        for term in self.glossary:
            term.enabled = enabled
        self._refresh_tree()

    def action_validate(self, quiet: bool = False) -> None:
        self.issues = self.glossary.validate()
        counts = {level: 0 for level in (ERROR, WARNING, INFO)}
        for issue in self.issues:
            counts[issue.level] = counts.get(issue.level, 0) + 1

        if self.issues or not quiet:
            self.clear_log()
            if not self.issues:
                self.log("Validation passed: no problems found.", "good")
            else:
                for issue in self.issues:
                    self.log(f"[{LEVEL_LABELS[issue.level]}] {issue.message}", issue.level)
                self.log(
                    f"-- {counts[ERROR]} error(s), {counts[WARNING]} warning(s), "
                    f"{counts[INFO]} note(s)",
                    "info",
                )
        message = (
            f"Validation: {counts[ERROR]} error(s), {counts[WARNING]} warning(s), "
            f"{counts[INFO]} note(s)"
        )
        self._refresh_tree()
        self._update_status(message)

    # ------------------------------------------------------------------
    # import / export
    # ------------------------------------------------------------------
    def action_import(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Import glossary",
            initialdir=self.settings["last_dir"],
            filetypes=[
                ("Glossary files", "*.json *.xlsx"),
                ("JSON", "*.json"),
                ("Excel", "*.xlsx"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        self._import_path(path, merge=False)

    def action_import_merge(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Merge glossary into current one",
            initialdir=self.settings["last_dir"],
            filetypes=[("Glossary files", "*.json *.xlsx"), ("All files", "*.*")],
        )
        if not path:
            return
        self._import_path(path, merge=True)

    def _import_path(self, path: str, merge: bool) -> None:
        try:
            terms, shape = core.load_glossary_file(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Import failed", str(exc))
            self.log(f"[Error] Could not import {Path(path).name}: {exc}", "error")
            return

        note = f"Detected the {core.SHAPE_LABELS.get(shape, shape)} format with {len(terms)} terms."
        if merge:
            added, updated, unchanged = self.glossary.merge_imported(terms)
            self.log(
                f"Merged {Path(path).name}: {added} added, {updated} updated, "
                f"{unchanged} unchanged. {note}",
                "good",
            )
        else:
            if self.glossary.stats()["total"] and not messagebox.askyesno(
                "Replace the current glossary?",
                f"The current glossary has {self.glossary.stats()['total']} terms.\n"
                "Importing will replace all of them. Continue?\n\n"
                "Use Merge Import instead to keep the existing terms.",
            ):
                return
            self.glossary.replace(terms)
            self.log(f"Imported {Path(path).name}: {len(terms)} terms. {note}", "good")
        self._update_status()
        self._refresh_tree()
        self.action_validate(quiet=True)

    def _export_terms(self) -> list[Term]:
        return self.glossary.exportable(require_dst=bool(self.skip_untranslated.get()))

    def _default_basename(self, suffix: str) -> str:
        stem = "glossary"
        if self.selected_vn:
            stem = core.sanitize_filename(
                f"{self.selected_vn.get('title') or self.selected_vn['id']}_glossary"
            )
        return f"{stem}.{suffix}"

    def _confirm_export(self, terms: list[Term]) -> bool:
        stats = self.glossary.stats()
        untranslated = sum(
            1 for t in self.glossary.enabled_terms() if t.src.strip() and not t.dst.strip()
        )
        disabled = stats["total"] - stats["enabled"]
        if not terms:
            messagebox.showwarning(
                "Nothing to export",
                "Every term is either disabled or still untranslated.\n"
                "To export empty translations anyway, untick "
                "\"Skip untranslated on export\".",
            )
            return False
        message = f"{len(terms)} terms will be exported."
        details = []
        if disabled:
            details.append(f"{disabled} disabled term(s) skipped")
        if untranslated and self.skip_untranslated.get():
            details.append(f"{untranslated} untranslated term(s) skipped")
        if details:
            message += "\n\n(" + "; ".join(details) + ")"
        if self.shape_var.get() == core.SHAPE_DICT:
            message += "\n\nNote: the key/value format cannot store the info column."
        return messagebox.askokcancel("Export glossary", message + "\n\nContinue?")

    def action_export_json(self) -> None:
        terms = self._export_terms()
        if not self._confirm_export(terms):
            return
        shape = self.shape_var.get()
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Export JSON glossary",
            initialdir=self.settings["last_dir"],
            initialfile=self._default_basename("json"),
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        try:
            core.write_json(path, terms, shape=shape)
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        self._after_export(Path(path), len(terms), core.SHAPE_LABELS.get(shape, shape))

    def action_export_xlsx(self) -> None:
        terms = self._export_terms()
        if not self._confirm_export(terms):
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Export XLSX glossary",
            initialdir=self.settings["last_dir"],
            initialfile=self._default_basename("xlsx"),
            defaultextension=".xlsx",
            filetypes=[("Excel workbook", "*.xlsx")],
        )
        if not path:
            return
        try:
            core.write_xlsx(path, terms)
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        self._after_export(Path(path), len(terms), "Excel workbook")

    def _after_export(self, path: Path, count: int, shape_label: str) -> None:
        self.settings["last_dir"] = str(path.parent)
        save_settings(self.settings)
        self.log(f"Exported {count} terms to {path} ({shape_label}).", "good")
        self._update_status(f"Exported {count} terms to {path.name}")
        messagebox.showinfo("Export complete", f"Exported {count} terms to:\n{path}")

    # ------------------------------------------------------------------
    # presets
    # ------------------------------------------------------------------
    def _lingua_gacha_root(self) -> Path:
        root = self.settings.get("lingua_gacha_root") or ""
        if root and Path(root).is_dir():
            return Path(root)
        chosen = filedialog.askdirectory(
            parent=self,
            title="Select the LinguaGacha folder (contains resource/preset/glossary)",
            initialdir=self.settings["last_dir"],
        )
        if chosen:
            self.settings["lingua_gacha_root"] = chosen
            save_settings(self.settings)
            return Path(chosen)
        return Path()

    def _preset_lang(self) -> str:
        current = self.settings.get("preset_lang") or core.DEFAULT_PRESET_LANG
        dialog = _PresetLanguageDialog(self, current)
        chosen = dialog.result
        if chosen:
            self.settings["preset_lang"] = chosen
            save_settings(self.settings)
        return chosen or current

    def action_save_preset(self) -> None:
        terms = self._export_terms()
        if not self._confirm_export(terms):
            return
        root = self._lingua_gacha_root()
        if not str(root):
            return
        lang = self._preset_lang()
        stem = self.selected_vn["id"] if self.selected_vn else "glossary"
        title = core.sanitize_filename((self.selected_vn or {}).get("title") or stem)
        name = f"{title}_{stem}"
        while True:
            dialog = _PresetNameDialog(self, name, core.preset_dir(root, lang), lang)
            name = dialog.result
            if not name:
                return
            try:
                target = core.save_preset(root, name, terms, lang=lang, overwrite=False)
            except FileExistsError as exc:
                if messagebox.askyesno("Preset already exists", f"{exc}\n\nOverwrite it?"):
                    try:
                        target = core.save_preset(root, name, terms, lang=lang, overwrite=True)
                    except OSError as err:
                        messagebox.showerror("Save failed", str(err))
                        return
                else:
                    continue
            except OSError as exc:
                messagebox.showerror("Save failed", str(exc))
                return
            break
        self.log(f"Saved preset to {target}", "good")
        messagebox.showinfo(
            "Preset saved",
            f"The preset was written to:\n{target}\n\n"
            "LinguaGacha will offer it as a preset for new projects.",
        )

    def action_load_preset(self) -> None:
        root = self._lingua_gacha_root()
        if not str(root):
            return
        lang = self._preset_lang()
        dialog = _PresetPickerDialog(self, root, lang)
        chosen = dialog.result
        if not chosen:
            return
        try:
            terms, shape = core.load_glossary_file(chosen)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Load failed", str(exc))
            return
        if self.glossary.stats()["total"] and not messagebox.askyesno(
            "Replace the current glossary?",
            "Loading a preset will replace the current glossary. Continue?\n\n"
            "Use Merge Import instead to keep the existing terms.",
        ):
            return
        self.glossary.replace(terms)
        self.log(
            f"Loaded preset {chosen.name}: {len(terms)} terms "
            f"({core.SHAPE_LABELS.get(shape, shape)}).",
            "good",
        )
        self._refresh_tree()
        self.action_validate(quiet=True)

    # ------------------------------------------------------------------
    # help / shutdown
    # ------------------------------------------------------------------
    def action_help(self) -> None:
        _HelpDialog(self)

    def _persist_ui_state(self) -> None:
        def as_int(var: tk.IntVar, fallback: int) -> int:
            try:
                return int(var.get())
            except (ValueError, tk.TclError):
                return fallback

        self.settings.update(
            {
                "include_aliases": bool(self.opt_aliases.get()),
                "alias_min_len": as_int(self.opt_alias_min, 2),
                "only_japanese_original": bool(self.opt_jp_only.get()),
                "strip_spaces": bool(self.opt_strip.get()),
                "prefill_dst": bool(self.opt_prefill.get()),
                "include_traits": bool(self.opt_traits.get()),
                "max_traits": as_int(self.opt_trait_max, 4),
                "include_spoiler_info": bool(self.opt_spoiler.get()),
                "include_reading": bool(self.opt_reading.get()),
                "export_shape": self.shape_var.get(),
                "skip_untranslated": bool(self.skip_untranslated.get()),
            }
        )
        save_settings(self.settings)

    def _on_close(self) -> None:
        self.glossary_tree.close_editor()
        if self.glossary.stats()["total"]:
            if not messagebox.askyesno(
                "Exit",
                f"The glossary has {self.glossary.stats()['total']} terms that have not "
                "been exported. Exit anyway?",
            ):
                return
        self._persist_ui_state()
        self._closed = True
        self.destroy()


# ==========================================================================
# small dialogs
# ==========================================================================


class _ModalDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str, geometry: str = "420x220") -> None:
        super().__init__(parent)
        self.title(title)
        self.geometry(geometry)
        self.transient(parent)
        self.resizable(False, False)
        self.result: Any = None
        self.grab_set()
        self.bind("<Escape>", lambda _e: self._dismiss())
        self.protocol("WM_DELETE_WINDOW", self._dismiss)

    def _dismiss(self) -> None:
        self.grab_release()
        self.destroy()


class _PresetLanguageDialog(_ModalDialog):
    def __init__(self, parent: tk.Misc, current: str) -> None:
        super().__init__(parent, "Preset language", "360x170")
        ttk.Label(
            self,
            text="Choose the LinguaGacha preset folder "
                 "(resource/preset/glossary/<language>):",
            wraplength=320,
        ).pack(anchor="w", padx=12, pady=(12, 6))
        self.var = tk.StringVar(value=current if current in PRESET_LANG_LABELS else "zh")
        box = ttk.Combobox(
            self, textvariable=self.var, state="readonly",
            values=[PRESET_LANG_LABELS[k] for k in core.PRESET_LANGS],
        )
        box.pack(fill="x", padx=12)
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=12, pady=12)
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self._dismiss).pack(side="right", padx=6)
        self.bind("<Return>", lambda _e: self._ok())

    def _ok(self) -> None:
        for key, label in PRESET_LANG_LABELS.items():
            if label == self.var.get():
                self.result = key
                break
        self._dismiss()


class _PresetNameDialog(_ModalDialog):
    def __init__(self, parent: tk.Misc, name: str, directory: Path, lang: str) -> None:
        super().__init__(parent, "Save as LinguaGacha preset", "540x230")
        ttk.Label(self, text=f"Location: {directory}", style="Dim.TLabel",
                  wraplength=490).pack(anchor="w", padx=12, pady=(10, 4))
        row = ttk.Frame(self)
        row.pack(fill="x", padx=12)
        ttk.Label(row, text="File name:").pack(side="left")
        self.var = tk.StringVar(value=name if name.lower().endswith(".json") else name + ".json")
        entry = ttk.Entry(row, textvariable=self.var)
        entry.pack(side="left", fill="x", expand=True)
        entry.select_range(0, "end")
        entry.focus_set()
        ttk.Label(
            self,
            text="An NN_ index prefix is added automatically, matching LinguaGacha's "
                 "preset naming convention.",
            style="Dim.TLabel",
            wraplength=490,
        ).pack(anchor="w", padx=12, pady=(6, 0))
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=12, pady=10)
        ttk.Button(buttons, text="Save", command=self._ok).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self._dismiss).pack(side="right", padx=6)
        self.bind("<Return>", lambda _e: self._ok())

    def _ok(self) -> None:
        self.result = self.var.get().strip()
        self._dismiss()


class _PresetPickerDialog(_ModalDialog):
    def __init__(self, parent: tk.Misc, root: Path, lang: str) -> None:
        super().__init__(parent, "Load LinguaGacha preset", "580x350")
        self.resizable(True, True)
        presets = core.list_presets(root, lang)
        ttk.Label(self, text=f"Folder: {core.preset_dir(root, lang)}",
                  style="Dim.TLabel").pack(anchor="w", padx=12, pady=(10, 4))

        if not presets:
            ttk.Label(self, text="There are no .json presets in that folder.").pack(
                anchor="w", padx=12, pady=8
            )

        listbox = tk.Listbox(self, height=10, exportselection=False)
        for preset in presets:
            listbox.insert("end", preset.name)
        scroll = ttk.Scrollbar(self, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scroll.set)
        listbox.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=(0, 10))
        scroll.pack(side="left", fill="y", padx=(0, 12), pady=(0, 10))
        listbox.bind("<Double-Button-1>", lambda _e: self._ok())
        if presets:
            listbox.selection_set(0)

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=12, pady=(0, 10))
        ttk.Button(buttons, text="Load", command=self._ok).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self._dismiss).pack(side="right", padx=6)
        ttk.Button(
            buttons, text="Open folder",
            command=lambda: webbrowser.open(core.preset_dir(root, lang).as_uri()),
        ).pack(side="left")

        self._presets = presets
        self._listbox = listbox

    def _ok(self) -> None:
        selection = self._listbox.curselection()
        if not selection:
            return
        self.result = self._presets[selection[0]]
        self._dismiss()


class _HelpDialog(_ModalDialog):
    TEXT = (
        "WORKFLOW\n"
        "1. Type a title (or a vndbid / vndb.org link) in the search box and click\n"
        "   \"Search VNs\".\n"
        "2. Select a visual novel and click \"Fetch Characters -> Append\". The tool\n"
        "   reads the full cast from VNDB, including unvoiced characters.\n"
        "3. Double-click cells to fill in the Translation column. The info column\n"
        "   carries the romanized reading, which is a useful hint for transliteration.\n"
        "4. \"Validate\" flags duplicate sources, missing translations, overly long\n"
        "   terms and other common mistakes.\n"
        "5. Export JSON / XLSX, or use \"Save Preset\" to drop the file straight into\n"
        "   LinguaGacha's preset folder.\n\n"
        "EXPORT FORMATS\n"
        '  Standard list   [{"src": ..., "dst": ..., "info": ...}, ...]   <- preferred\n'
        "  Key/value dict  {\"src\": \"dst\", ...}                            <- info is lost\n"
        "Import also accepts RPG Maker Actors.json and any .xlsx whose header row\n"
        "contains src / dst / info (Chinese header names are recognised too).\n\n"
        "PRESET FOLDER\n"
        "<LinguaGacha>/resource/preset/glossary/zh|en|user/\n"
        "An NN_ prefix is added automatically so presets sort correctly, and new\n"
        "projects can pick them straight from the preset list.\n\n"
        "DATA SOURCE\n"
        "VNDB.org Kana API (https://api.vndb.org/kana), free for non-commercial use.\n"
        "Characters come from POST /character with a nested vn filter rather than the\n"
        "vn va field, because va only lists voiced characters."
    )

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, "How to use", "640x570")
        text = tk.Text(self, wrap="word", font=("Consolas", 9), height=24)
        scroll = ttk.Scrollbar(self, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=(12, 10))
        scroll.pack(side="left", fill="y", padx=(0, 12), pady=(12, 10))
        text.insert("1.0", self.TEXT)
        text.configure(state="disabled")
        ttk.Button(self, text="Close", command=self._dismiss).pack(anchor="e", padx=12, pady=(0, 10))


# ==========================================================================
# entry point
# ==========================================================================


def main() -> int:
    app = App()
    try:
        app.mainloop()
    except KeyboardInterrupt:
        app._persist_ui_state()
    return 0


if __name__ == "__main__":
    sys.exit(main())