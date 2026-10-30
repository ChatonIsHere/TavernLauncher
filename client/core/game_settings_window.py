"""
Edits the game's own GameConfiguration.json directly, since with the
official launcher/main menu unavailable there's no other way to change
these. Field types and enum values below are taken directly from the
decompiled Alta.Utilities.GameSettings class, not guessed.
"""
import os
import json
import tkinter as tk
from tkinter import ttk, messagebox

from tavern_shared.theme import (
    BG, SURF, SURF2, BORDER, AMBER, AMBERDIM, PARCH, MUTED, GREEN, RED, CYAN,
    _btn, _section_label, _mk_scrollbar, _mk_combobox, _wheel_scroll, _wheel_scrolls,
)
from tavern_shared.window_chrome import _start_hidden, _finish_dark_window, _set_window_icon

# (field, kind, choices_or_range) -- kind is "bool", "enum", "int", "float", or "text".
# For "int"/"float", choices_or_range is either None or an inclusive (min, max).
FIELDS = [
    ("LogSettingsPath", "text", None),
    ("HasHapticsWhenToggleHolding", "bool", None),
    ("HapticsStrengthMultiplier", "float", None),
    ("IsUsingSmoothLocomotion", "bool", None),
    ("IsBendingBodyOnLocalBody", "bool", None),
    ("SmoothLocomotionActiveHand", "enum", ["Left", "Right"]),
    ("TouchLocomotionMode", "enum", ["Head", "Controller", "LockedHead", "LockedController"]),
    ("PressLocomotionMode", "enum", ["Head", "Controller", "LockedHead", "LockedController"]),
    ("SmoothLocomotionRunMode", "enum", ["Toggle", "Hold"]),
    ("IsInvertingSmoothWalkAndRun", "bool", None),
    ("SnapRotation", "enum", ["Disabled", "Snap", "Smooth"]),
    ("SmoothRotationSpeed", "enum", ["Slow", "Medium", "Fast", "SuperFast"]),
    ("SwipeSensitivityLevelValue", "enum", ["Low", "Medium", "High"]),
    ("MovementComfortStrength", "enum", ["Disabled", "Low", "Medium", "High"]),
    ("MovementComfortSpeed", "enum", ["Delayed", "Immediate"]),
    ("IsSmoothRotationOnSwipe", "enum", ["Centered", "Relative"]),
    ("SnapRotationMode", "enum", ["Swipe", "Press"]),
    ("SelfStepVolume", "float", None),
    ("CanSellSkillsWithChildren", "bool", None),
    ("ModsDirectoryPath", "text", None),
    ("CameraPhotoResolutionScale", "float", None),
    ("StreamingResolutionScale", "float", None),
    ("ViewFinderResolutionScale", "float", None),
    ("PlayerNamesToggle", "bool", None),
    ("IsUsingFreeGestureMode", "bool", None),
    ("DefaultCullingDistance", "float", None),
    ("ScreenshotDockHideOptions", "enum", ["Never", "WhenEmpty", "Always"]),
    ("RenderOneCameraFromOtherPlayers", "bool", None),
    ("HasAmbientOcclusionEnabled", "bool", None),
    ("AudioFXVolumeMultiplier", "float", (0.0, 2.0)),
    ("HasAccessibilityFeatures", "bool", None),
    ("HeightAssistValue", "int", None),
    ("UseGrabPositionMemory", "bool", None),
    ("AccessibleDocksAreToggle", "bool", None),
    ("HasGrabAssist", "bool", None),
    ("PixelLightCount", "int", (1, 8)),
]

# Exact values as originally specified -- what "Defaults" resets to.
DEFAULTS = {
    "LogSettingsPath": "",
    "HasHapticsWhenToggleHolding": True,
    "HapticsStrengthMultiplier": 1.0,
    "IsUsingSmoothLocomotion": True,
    "IsBendingBodyOnLocalBody": True,
    "SmoothLocomotionActiveHand": "Left",
    "TouchLocomotionMode": "Head",
    "PressLocomotionMode": "Head",
    "SmoothLocomotionRunMode": "Toggle",
    "IsInvertingSmoothWalkAndRun": False,
    "SnapRotation": "Snap",
    "SmoothRotationSpeed": "Medium",
    "SwipeSensitivityLevelValue": "Medium",
    "MovementComfortStrength": "Disabled",
    "MovementComfortSpeed": "Immediate",
    "IsSmoothRotationOnSwipe": "Centered",
    "SnapRotationMode": "Swipe",
    "SelfStepVolume": 1.0,
    "CanSellSkillsWithChildren": False,
    "ModsDirectoryPath": "",
    "CameraPhotoResolutionScale": 1.0,
    "StreamingResolutionScale": 1.0,
    "ViewFinderResolutionScale": 1.0,
    "PlayerNamesToggle": True,
    "IsUsingFreeGestureMode": True,
    "DefaultCullingDistance": 100.0,
    "ScreenshotDockHideOptions": "Never",
    "RenderOneCameraFromOtherPlayers": True,
    "HasAmbientOcclusionEnabled": False,
    "AudioFXVolumeMultiplier": 1.0,
    "HasAccessibilityFeatures": False,
    "HeightAssistValue": 2,
    "UseGrabPositionMemory": True,
    "AccessibleDocksAreToggle": True,
    "HasGrabAssist": True,
    "PixelLightCount": 5,
}

# Fields worth a specific one-line note beyond just their type.
FIELD_NOTES = {
    "PixelLightCount": "Valid range 1-8.",
    "AudioFXVolumeMultiplier": "Valid range 0.0-2.0.",
}


def find_game_config_path():
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "AppData", "Roaming", "A Township Tale", "GameConfiguration.json"),
        os.path.join(home, "AppData", "LocalLow", "Alta", "A Township Tale", "GameConfiguration.json"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


class GameSettingsWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        _start_hidden(self)
        self.title("Game Settings")
        self.configure(bg=BG)
        self.resizable(True, True)
        _set_window_icon(self)
        self._path = find_game_config_path()
        self._data = {}
        self._widgets = {}  # field -> (kind, tk var or Entry)
        self._build()
        _finish_dark_window(self)
        self.update_idletasks()
        self.geometry("560x700")

    def _build(self):
        h = tk.Frame(self, bg=SURF, height=44)
        h.pack(fill="x"); h.pack_propagate(False)
        tk.Label(h, text="🎮  Game Settings", bg=SURF, fg=AMBER,
                 font=("Georgia",12,"bold")).pack(side="left", padx=16, pady=8)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        tk.Label(self,
            text="Edits GameConfiguration.json directly -- the only way to change these "
                 "without the game's own main menu. Close the game completely before "
                 "editing, and before launching again after saving, or the game will "
                 "overwrite your changes on exit.",
            bg=BG, fg=MUTED, font=("Segoe UI",9), wraplength=520, justify="left"
        ).pack(anchor="w", padx=20, pady=(10,6))

        if self._path:
            self._load()

        outer = tk.Frame(self, bg=SURF, highlightbackground=BORDER, highlightthickness=1)
        outer.pack(fill="both", expand=True, padx=16, pady=(8,8))
        canvas = tk.Canvas(outer, bg=SURF, highlightthickness=0)
        sb = _mk_scrollbar(outer, canvas.yview)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.config(yscrollcommand=sb.set)
        self._form = tk.Frame(canvas, bg=SURF)
        canvas.create_window((0,0), window=self._form, anchor="nw")
        self._form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        self._canvas = canvas
        _wheel_scrolls(canvas, self._form)

        self._render_form()

        self._status = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status, bg=BG, fg=CYAN,
                 font=("Segoe UI",9), wraplength=520, justify="left"
        ).pack(anchor="w", padx=20, pady=(0,4))

        btn_row = tk.Frame(self, bg=BG)
        btn_row.pack(fill="x", padx=16, pady=(0,16))
        _btn(btn_row, "↺ Defaults", self._reset_to_defaults,
             font=("Segoe UI",9), pady=10).pack(side="left")
        _btn(btn_row, "💾 Save", self._save, "primary",
             font=("Georgia",10,"bold"), pady=10).pack(side="left", fill="x", expand=True, padx=(8,0))

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            if not isinstance(self._data, dict):
                self._data = {}
        except Exception as e:
            self._data = {}
            messagebox.showwarning("Couldn't read file",
                f"Starting from defaults instead:\n{e}", parent=self)

    def _render_form(self):
        for child in self._form.winfo_children():
            child.destroy()
        self._widgets = {}

        if not self._path:
            tk.Label(self._form,
                text="Couldn't find GameConfiguration.json automatically -- it's usually "
                     "in %AppData%\\Roaming\\A Township Tale\\ or %AppData%\\LocalLow\\"
                     "Alta\\A Township Tale\\. Make sure you've launched the game at "
                     "least once, then reopen this window.",
                bg=SURF, fg=MUTED, font=("Segoe UI",9), wraplength=480, justify="left"
            ).pack(anchor="w", padx=12, pady=16)
            return

        for field, kind, extra in FIELDS:
            row = tk.Frame(self._form, bg=SURF)
            row.pack(fill="x", padx=12, pady=4)
            tk.Label(row, text=field, bg=SURF, fg=PARCH,
                     font=("Segoe UI",9,"bold"), width=32, anchor="w").pack(side="left")

            current = self._data.get(field)

            if kind == "bool":
                var = tk.BooleanVar(value=bool(current) if current is not None else False)
                tk.Checkbutton(row, variable=var, bg=SURF, activebackground=SURF,
                               selectcolor=AMBERDIM, relief="flat",
                               highlightthickness=0, bd=0).pack(side="left")
                self._widgets[field] = ("bool", var)

            elif kind == "enum":
                var = tk.StringVar(value=current if current in extra else extra[0])
                combo = _mk_combobox(row, var, extra)
                # ttk.Combobox's own class binding for MouseWheel (cycles
                # the selected value) fires before the page's -- rebinding
                # directly on the widget intercepts it first, redirects
                # the scroll to the page instead, and "break" stops the
                # combobox's own handler from also firing.
                def _combo_wheel(event, row=row):
                    _wheel_scroll(event, start=row)
                    return "break"
                combo.bind("<MouseWheel>", _combo_wheel)
                self._widgets[field] = ("enum", var)

            elif kind in ("int", "float"):
                var = tk.StringVar(value=str(current) if current is not None else "")
                tk.Entry(row, textvariable=var, bg=BG, fg=PARCH, insertbackground=AMBER,
                         relief="flat", font=("Consolas",9), bd=4, width=14).pack(side="left")
                self._widgets[field] = (kind, var)

            else:  # text
                var = tk.StringVar(value=current if isinstance(current, str) else "")
                tk.Entry(row, textvariable=var, bg=BG, fg=PARCH, insertbackground=AMBER,
                         relief="flat", font=("Consolas",9), bd=4, width=28).pack(side="left")
                self._widgets[field] = ("text", var)

            note = FIELD_NOTES.get(field)
            if note:
                tk.Label(row, text=note, bg=SURF, fg=MUTED,
                         font=("Segoe UI",7), wraplength=180, justify="left").pack(side="left", padx=(8,0))

    def _reset_to_defaults(self):
        for field, (kind, var) in self._widgets.items():
            if field not in DEFAULTS:
                continue
            value = DEFAULTS[field]
            if kind == "bool":
                var.set(bool(value))
            else:
                var.set(str(value))
        self._status.set("Defaults loaded -- click Save to write them to the file.")

    def _save(self):
        if not self._path:
            messagebox.showerror("No file", "Locate GameConfiguration.json first.", parent=self)
            return

        for field, kind, extra in FIELDS:
            stored_kind, var = self._widgets.get(field, (None, None))
            if var is None:
                continue
            if kind == "bool":
                self._data[field] = bool(var.get())
            elif kind == "enum":
                self._data[field] = var.get()
            elif kind == "int":
                text = var.get().strip()
                try:
                    value = int(text)
                    if extra and not (extra[0] <= value <= extra[1]):
                        raise ValueError
                    self._data[field] = value
                except ValueError:
                    messagebox.showerror("Invalid value",
                        f"{field} needs a whole number" + (f" between {extra[0]} and {extra[1]}." if extra else "."),
                        parent=self)
                    return
            elif kind == "float":
                text = var.get().strip()
                try:
                    value = float(text)
                    if extra and not (extra[0] <= value <= extra[1]):
                        raise ValueError
                    self._data[field] = value
                except ValueError:
                    messagebox.showerror("Invalid value",
                        f"{field} needs a number" + (f" between {extra[0]} and {extra[1]}." if extra else "."),
                        parent=self)
                    return
            else:
                self._data[field] = var.get()

        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            self._status.set("Saved. Keep the game closed until you're done, or it may overwrite this.")
        except Exception as e:
            messagebox.showerror("Couldn't save", str(e), parent=self)
