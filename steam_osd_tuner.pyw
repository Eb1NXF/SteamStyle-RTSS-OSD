# -*- coding: utf-8 -*-
"""Steam-Style OSD Tuner for RTSS & MSI Afterburner.

Renders a compact, native-rendered Steam Performance Monitor look inside
MSI Afterburner's built-in ``[OSDLayout1]`` + RTSS ``[OSD]`` / ``[Font]`` engine
(zero OverlayEditor.dll sprite overhead, zero game restart required).

Features:
- Auto-discovers MSI Afterburner & RTSS via Windows Registry, running processes,
  environment variables, saved ``paths.json``, and standard install folders.
- Dynamically parses active ``ShowInOSD=1`` sensors and custom ``Group=`` labels
  from ``MSIAfterburner.cfg`` using official MAHM Source IDs.
- 1:1 RGBA visual preview with bright / dark / black scene contrast checks.
- Automatically creates a one-time initial backup before the first edit plus
  rolling backups (last 10 applies) inside ``backups/``.
- Bilingual UI (English / 繁體中文) with CLI ``--self-test`` and ``--render-assets``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import json
import locale
import mmap
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
import ctypes
from ctypes import wintypes

if os.name == "nt":
    import winreg
else:  # pragma: no cover - non-Windows typing fallback
    winreg = None  # type: ignore[assignment]


def _fatal_box(title: str, message: str) -> None:
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
            return
        except Exception:
            pass
    print(f"[{title}] {message}", file=sys.stderr)


try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - dependency guard
    _fatal_box(
        "SteamStyle RTSS OSD Tuner - Missing Dependency",
        "Pillow is required.\n\nPlease run:\n  pip install -r requirements.txt\n\nor launch via launch.bat.",
    )
    sys.exit(1)

try:
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, ttk
    from PIL import ImageTk
    HAS_TK = True
except ImportError:  # pragma: no cover - headless python environment
    tk = None  # type: ignore[assignment]
    colorchooser = filedialog = messagebox = ttk = ImageTk = None  # type: ignore[assignment]
    HAS_TK = False


APP_DIR = Path(__file__).resolve().parent
BACKUPS_ROOT = APP_DIR / "backups"
PATHS_CONFIG_FILE = APP_DIR / "paths.json"

DEFAULT_FACES = [
    "Segoe UI Variable Text Semibold",
    "Segoe UI Semibold",
    "Segoe UI",
    "Bahnschrift",
    "Consolas",
    "Unispace",
]

FONT_FILENAME_HINTS = {
    "Segoe UI Variable Text Semibold": ("SegUIVar.ttf", (600, "Text Semibold")),
    "Segoe UI Semibold": ("seguisb.ttf", None),
    "Segoe UI": ("segoeui.ttf", None),
    "Bahnschrift": ("bahnschrift.ttf", (600, None)),
    "Consolas": ("consola.ttf", None),
    "Unispace": ("unispace bd.ttf", None),
}

PRESETS = [
    ("mist", 45, "1E222B"),
    ("charcoal", 58, "181C24"),
    ("balanced", 69, "11141A"),
    ("obsidian", 80, "0A0C10"),
]

DEFAULT_COLORS = {
    "c0": "58C472",  # GPU / VRAM / MEM
    "c1": "D6D682",  # CPU
    "c2": "DE8080",  # API / FPS
    "c4": "BCC2CA",  # Primary values & RTSS BaseColor
    "c5": "B290DF",  # RAM
    "c6": "9096A0",  # Units
}

FIXED_A_PREFIX = "<C3=FF5555>"
FIXED_A_SUFFIX = (
    "<A0=-1><A1=2><S0=-50><S1={s1}><A2=3><A3=4><A4=-2><A5=-3>"
    "<A6=-4><A7=5><A8=-5><A9=-6><A10=5>"
)
GROUP_COLOR_TAG = (
    "<C0>;<C1>:80,90,A0,F1,F2,F3,F4,F5,F6,F7,FF,100;"
    "<C2>:50,51,52,53,54,55,56;<C5>:91,92,93"
)
GRAPH_COLOR_TAG = (
    "<C0>;<C1>:80,90,A0,F1,F2,F3,F4,F5,F6,F7,FF,100;"
    "<C2>:50,51,52,53,54,55,56"
)

SAMPLE_TEXT_FOR_SCALE = "D3D12 195 FPS  142 FPS  5.2 ms"

# MAHMSharedMemory.h sensor catalog (ordered by MSI Afterburner factory Monitoring table):
# src_name -> (src_id_int, default_group, color_role, sample_val, base_unit, temp_unit_aware)
SOURCE_CATALOG: dict[str, tuple[int, str, str, str, str, bool]] = {
    "GPU temperature": (0x00, "GPU", "c0", "64", " °C", True),
    "PCB temperature": (0x01, "PCB", "c0", "58", " °C", True),
    "Memory temperature": (0x02, "MEM", "c0", "68", " °C", True),
    "VRM temperature": (0x03, "VRM", "c0", "66", " °C", True),
    "GPU usage": (0x30, "GPU", "c0", "95", " %", False),
    "FB usage": (0x32, "GPU", "c0", "42", " %", False),
    "VID usage": (0x33, "GPU", "c0", "12", " %", False),
    "BUS usage": (0x34, "GPU", "c0", "18", " %", False),
    "Memory usage": (0x31, "MEM", "c0", "14292", " MB", False),
    r"Memory usage \ process": (0x35, "MEM", "c0", "11240", " MB", False),
    "Core clock": (0x20, "GPU", "c0", "2932", " MHz", False),
    "Shader clock": (0x21, "GPU", "c0", "2932", " MHz", False),
    "Memory clock": (0x22, "MEM", "c0", "16001", " MHz", False),
    "GPU voltage": (0x40, "GPU", "c0", "1.050", " V", False),
    "Aux voltage": (0x41, "GPU", "c0", "1.050", " V", False),
    "Memory voltage": (0x42, "GPU", "c0", "1.350", " V", False),
    "Aux2 voltage": (0x43, "GPU", "c0", "1.050", " V", False),
    "Power percent": (0x60, "GPU", "c0", "88", " %", False),
    "Power": (0x61, "GPU", "c0", "212.5", " W", False),
    "Fan speed": (0x10, "GPU", "c0", "45", " %", False),
    "Fan speed 2": (0x12, "GPU", "c0", "45", " %", False),
    "Fan speed 3": (0x14, "GPU", "c0", "45", " %", False),
    "Fan tachometer": (0x11, "GPU", "c0", "1450", " RPM", False),
    "Fan tachometer 2": (0x13, "GPU", "c0", "1450", " RPM", False),
    "Fan tachometer 3": (0x15, "GPU", "c0", "1450", " RPM", False),
    "Temp limit": (0x70, "LIM", "c0", "0", "", False),
    "Power limit": (0x71, "LIM", "c0", "0", "", False),
    "Voltage limit": (0x72, "LIM", "c0", "0", "", False),
    "OV max limit": (0x74, "LIM", "c0", "0", "", False),
    "No load limit": (0x74, "LIM", "c0", "0", "", False),
    "SLI sync limit": (0x75, "LIM", "c0", "0", "", False),
    "CPU temperature": (0x80, "CPU", "c1", "62", " °C", True),
    "CPU usage": (0x90, "CPU", "c1", "48", " %", False),
    "CPU clock": (0xA0, "CPU", "c1", "5330", " MHz", False),
    "CPU power": (0x100, "CPU", "c1", "138.4", " W", False),
    "RAM usage": (0x91, "RAM", "c5", "48384", " MB", False),
    r"RAM usage \ process": (0x93, "RAM", "c5", "12480", " MB", False),
    "Commit charge": (0x92, "RAM", "c5", "52100", " MB", False),
    "Pagefile usage": (0x92, "RAM", "c5", "52100", " MB", False),
    "Framerate": (0x50, "<APP>", "c2", "195", " FPS", False),
    "Frametime": (0x51, "<APP>", "c2", "5.2", " ms", False),
    "Framerate Min": (0x52, "<APP>", "c2", "110", " FPS", False),
    "Framerate Avg": (0x53, "<APP>", "c2", "165", " FPS", False),
    "Framerate Max": (0x54, "<APP>", "c2", "240", " FPS", False),
    "Framerate 1% Low": (0x55, "<APP>", "c2", "142", " FPS", False),
    "Framerate 0.1% Low": (0x56, "<APP>", "c2", "98", " FPS", False),
}
_CATALOG_INDEX: dict[str, int] = {name: i * 10 for i, name in enumerate(SOURCE_CATALOG)}

# Hand-tuned reference pixel widths (at Segoe UI Variable Text Semibold -18, S1=72)
# for the standard 5-group Steam bar layout so canonical setups stay pixel-exact.
CANONICAL_GROUP_BASE_WIDTHS = {
    ("50", "<APP>", ("Framerate", "Framerate 1% Low", "Frametime")): 206,
    ("50", "<APP>", ("Framerate", "Frametime", "Framerate 1% Low")): 206,
    ("80", "CPU", ("CPU temperature", "CPU usage", "CPU clock", "CPU power")): 254,
    ("0", "GPU", ("GPU temperature", "GPU usage", "Core clock", "Power")): 250,
    ("31", "VRAM", ("Memory usage", "Memory clock")): 202,
    ("91", "RAM", ("RAM usage",)): 116,
}

I18N: dict[str, dict[str, str]] = {
    "zh": {
        "title": "Steam 風格 RTSS / MSI Afterburner OSD 視覺調校器",
        "lang_btn": "English",
        "paths_frame": "安裝路徑與偵測狀態",
        "ab_dir": "Afterburner：",
        "rtss_dir": "RTSS：",
        "browse": "瀏覽...",
        "redetect": "重新偵測",
        "status_ready": "就緒",
        "status_missing": "找不到設定檔",
        "rtss_online": "RTSS 執行中（可熱重載）",
        "rtss_offline": "RTSS 未執行（套用時寫入檔案）",
        "ab_online": "AB 執行中",
        "ab_offline": "AB 未執行",
        "preview_frame": "即時 1:1 視覺預覽（左側／頂端即螢幕實機左上邊緣）",
        "scene_bright": "亮景預覽（選單／天空亮底）",
        "scene_dark": "暗景預覽（室內／夜戰暗底）",
        "scene_black": "純黑底對照",
        "bg_frame": "一、底色與透明度（<C7=AARRGGBB>）",
        "bg_alpha": "底色不透明度 (%)：",
        "bg_rgb": "底色 RGB 色碼：",
        "pick_bg": "挑選底色",
        "alpha_hint": "（0% 全透明 / 100% 不透明）",
        "preset_mist": "輕透薄霧 45%",
        "preset_charcoal": "偏淺炭灰 58%",
        "preset_balanced": "適中炭黑 69%",
        "preset_obsidian": "高對比濃黑 80%",
        "geom_frame": "二、位置與間距幾何",
        "flush_chk": "完全貼齊螢幕上緣與左緣 (0, 0)（Position=1,1 + <M=-1,-1>）",
        "float_xy": "未貼邊時左／上邊距：",
        "top_lbl": "px  /  上：",
        "intra_spaces": "組內空白（標題／數值）：",
        "intra_sep": "格  /  數值間：",
        "intra_unit": "格",
        "bar_h": "黑底高度 BarH (px)：",
        "pad_x": "左右內距 PadX (px)：",
        "gap_before": "分隔線前留白 (px)：",
        "gap_after": "分隔線後留白 (px)：",
        "div_alpha": "分隔線不透明度 (%)：",
        "order_lbl": "群組順序（點選後移動）：",
        "order_steam": "Steam 預設",
        "order_ab": "依小飛機順序",
        "font_frame": "三、字型與抗鋸齒（同步寫入啟用中的 RTSS 設定檔）",
        "face_lbl": "字體家族 Face：",
        "font_size": "主字高大小 Height：",
        "s1_pct": "單位比例 S1 (%)：",
        "outline": "黑框強度 Outline：",
        "color_frame": "四、標籤、數值與單位配色",
        "c2_lbl": "FPS / API 群組色 <C2>",
        "c1_lbl": "CPU 群組色 <C1>",
        "c0_lbl": "GPU / VRAM 群組色 <C0>",
        "c5_lbl": "RAM 群組色 <C5>",
        "c4_lbl": "數值主色 <C4> / BaseColor",
        "c6_lbl": "單位文字色 <C6>",
        "reset_colors": "重設為 Steam 預設文字配色",
        "tag_frame": "即時產出之 MSIAfterburner.cfg [OSDLayout1] 參數（唯讀對照）",
        "apply_btn": "立即套用（免重開遊戲）",
        "reload_btn": "重讀目前設定",
        "restore_btn": "還原初次套用前備份",
        "geom_flush": "貼齊 (0,0)",
        "geom_float": "懸浮 ({x},{y})",
        "geom_summary": "底條：{w}×{h} px｜{edge}｜字高：{cap} px｜{groups} 組",
        "status_loaded": "已載入設定與 {groups} 組感測群組（首次套用會先自動保存初始備份於 backups/initial/）。",
        "status_reloaded": "已重新讀取現行設定檔。",
        "status_applying": "套用中（備份 → 更新 RTSS 與 MSIAfterburner → 熱重載）...",
        "status_applied_live": "套用成功！已更新 {n} 份 RTSS 設定＋[OSDLayout1]｜OSDFrame={frame}｜備份：{bak}",
        "status_applied_offline": "已寫入 {n} 份 RTSS 設定＋[OSDLayout1]（{note}）｜備份：{bak}",
        "status_restored": "已從初始備份還原 {n} 份檔案。",
        "err_missing_paths": "請先選擇有效的 MSI Afterburner 與 RTSS 安裝資料夾。",
        "restore_confirm": "確定要還原到第一次套用前的原始備份嗎？\n將會覆蓋目前的 OSD 樣式與字型設定。",
        "restore_missing": "尚未找到初始備份（backups/initial/）。初次按「立即套用」時會自動建立。",
        "apply_fail_title": "套用失敗",
        "restore_fail_title": "還原失敗",
        "pick_bg_title": "選擇 OSD 背景 RGB 顏色",
        "pick_fg_title": "選擇顏色",
        "pick_ab_title": "選擇 MSI Afterburner 安裝資料夾",
        "pick_rtss_title": "選擇 RivaTuner Statistics Server (RTSS) 安裝資料夾",
        "invalid_dir_title": "無效資料夾",
        "invalid_ab_dir": "該資料夾中找不到 Profiles\\MSIAfterburner.cfg。",
        "invalid_rtss_dir": "該資料夾中找不到 Profiles\\Global。",
    },
    "en": {
        "title": "Steam-Style OSD Tuner for RTSS & MSI Afterburner",
        "lang_btn": "繁體中文",
        "paths_frame": "Detected Paths & Runtime Status",
        "ab_dir": "Afterburner:",
        "rtss_dir": "RTSS:",
        "browse": "Browse...",
        "redetect": "Auto-Detect",
        "status_ready": "Ready",
        "status_missing": "Config Missing",
        "rtss_online": "RTSS Running (Hot-Reload Ready)",
        "rtss_offline": "RTSS Offline (File Write Only)",
        "ab_online": "AB Running",
        "ab_offline": "AB Offline",
        "preview_frame": "Live 1:1 RGBA Preview (Top & Left edges correspond to screen (0, 0))",
        "scene_bright": "Bright Scene (Menu / Sky)",
        "scene_dark": "Dark Scene (Indoor / Night)",
        "scene_black": "Pure Black Reference",
        "bg_frame": "1. Background Color & Opacity (<C7=AARRGGBB>)",
        "bg_alpha": "Background Opacity (%):",
        "bg_rgb": "Background RGB Hex:",
        "pick_bg": "Pick Color",
        "alpha_hint": "(0% Transparent / 100% Solid)",
        "preset_mist": "Mist 45%",
        "preset_charcoal": "Slate 58%",
        "preset_balanced": "Steam Dark 69%",
        "preset_obsidian": "Deep Black 80%",
        "geom_frame": "2. Position & Spacing Geometry",
        "flush_chk": "Dock flush to top-left screen edge (0, 0) (Position=1,1 + <M=-1,-1>)",
        "float_xy": "Offset when undocked X / Y:",
        "top_lbl": "px  /  Y:",
        "intra_spaces": "Label / Data Spaces:",
        "intra_sep": "sp  /  Data:",
        "intra_unit": "sp",
        "bar_h": "Bar Height (px):",
        "pad_x": "Horizontal Padding (px):",
        "gap_before": "Gap Before Divider (px):",
        "gap_after": "Gap After Divider (px):",
        "div_alpha": "Divider Opacity (%):",
        "order_lbl": "Group Order (click & move):",
        "order_steam": "Steam Order",
        "order_ab": "AB List Order",
        "font_frame": "3. Typography & Anti-Aliasing (Synced to active RTSS profiles)",
        "face_lbl": "Font Face:",
        "font_size": "Main Font Height:",
        "s1_pct": "Unit Scale S1 (%):",
        "outline": "Outline Strength:",
        "color_frame": "4. Labels, Values & Units Palette",
        "c2_lbl": "FPS / API Group <C2>",
        "c1_lbl": "CPU Group <C1>",
        "c0_lbl": "GPU / VRAM Group <C0>",
        "c5_lbl": "RAM Group <C5>",
        "c4_lbl": "Value Text <C4> / BaseColor",
        "c6_lbl": "Unit Text <C6>",
        "reset_colors": "Reset to Steam Default Palette",
        "tag_frame": "Generated MSIAfterburner.cfg [OSDLayout1] Tags (Read-Only)",
        "apply_btn": "Apply Live (No Game Restart)",
        "reload_btn": "Reload Current",
        "restore_btn": "Restore Initial Backup",
        "geom_flush": "Flush (0,0)",
        "geom_float": "Floating ({x},{y})",
        "geom_summary": "Bar: {w}x{h} px | {edge} | Cap: {cap} px | {groups} groups",
        "status_loaded": "Loaded config with {groups} active sensor groups (first Apply auto-saves backups/initial/).",
        "status_reloaded": "Reloaded settings from disk.",
        "status_applying": "Applying (backup -> RTSS & MSIAfterburner -> hot-reload)...",
        "status_applied_live": "Applied! Updated {n} RTSS profile(s) + [OSDLayout1] | OSDFrame={frame} | Backup: {bak}",
        "status_applied_offline": "Saved {n} RTSS profile(s) + [OSDLayout1] ({note}) | Backup: {bak}",
        "status_restored": "Restored {n} file(s) from initial backup.",
        "err_missing_paths": "Please select valid MSI Afterburner and RTSS installation folders first.",
        "restore_confirm": "Restore original settings from before the first Apply?\nThis overwrites current OSD layout and font settings.",
        "restore_missing": "No initial backup found yet (backups/initial/). It is created on your first Apply.",
        "apply_fail_title": "Apply Failed",
        "restore_fail_title": "Restore Failed",
        "pick_bg_title": "Choose OSD Background RGB",
        "pick_fg_title": "Choose Color",
        "pick_ab_title": "Select MSI Afterburner Installation Folder",
        "pick_rtss_title": "Select RivaTuner Statistics Server (RTSS) Installation Folder",
        "invalid_dir_title": "Invalid Folder",
        "invalid_ab_dir": "Could not find Profiles\\MSIAfterburner.cfg in the selected folder.",
        "invalid_rtss_dir": "Could not find Profiles\\Global in the selected folder.",
    },
}


def detect_default_lang() -> str:
    if os.name == "nt":
        try:
            lang_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
            if (lang_id & 0xFF) == 0x04 and lang_id in (0x0404, 0x0C04, 0x1004, 0x1404, 0x7C04):
                return "zh"
            if (lang_id & 0xFF) == 0x04:
                return "zh"
        except Exception:
            pass
    return "en"


def _decode_bytes_safe(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    enc = locale.getpreferredencoding(False) or "utf-8"
    for candidate in ("utf-8", enc, "latin1"):
        try:
            return data.decode(candidate)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def _run_silent(cmd: list[str], timeout_s: float = 8.0) -> tuple[int, str, str]:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.run(
        cmd,
        creationflags=flags,
        capture_output=True,
        timeout=timeout_s,
    )
    return int(proc.returncode), _decode_bytes_safe(proc.stdout), _decode_bytes_safe(proc.stderr)


@dataclass
class InstallPaths:
    ab_dir: Path | None = None
    rtss_dir: Path | None = None

    @property
    def ab_exe(self) -> Path | None:
        return (self.ab_dir / "MSIAfterburner.exe") if self.ab_dir else None

    @property
    def ab_cfg(self) -> Path | None:
        return (self.ab_dir / "Profiles" / "MSIAfterburner.cfg") if self.ab_dir else None

    @property
    def rtss_exe(self) -> Path | None:
        return (self.rtss_dir / "RTSS.exe") if self.rtss_dir else None

    @property
    def rtss_profiles_dir(self) -> Path | None:
        return (self.rtss_dir / "Profiles") if self.rtss_dir else None

    @property
    def rtss_hooks_dll(self) -> Path | None:
        return (self.rtss_dir / "RTSSHooks64.dll") if self.rtss_dir else None

    def is_valid_ab(self) -> bool:
        return bool(self.ab_cfg and self.ab_cfg.is_file())

    def is_valid_rtss(self) -> bool:
        return bool(self.rtss_profiles_dir and (self.rtss_profiles_dir / "Global").is_file())


def _extract_dir_from_reg_value(raw: str) -> Path | None:
    if not raw:
        return None
    s = raw.strip().strip('"').split(",")[0].strip().strip('"')
    p = Path(s)
    if p.suffix.lower() in (".exe", ".ico"):
        return p.parent
    return p


def _query_registry_dirs(subkeys: list[str], value_names: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    if winreg is None:
        return found
    for view_flag in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for subkey in subkeys:
                try:
                    with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view_flag) as hkey:
                        for val_name in value_names:
                            try:
                                val, _ = winreg.QueryValueEx(hkey, val_name)
                                if isinstance(val, str):
                                    d = _extract_dir_from_reg_value(val)
                                    if d and d not in found:
                                        found.append(d)
                            except OSError:
                                pass
                except OSError:
                    pass
    return found


def _query_running_process_dirs(target_exe_names: set[str]) -> dict[str, Path]:
    """Find directories of running processes via Win32 Toolhelp32 + QueryFullProcessImageNameW."""
    result: dict[str, Path] = {}
    if os.name != "nt":
        return result

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return result
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            exe_name = pe.szExeFile.lower()
            if exe_name in target_exe_names:
                hproc = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pe.th32ProcessID)
                if hproc:
                    try:
                        buf = ctypes.create_unicode_buffer(1024)
                        size = wintypes.DWORD(len(buf))
                        if k32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                            p = Path(buf.value)
                            result[exe_name] = p.parent
                    finally:
                        k32.CloseHandle(hproc)
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    return result


def is_process_running(exe_name: str) -> bool:
    return exe_name.lower() in _query_running_process_dirs({exe_name.lower()})


def load_saved_paths(paths_file: Path = PATHS_CONFIG_FILE) -> dict[str, str]:
    if not paths_file.is_file():
        return {}
    try:
        data = json.loads(paths_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: str(v) for k, v in data.items() if isinstance(v, str)}
    except Exception:
        pass
    return {}


def save_custom_paths(paths: InstallPaths, paths_file: Path = PATHS_CONFIG_FILE) -> None:
    payload = {}
    if paths.ab_dir:
        payload["ab_dir"] = str(paths.ab_dir)
    if paths.rtss_dir:
        payload["rtss_dir"] = str(paths.rtss_dir)
    paths_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def discover_install_paths(
    custom_ab_dir: str | Path | None = None,
    custom_rtss_dir: str | Path | None = None,
    paths_file: Path = PATHS_CONFIG_FILE,
) -> InstallPaths:
    saved = load_saved_paths(paths_file)
    proc_dirs = _query_running_process_dirs({"msiafterburner.exe", "rtss.exe"})

    std_roots = [
        Path(p)
        for p in (
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("ProgramFiles"),
            os.environ.get("LOCALAPPDATA"),
        )
        if p
    ]

    ab_candidates: list[Path] = []
    rtss_candidates: list[Path] = []

    if custom_ab_dir:
        ab_candidates.append(Path(custom_ab_dir))
    if os.environ.get("STEAM_OSD_AB_DIR"):
        ab_candidates.append(Path(os.environ["STEAM_OSD_AB_DIR"]))
    if saved.get("ab_dir"):
        ab_candidates.append(Path(saved["ab_dir"]))
    if "msiafterburner.exe" in proc_dirs:
        ab_candidates.append(proc_dirs["msiafterburner.exe"])
    ab_candidates.extend(
        _query_registry_dirs(
            [
                r"SOFTWARE\MSI\Afterburner",
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Afterburner",
            ],
            ("InstallDir", "InstallPath", "InstallLocation", "DisplayIcon", "UninstallString"),
        )
    )
    for r in std_roots:
        ab_candidates.append(r / "MSI Afterburner")

    if custom_rtss_dir:
        rtss_candidates.append(Path(custom_rtss_dir))
    if os.environ.get("STEAM_OSD_RTSS_DIR"):
        rtss_candidates.append(Path(os.environ["STEAM_OSD_RTSS_DIR"]))
    if saved.get("rtss_dir"):
        rtss_candidates.append(Path(saved["rtss_dir"]))
    if "rtss.exe" in proc_dirs:
        rtss_candidates.append(proc_dirs["rtss.exe"])
    rtss_candidates.extend(
        _query_registry_dirs(
            [
                r"SOFTWARE\Unwinder\RTSS",
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\RTSS",
            ],
            ("InstallDir", "InstallPath", "InstallLocation", "DisplayIcon", "UninstallString"),
        )
    )
    for r in std_roots:
        rtss_candidates.append(r / "RivaTuner Statistics Server")

    ab_dir = next((d for d in ab_candidates if (d / "Profiles" / "MSIAfterburner.cfg").is_file()), None)
    rtss_dir = next((d for d in rtss_candidates if (d / "Profiles" / "Global").is_file()), None)
    return InstallPaths(ab_dir=ab_dir, rtss_dir=rtss_dir)


def _split_ini_lines(raw_bytes: bytes) -> list[str]:
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raw_bytes = raw_bytes[3:]
    text = raw_bytes.decode("latin1")
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _parse_section(lines: list[str], section_name: str) -> dict[str, str]:
    sec = ""
    res: dict[str, str] = {}
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]") and len(s) >= 2:
            sec = s[1:-1].strip()
            continue
        if sec == section_name and "=" in line:
            k, v = line.split("=", 1)
            res[k.strip()] = v.strip()
    return res


def _upsert_ini_section_keys(
    lines: list[str],
    section_name: str,
    updates: dict[str, str],
    *,
    create_if_missing: bool = False,
) -> list[str]:
    out = list(lines)
    sec_start: int | None = None
    sec_end = len(out)
    for idx, line in enumerate(out):
        s = line.strip()
        if s.startswith("[") and s.endswith("]") and len(s) >= 2:
            name = s[1:-1].strip()
            if sec_start is not None:
                sec_end = idx
                break
            if name == section_name:
                sec_start = idx

    if sec_start is None:
        if not create_if_missing:
            return out
        # Trim trailing empty strings, append clean [section_name], keep final trailing empty line if present
        had_trailing_empty = len(out) > 0 and out[-1] == ""
        while out and out[-1] == "":
            out.pop()
        if out:
            out.append("")
        out.append(f"[{section_name}]")
        for k, v in updates.items():
            out.append(f"{k}={v}")
        if had_trailing_empty:
            out.append("")
        return out

    # Insert missing keys before any trailing blank lines at the end of the target section
    insert_at = sec_end
    while insert_at > sec_start + 1 and out[insert_at - 1] == "":
        insert_at -= 1

    seen: set[str] = set()
    for i in range(sec_start + 1, sec_end):
        if "=" in out[i]:
            k = out[i].split("=", 1)[0].strip()
            if k in updates and k not in seen:
                out[i] = f"{k}={updates[k]}"
                seen.add(k)

    missing = [f"{k}={v}" for k, v in updates.items() if k not in seen]
    if missing:
        out = out[:insert_at] + missing + out[insert_at:]
    return out


class TEXTMETRICW(ctypes.Structure):
    _fields_ = [
        ("tmHeight", wintypes.LONG),
        ("tmAscent", wintypes.LONG),
        ("tmDescent", wintypes.LONG),
        ("tmInternalLeading", wintypes.LONG),
        ("tmExternalLeading", wintypes.LONG),
        ("tmAveCharWidth", wintypes.LONG),
        ("tmMaxCharWidth", wintypes.LONG),
        ("tmWeight", wintypes.LONG),
        ("tmOverhang", wintypes.LONG),
        ("tmDigitizedAspectX", wintypes.LONG),
        ("tmDigitizedAspectY", wintypes.LONG),
        ("tmFirstChar", wintypes.WCHAR),
        ("tmLastChar", wintypes.WCHAR),
        ("tmDefaultChar", wintypes.WCHAR),
        ("tmBreakChar", wintypes.WCHAR),
        ("tmItalic", wintypes.BYTE),
        ("tmUnderlined", wintypes.BYTE),
        ("tmStruckOut", wintypes.BYTE),
        ("tmPitchAndFamily", wintypes.BYTE),
        ("tmCharSet", wintypes.BYTE),
    ]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


def _measure_gdi_single(face: str, size: int) -> tuple[int, int, int]:
    gdi32 = ctypes.windll.gdi32
    hdc = gdi32.CreateCompatibleDC(0)
    if not hdc:
        raise RuntimeError("CreateCompatibleDC failed")
    try:
        hf = gdi32.CreateFontW(-size, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 4, 0, face)
        old = gdi32.SelectObject(hdc, hf)
        tm = TEXTMETRICW()
        gdi32.GetTextMetricsW(hdc, ctypes.byref(tm))
        sz = SIZE()
        gdi32.GetTextExtentPoint32W(hdc, SAMPLE_TEXT_FOR_SCALE, len(SAMPLE_TEXT_FOR_SCALE), ctypes.byref(sz))
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(hf)
        return int(tm.tmAscent), int(tm.tmInternalLeading), int(sz.cx)
    finally:
        gdi32.DeleteDC(hdc)


def _measure_gdi_text_width(face: str, size: int, text: str) -> int:
    if not text:
        return 0
    if os.name == "nt":
        try:
            gdi32 = ctypes.windll.gdi32
            hdc = gdi32.CreateCompatibleDC(0)
            if hdc:
                try:
                    hf = gdi32.CreateFontW(-size, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 4, 0, face)
                    old = gdi32.SelectObject(hdc, hf)
                    sz = SIZE()
                    gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(sz))
                    gdi32.SelectObject(hdc, old)
                    gdi32.DeleteObject(hf)
                    return int(sz.cx)
                finally:
                    gdi32.DeleteDC(hdc)
        except Exception:
            pass
    return max(4, round(len(text) * size * 0.52))


CANONICAL_REF_SAMPLE_WIDTH_18 = 255
_REF_WIDTH_CACHE: dict[str, int] = {}


def measure_gdi_metrics(face: str, size: int) -> tuple[int, int, float]:
    """Return (cap_h, internal_leading, scale_ratio)."""
    try:
        if "ref" not in _REF_WIDTH_CACHE:
            if _find_font_file("Segoe UI Variable Text Semibold")[0] is not None:
                _, _, ref_w = _measure_gdi_single("Segoe UI Variable Text Semibold", 18)
                _REF_WIDTH_CACHE["ref"] = max(1, ref_w)
            else:
                _REF_WIDTH_CACHE["ref"] = CANONICAL_REF_SAMPLE_WIDTH_18
        ascent, internal_leading, sample_w = _measure_gdi_single(face, size)
        cap_h = max(8, ascent - internal_leading)
        scale = max(0.5, sample_w / _REF_WIDTH_CACHE["ref"])
        return cap_h, max(0, internal_leading), scale
    except Exception:
        scale = size / 18.0
        return max(8, round(13 * scale)), max(1, round(6 * scale)), scale


def _find_font_file(face: str) -> tuple[Path | None, tuple[int, str | None] | None]:
    hint = FONT_FILENAME_HINTS.get(face)
    search_dirs: list[Path] = []
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    search_dirs.append(Path(windir) / "Fonts")
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        search_dirs.append(Path(local_app) / "Microsoft" / "Windows" / "Fonts")

    if hint:
        fname, var_cfg = hint
        for d in search_dirs:
            cand = d / fname
            if cand.is_file():
                return cand, var_cfg

    if winreg is not None:
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as hk:
                    idx = 0
                    while True:
                        try:
                            name, val, _ = winreg.EnumValue(hk, idx)
                            idx += 1
                            if isinstance(val, str) and face.lower() in name.lower():
                                vp = Path(val)
                                if not vp.is_absolute():
                                    for d in search_dirs:
                                        if (d / vp).is_file():
                                            return d / vp, None
                                elif vp.is_file():
                                    return vp, None
                        except OSError:
                            break
            except OSError:
                pass
    return None, None


def get_available_faces() -> list[str]:
    installed: list[str] = []
    for f in DEFAULT_FACES:
        p, _ = _find_font_file(f)
        if p is not None:
            installed.append(f)
    return installed or list(DEFAULT_FACES[1:])


def default_face_for_os() -> str:
    avail = get_available_faces()
    if "Segoe UI Variable Text Semibold" in avail:
        return "Segoe UI Variable Text Semibold"
    return avail[0] if avail else "Segoe UI Semibold"


@dataclass(frozen=True)
class SensorSample:
    src_name: str
    src_id: int
    id_hex: str
    val: str
    unit: str


@dataclass(frozen=True)
class OsdGroupSpec:
    id_hex: str
    label_raw: str
    preview_label: str
    color_key: str
    sensors: tuple[SensorSample, ...]
    order_tier: int
    appearance_idx: int


def canonical_groups() -> list[OsdGroupSpec]:
    return [
        OsdGroupSpec(
            id_hex="50",
            label_raw="<APP>",
            preview_label="D3D12",
            color_key="c2",
            sensors=(
                SensorSample("Framerate", 0x50, "50", "195", " FPS"),
                SensorSample("Framerate 1% Low", 0x55, "55", "142", " FPS"),
                SensorSample("Frametime", 0x51, "51", "5.2", " ms"),
            ),
            order_tier=0,
            appearance_idx=0,
        ),
        OsdGroupSpec(
            id_hex="80",
            label_raw="CPU",
            preview_label="CPU",
            color_key="c1",
            sensors=(
                SensorSample("CPU temperature", 0x80, "80", "62", " °C"),
                SensorSample("CPU usage", 0x90, "90", "48", " %"),
                SensorSample("CPU clock", 0xA0, "A0", "5330", " MHz"),
                SensorSample("CPU power", 0x100, "100", "138.4", " W"),
            ),
            order_tier=1,
            appearance_idx=1,
        ),
        OsdGroupSpec(
            id_hex="0",
            label_raw="GPU",
            preview_label="GPU",
            color_key="c0",
            sensors=(
                SensorSample("GPU temperature", 0x00, "0", "64", " °C"),
                SensorSample("GPU usage", 0x30, "30", "95", " %"),
                SensorSample("Core clock", 0x20, "20", "2932", " MHz"),
                SensorSample("Power", 0x61, "61", "212.5", " W"),
            ),
            order_tier=2,
            appearance_idx=2,
        ),
        OsdGroupSpec(
            id_hex="31",
            label_raw="VRAM",
            preview_label="VRAM",
            color_key="c0",
            sensors=(
                SensorSample("Memory usage", 0x31, "31", "14292", " MB"),
                SensorSample("Memory clock", 0x22, "22", "16001", " MHz"),
            ),
            order_tier=3,
            appearance_idx=3,
        ),
        OsdGroupSpec(
            id_hex="91",
            label_raw="RAM",
            preview_label="RAM",
            color_key="c5",
            sensors=(
                SensorSample("RAM usage", 0x91, "91", "48384", " MB"),
            ),
            order_tier=4,
            appearance_idx=4,
        ),
    ]


def _classify_source(src_name: str, fahrenheit: bool) -> tuple[int, str, str, str, str]:
    meta = SOURCE_CATALOG.get(src_name)
    if meta:
        sid, grp, ck, val, unit, temp_aware = meta
        if temp_aware and fahrenheit:
            unit = " °F"
        return sid, grp, ck, val, unit

    # Dynamic pattern rules for multi-core CPU / multi-GPU sources
    m_cpu = re.match(r"^CPU(\d+)\s+(temperature|usage|clock)$", src_name, re.I)
    if m_cpu:
        idx_s, kind = m_cpu.group(1), m_cpu.group(2).lower()
        grp = f"CPU{idx_s}"
        if kind == "temperature":
            return 0x80, grp, "c1", "62", (" °F" if fahrenheit else " °C")
        if kind == "usage":
            return 0x90, grp, "c1", "48", " %"
        return 0xA0, grp, "c1", "5330", " MHz"

    m_gpu = re.match(r"^GPU(\d+)\s+(.+)$", src_name, re.I)
    if m_gpu:
        idx_n, rest = int(m_gpu.group(1)), m_gpu.group(2).lower()
        base_map = {
            "temperature": (0xB0, "c0", "64", " °F" if fahrenheit else " °C", "GPU"),
            "usage": (0x30, "c0", "95", " %", "GPU"),
            "core clock": (0x20, "c0", "2932", " MHz", "GPU"),
            "memory usage": (0x31, "c0", "14292", " MB", "MEM"),
            "memory clock": (0x22, "c0", "16001", " MHz", "MEM"),
            "power": (0x61, "c0", "212.5", " W", "GPU"),
        }
        if rest in base_map:
            sid, ck, val, unit, gbase = base_map[rest]
            return sid, f"{gbase}{idx_n}", ck, val, unit

    return 0xFF, "MISC", "c1", "100", ""


def _group_tier(src_id: int, label_raw: str) -> int:
    up = label_raw.upper()
    if up in ("<APP>", "FPS") or src_id in (0x50, 0x51, 0x52, 0x53, 0x54, 0x55, 0x56):
        return 0
    if up == "CPU":
        return 1
    if up == "GPU":
        return 2
    if up in ("VRAM", "MEM"):
        return 3
    if up == "RAM":
        return 4
    return 5


def _default_monitoring_rank(src_name: str, file_idx: int) -> tuple[int, int, int]:
    """Return canonical MSI Afterburner Monitoring order key when Sources= is absent."""
    if src_name in _CATALOG_INDEX:
        return (0, _CATALOG_INDEX[src_name], file_idx)
    m_cpu = re.match(r"^CPU(\d+)\s+(temperature|usage|clock)$", src_name, re.I)
    if m_cpu:
        core_idx, kind = int(m_cpu.group(1)), m_cpu.group(2).lower()
        base = _CATALOG_INDEX.get(f"CPU {kind}", 310)
        return (0, base - 5, core_idx)
    m_gpu = re.match(r"^GPU(\d+)\s+(.+)$", src_name, re.I)
    if m_gpu:
        gpu_idx = int(m_gpu.group(1))
        return (0, 500 + gpu_idx * 20, file_idx)
    return (1, 0, file_idx)


def read_live_mahm_entries() -> list[tuple[str, int, str, int]]:
    """Read (src_name, src_id, src_units, flags) from live MAHMSharedMemory if available."""
    if os.name != "nt":
        return []
    try:
        hdr_map = mmap.mmap(-1, 32, tagname="MAHMSharedMemory", access=mmap.ACCESS_READ)
        hdr_raw = hdr_map[:]
        hdr_map.close()
        sig, _ver, hdr_sz, num_entries, entry_sz = struct.unpack("<4s4I", hdr_raw[:20])
        if sig != b"MHAM" or hdr_sz < 20 or entry_sz < 1324 or num_entries <= 0:
            return []
        total = hdr_sz + num_entries * entry_sz
        full_map = mmap.mmap(-1, total, tagname="MAHMSharedMemory", access=mmap.ACCESS_READ)
        buf = full_map[:]
        full_map.close()
    except Exception:
        return []

    entries: list[tuple[str, int, str, int]] = []
    for i in range(num_entries):
        off = hdr_sz + i * entry_sz
        chunk = buf[off : off + entry_sz]
        if len(chunk) < 1324:
            break
        src_name = chunk[0:260].split(b"\x00", 1)[0].decode("latin1")
        src_units = chunk[260:520].split(b"\x00", 1)[0].decode("latin1")
        _d, _mn, _mx, flags, _gpu, src_id = struct.unpack("<3f3I", chunk[1300:1324])
        if src_name:
            entries.append((src_name, int(src_id), src_units, int(flags)))
    return entries


def _is_live_ab_cfg(ab_cfg_path: Path) -> bool:
    proc_dirs = _query_running_process_dirs({"msiafterburner.exe"})
    live_dir = proc_dirs.get("msiafterburner.exe")
    if not live_dir:
        return False
    try:
        return os.path.normcase(str(ab_cfg_path.parent.parent.resolve())) == os.path.normcase(str(live_dir.resolve()))
    except OSError:
        return False


def extract_group_separator_order(gs: str) -> list[str]:
    """Return ordered uppercase group IDs sorted left-to-right by <P=x,y> X coordinate."""
    if not gs:
        return []
    entries: list[tuple[int, int, str]] = []
    for seg_idx, seg in enumerate(gs.split(";")):
        m_id = re.search(r":([0-9A-Fa-f]+)\s*$", seg)
        if not m_id:
            continue
        p_matches = re.findall(r"<P=(-?\d+),(-?\d+)>", seg)
        if not p_matches:
            continue
        entries.append((int(p_matches[-1][0]), seg_idx, m_id.group(1).upper()))
    if not entries:
        return []
    entries.sort(key=lambda item: (item[0], item[1]))
    seen: set[str] = set()
    ordered: list[str] = []
    for _, _, gid in entries:
        if gid not in seen:
            seen.add(gid)
            ordered.append(gid)
    return ordered


def order_groups(
    groups: list[OsdGroupSpec],
    mode: str = "steam",
    custom_order: list[str] | None = None,
) -> list[OsdGroupSpec]:
    """Order groups using 'steam', 'ab' (Monitoring list order), or 'custom' ID order."""
    if mode == "ab":
        return sorted(groups, key=lambda g: g.appearance_idx)
    if mode == "custom" and custom_order:
        pos_map = {cid.upper(): idx for idx, cid in enumerate(custom_order)}
        matched = [g for g in groups if g.id_hex.upper() in pos_map]
        unmatched = [g for g in groups if g.id_hex.upper() not in pos_map]
        matched.sort(key=lambda g: pos_map[g.id_hex.upper()])
        unmatched.sort(key=lambda g: (g.order_tier, g.appearance_idx))
        return matched + unmatched
    return sorted(groups, key=lambda g: (g.order_tier, g.appearance_idx))


def parse_ab_osd_groups(
    ab_cfg_path: Path | None,
    *,
    order_mode: str = "auto",
    custom_order: list[str] | None = None,
    allow_live_mahm: bool = False,
) -> tuple[list[OsdGroupSpec], bool]:
    """Parse active ShowInOSD=1 groups in MSIAfterburner.cfg, honoring custom or Steam order."""
    if not ab_cfg_path or not ab_cfg_path.is_file():
        specs = canonical_groups()
        if order_mode in ("ab", "custom"):
            specs = order_groups(specs, mode=order_mode, custom_order=custom_order)
        return specs, False

    lines = _split_ini_lines(ab_cfg_path.read_bytes())
    settings = _parse_section(lines, "Settings")
    fahrenheit = settings.get("Fahrenheit", "0").strip() == "1"

    sources_raw = settings.get("Sources", "").strip()
    sources_order: list[str] = []
    if sources_raw:
        for tok in sources_raw.split(","):
            tok = tok.strip()
            if not tok:
                continue
            sources_order.append(tok[1:] if tok[:1] in "+-" else tok)

    section_sources: dict[str, dict[str, str]] = {}
    file_source_order: list[str] = []
    cur_src: str | None = None
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]") and len(s) >= 2:
            sec = s[1:-1].strip()
            if sec.startswith("Source "):
                cur_src = sec[7:]
                section_sources[cur_src] = {}
                file_source_order.append(cur_src)
            else:
                cur_src = None
            continue
        if cur_src is not None and "=" in line:
            k, v = line.split("=", 1)
            section_sources[cur_src][k.strip()] = v.strip()

    mahm_entries: list[tuple[str, int, str, int]] = []
    if allow_live_mahm and _is_live_ab_cfg(ab_cfg_path):
        mahm_entries = read_live_mahm_entries()
    mahm_map = {name: (sid, unit) for name, sid, unit, _flags in mahm_entries}

    if sources_order:
        ordered_names = sources_order
    elif mahm_entries:
        seen_names: set[str] = set()
        ordered_names = []
        for m_name, _, _, _ in mahm_entries:
            if m_name in section_sources and m_name not in seen_names:
                seen_names.add(m_name)
                ordered_names.append(m_name)
        for f_name in file_source_order:
            if f_name not in seen_names:
                ordered_names.append(f_name)
    else:
        ordered_names = [
            name
            for _, name in sorted(
                enumerate(file_source_order),
                key=lambda pair: _default_monitoring_rank(pair[1], pair[0]),
            )
        ]

    buckets: dict[str, list[tuple[int, str, str, str, str, str]]] = {}
    first_seen_idx: dict[str, int] = {}
    group_display_raw: dict[str, str] = {}
    group_color_key: dict[str, str] = {}

    for idx, src_name in enumerate(ordered_names):
        props = section_sources.get(src_name)
        if not props or props.get("ShowInOSD", "0").strip() != "1":
            continue
        custom_group = props.get("Group", "").strip()
        sid, default_grp, ck, sample_val, unit = _classify_source(src_name, fahrenheit)
        if src_name not in SOURCE_CATALOG and sid == 0xFF and src_name in mahm_map:
            m_sid, m_unit = mahm_map[src_name]
            sid = m_sid
            if m_unit:
                unit = " " + m_unit
        grp_label = custom_group if custom_group else default_grp
        key = grp_label.lower()
        if key not in buckets:
            buckets[key] = []
            first_seen_idx[key] = idx
            group_display_raw[key] = grp_label
            group_color_key[key] = ck
        buckets[key].append((sid, src_name, sample_val, unit, grp_label, ck))

    if not buckets:
        return [], True

    specs: list[OsdGroupSpec] = []
    for key, items in buckets.items():
        first_sid = items[0][0]
        raw_lbl = group_display_raw[key]
        prev_lbl = "D3D12" if raw_lbl == "<APP>" else raw_lbl
        ck = group_color_key[key]
        sensors = tuple(
            SensorSample(src_name=it[1], src_id=it[0], id_hex=f"{it[0]:X}", val=it[2], unit=it[3])
            for it in items
        )
        tier = _group_tier(first_sid, raw_lbl)
        specs.append(
            OsdGroupSpec(
                id_hex=f"{first_sid:X}",
                label_raw=raw_lbl,
                preview_label=prev_lbl,
                color_key=ck,
                sensors=sensors,
                order_tier=tier,
                appearance_idx=first_seen_idx[key],
            )
        )

    if order_mode == "auto":
        layout1_sec = _parse_section(lines, "OSDLayout1")
        existing_order = extract_group_separator_order(layout1_sec.get("GroupSeparator", ""))
        if existing_order:
            return order_groups(specs, "custom", existing_order), True
        return order_groups(specs, "steam"), True
    return order_groups(specs, order_mode, custom_order), True


@dataclass
class OsdConfig:
    bg_alpha_pct: int = 69
    bg_rgb: str = "11141A"
    flush_edge: bool = True
    pos_x: int = 1
    pos_y: int = 1
    bar_h: int = 30
    pad_x: int = 15
    gap_before: int = 16
    gap_after: int = 12
    div_alpha_pct: int = 18
    name_spaces: int = 1
    data_spaces: int = 1
    face: str = field(default_factory=default_face_for_os)
    font_size: int = 18
    s1_pct: int = 72
    outline_factor: int = 0
    c0: str = "58C472"
    c1: str = "D6D682"
    c2: str = "DE8080"
    c4: str = "BCC2CA"
    c5: str = "B290DF"
    c6: str = "9096A0"


@dataclass
class ComputedLayout:
    rtss_pos_x: int
    rtss_pos_y: int
    c7_tag: str
    c8_tag: str
    bar_w: int
    bar_h: int
    cap_h: int
    text_py: int
    div_h: int
    div_py: int
    groups: list[OsdGroupSpec]
    p_groups: list[int]
    d_x: list[int]
    screen_groups: list[int]
    screen_divs: list[int]
    format_header: str
    prolog_separator: str
    group_separator: str
    group_name_separator: str
    group_data_separator: str


def pct_to_hex(pct: int) -> str:
    val = max(0, min(255, round(max(0, min(100, pct)) * 255 / 100)))
    return f"{val:02X}"


def hex_to_pct(two_hex: str) -> int:
    return max(0, min(100, round(int(two_hex, 16) * 100 / 255)))


def _measure_units_only_w(group: OsdGroupSpec, face: str, font_size: int, s1_pct: int) -> int:
    unit_size = max(8, round(font_size * s1_pct / 100))
    return sum(_measure_gdi_text_width(face, unit_size, s.unit) for s in group.sensors if s.unit)


def _measure_group_width(
    group: OsdGroupSpec,
    face: str,
    font_size: int,
    s1_pct: int,
    scale: float,
    name_spaces: int = 1,
    data_spaces: int = 1,
) -> int:
    ns = max(1, min(4, int(name_spaces)))
    ds = max(1, min(4, int(data_spaces)))
    sig = (group.id_hex, group.label_raw, tuple(s.src_name for s in group.sensors))
    base_canon = CANONICAL_GROUP_BASE_WIDTHS.get(sig)
    if base_canon is not None:
        w = round(base_canon * scale)
        if s1_pct != 72:
            w += _measure_units_only_w(group, face, font_size, s1_pct) - _measure_units_only_w(group, face, font_size, 72)
        extra_spaces = (ns - 1) + (ds - 1) * max(0, len(group.sensors) - 1)
        if extra_spaces != 0:
            w += extra_spaces * _measure_gdi_text_width(face, font_size, " ")
        return max(32, w)

    unit_size = max(8, round(font_size * s1_pct / 100))
    width_px = _measure_gdi_text_width(face, font_size, group.preview_label + (" " * ns))
    for idx, s in enumerate(group.sensors):
        width_px += _measure_gdi_text_width(face, font_size, s.val)
        if s.unit:
            width_px += _measure_gdi_text_width(face, unit_size, s.unit)
        if idx < len(group.sensors) - 1:
            width_px += _measure_gdi_text_width(face, font_size, " " * ds)
    return max(24, width_px)


def compute_layout(cfg: OsdConfig, groups: list[OsdGroupSpec] | None = None) -> ComputedLayout:
    active_groups = list(groups) if groups else canonical_groups()
    ns = max(1, min(4, int(cfg.name_spaces)))
    ds = max(1, min(4, int(cfg.data_spaces)))
    cap_h, internal_leading, scale = measure_gdi_metrics(cfg.face, cfg.font_size)
    widths = [
        _measure_group_width(g, cfg.face, cfg.font_size, cfg.s1_pct, scale, ns, ds)
        for g in active_groups
    ]

    if cfg.flush_edge:
        rtss_pos_x = 1
        rtss_pos_y = 1
        origin_offset = 1
        m_tag = "<M=-1,-1>"
    else:
        rtss_pos_x = max(1, int(cfg.pos_x))
        rtss_pos_y = max(1, int(cfg.pos_y))
        origin_offset = 0
        m_tag = ""

    screen_groups: list[int] = []
    screen_divs: list[int] = []
    cur_x = cfg.pad_x
    for idx, w in enumerate(widths):
        screen_groups.append(cur_x)
        if idx < len(widths) - 1:
            div_x = cur_x + w + cfg.gap_before
            screen_divs.append(div_x)
            cur_x = div_x + cfg.gap_after
        else:
            cur_x = cur_x + w + cfg.pad_x

    bar_w = max(40, cur_x)

    p_groups = [max(0, x - origin_offset) for x in screen_groups]
    d_x = [max(0, d - origin_offset) for d in screen_divs]

    top_pad = (cfg.bar_h - cap_h + 1) // 2
    text_py = max(0, top_pad - internal_leading - origin_offset)
    div_h = max(6, cap_h + 1)
    div_py = max(0, (cfg.bar_h - div_h) // 2 - origin_offset)

    c7_tag = f"{pct_to_hex(cfg.bg_alpha_pct)}{cfg.bg_rgb.upper()}"
    c8_tag = f"{pct_to_hex(cfg.div_alpha_pct)}FFFFFF"

    format_header = (
        f"<C0={cfg.c0.upper()}><C1={cfg.c1.upper()}><C2={cfg.c2.upper()}>{FIXED_A_PREFIX}"
        f"<C4={cfg.c4.upper()}><C5={cfg.c5.upper()}><C6={cfg.c6.upper()}><C7={c7_tag}><C8={c8_tag}>"
        f"<C250={cfg.c4.upper()}>" + FIXED_A_SUFFIX.format(s1=cfg.s1_pct)
    )
    prolog_separator = f'"<P=0,0><L0>{m_tag}<C7><B={bar_w},{cfg.bar_h}><C><L1>"'

    parts: list[str] = []
    for idx, grp in enumerate(active_groups):
        prefix = ""
        if idx > 0 and cfg.div_alpha_pct > 0:
            prefix = f"<P={d_x[idx - 1]},{div_py}><C8><B=1,{div_h}><C>"
        parts.append(f"{prefix}<P={p_groups[idx]},{text_py}>:{grp.id_hex}")
    group_separator = ";".join(parts)

    return ComputedLayout(
        rtss_pos_x=rtss_pos_x,
        rtss_pos_y=rtss_pos_y,
        c7_tag=c7_tag,
        c8_tag=c8_tag,
        bar_w=bar_w,
        bar_h=cfg.bar_h,
        cap_h=cap_h,
        text_py=text_py,
        div_h=div_h,
        div_py=div_py,
        groups=active_groups,
        p_groups=p_groups,
        d_x=d_x,
        screen_groups=screen_groups,
        screen_divs=screen_divs,
        format_header=format_header,
        prolog_separator=prolog_separator,
        group_separator=group_separator,
        group_name_separator='"' + (" " * ns) + '"',
        group_data_separator='"' + (" " * ds) + '"',
    )


def _parse_quoted_spaces(raw: str, default: int = 1) -> int:
    s = raw.strip()
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        inner = s[1:-1]
        if inner and all(ch == " " for ch in inner):
            return max(1, min(4, len(inner)))
    return default


def load_current_config(paths: InstallPaths, *, allow_live_mahm: bool = False) -> tuple[OsdConfig, list[OsdGroupSpec]]:
    cfg = OsdConfig()
    groups, _ = parse_ab_osd_groups(paths.ab_cfg, order_mode="auto", allow_live_mahm=allow_live_mahm)
    if not groups:
        groups = canonical_groups()

    ab_layout: dict[str, str] = {}
    if paths.ab_cfg and paths.ab_cfg.is_file():
        ab_lines = _split_ini_lines(paths.ab_cfg.read_bytes())
        ab_layout = _parse_section(ab_lines, "OSDLayout1")
    fh = ab_layout.get("FormatHeader", "")
    has_steam_tags = "<C7=" in fh or "<B=" in ab_layout.get("PrologSeparator", "")

    if paths.rtss_profiles_dir:
        global_path = paths.rtss_profiles_dir / "Global"
        if global_path.is_file():
            g_lines = _split_ini_lines(global_path.read_bytes())
            osd = _parse_section(g_lines, "OSD")
            font = _parse_section(g_lines, "Font")
            if "PositionX" in osd and osd["PositionX"].lstrip("-").isdigit():
                cfg.pos_x = max(1, int(osd["PositionX"]))
            if "PositionY" in osd and osd["PositionY"].lstrip("-").isdigit():
                cfg.pos_y = max(1, int(osd["PositionY"]))
            if "Face" in font and font["Face"]:
                face_raw = font["Face"][:31]
                # Preserve non-stock custom font unless it is RTSS's stock Unispace
                if face_raw != "Unispace" or has_steam_tags:
                    cfg.face = face_raw
            if "Height" in font:
                try:
                    raw_h = abs(int(font["Height"]))
                    if raw_h >= 12 or has_steam_tags:
                        cfg.font_size = max(10, min(32, raw_h))
                except ValueError:
                    pass
            if "OutlineFactor" in font and font["OutlineFactor"].isdigit():
                if has_steam_tags:
                    cfg.outline_factor = max(0, min(255, int(font["OutlineFactor"])))
            if "CharsetSize1" in font and font["CharsetSize1"].isdigit():
                cfg.s1_pct = max(45, min(100, int(font["CharsetSize1"])))

    cfg.name_spaces = _parse_quoted_spaces(ab_layout.get("GroupNameSeparator", ""), 1)
    cfg.data_spaces = _parse_quoted_spaces(ab_layout.get("GroupDataSeparator", ""), 1)

    if has_steam_tags:
        for tag, attr in [("C0", "c0"), ("C1", "c1"), ("C2", "c2"), ("C4", "c4"), ("C5", "c5"), ("C6", "c6")]:
            m = re.search(rf"<{tag}=([0-9A-Fa-f]{{6}})>", fh)
            if m:
                setattr(cfg, attr, m.group(1).upper())
        m7 = re.search(r"<C7=([0-9A-Fa-f]{2})([0-9A-Fa-f]{6})>", fh)
        if m7:
            cfg.bg_alpha_pct = hex_to_pct(m7.group(1))
            cfg.bg_rgb = m7.group(2).upper()
        m8 = re.search(r"<C8=([0-9A-Fa-f]{2})[0-9A-Fa-f]{6}>", fh)
        if m8:
            cfg.div_alpha_pct = hex_to_pct(m8.group(1))
        ms1 = re.search(r"<S1=(\d+)>", fh)
        if ms1:
            cfg.s1_pct = max(45, min(100, int(ms1.group(1))))

        prolog = ab_layout.get("PrologSeparator", "")
        cfg.flush_edge = "<M=-1,-1>" in prolog
        mb = re.search(r"<B=(\d+),(\d+)>", prolog) or re.search(r"<E=(\d+),(\d+)>", prolog)
        if mb:
            cfg.bar_h = max(18, min(60, int(mb.group(2))))

        gs = ab_layout.get("GroupSeparator", "")
        origin_offset = 1 if cfg.flush_edge else 0
        first_gid = groups[0].id_hex if groups else "50"
        m_first = re.search(rf"<P=(\d+),\d+>:{re.escape(first_gid)}(?:;|$)", gs)
        if m_first:
            cfg.pad_x = max(4, min(50, int(m_first.group(1)) + origin_offset))
        if len(groups) >= 2:
            g0, g1 = groups[0], groups[1]
            m_div = re.search(
                rf"<P=(\d+),\d+><C8><B=1,\d+><C><P=(\d+),\d+>:{re.escape(g1.id_hex)}(?:;|$)",
                gs,
            )
            if m_div:
                _, _, scale = measure_gdi_metrics(cfg.face, cfg.font_size)
                w0 = _measure_group_width(
                    g0,
                    cfg.face,
                    cfg.font_size,
                    cfg.s1_pct,
                    scale,
                    cfg.name_spaces,
                    cfg.data_spaces,
                )
                d1_x = int(m_div.group(1)) + origin_offset
                x1 = int(m_div.group(2)) + origin_offset
                cfg.gap_after = max(2, min(40, x1 - d1_x))
                cfg.gap_before = max(2, min(40, d1_x - (cfg.pad_x + w0)))

    return cfg, groups


def _load_pil_font(face: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_path, var_cfg = _find_font_file(face)
    if font_path is None:
        fallback_face = default_face_for_os()
        font_path, var_cfg = _find_font_file(fallback_face)
    if font_path is None:
        return ImageFont.load_default()
    try:
        font = ImageFont.truetype(str(font_path), size)
        if var_cfg is not None:
            weight, named_style = var_cfg
            try:
                if font_path.name.lower() == "seguivar.ttf":
                    font.set_variation_by_axes([weight, max(5, min(36, size))])
                elif font_path.name.lower() == "bahnschrift.ttf":
                    font.set_variation_by_axes([weight, 100])
            except Exception:
                if named_style:
                    try:
                        font.set_variation_by_name(named_style)
                    except Exception:
                        pass
        return font
    except Exception:
        return ImageFont.load_default()


def _hex_to_rgba(hex_rgb: str, alpha: int = 255) -> tuple[int, int, int, int]:
    clean = re.sub(r"[^0-9A-Fa-f]", "", hex_rgb).ljust(6, "0")[:6]
    return (int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16), max(0, min(255, alpha)))


def render_preview_pil(cfg: OsdConfig, groups: list[OsdGroupSpec] | None = None, scene: str = "bright") -> Image.Image:
    layout = compute_layout(cfg, groups=groups)
    margin_preview = 18
    canvas_w = max(860, layout.bar_w + (0 if cfg.flush_edge else layout.rtss_pos_x) + margin_preview)
    canvas_h = max(72, layout.bar_h + (0 if cfg.flush_edge else layout.rtss_pos_y) + 26)

    bg = Image.new("RGBA", (canvas_w, canvas_h))
    bg_draw = ImageDraw.Draw(bg)

    if scene == "bright":
        for x in range(canvas_w):
            t = x / max(1, canvas_w - 1)
            r = int(115 + 65 * abs(0.5 - t) * 2 - 20 * t)
            g = int(95 + 55 * (1 - abs(0.4 - t)))
            b = int(130 + 55 * t)
            bg_draw.line([(x, 0), (x, canvas_h - 1)], fill=(min(255, r), min(255, g), min(255, b), 255))
        bg_draw.rectangle([40, 6, 160, canvas_h], fill=(210, 155, 85, 255))
        bg_draw.rectangle([380, 0, 520, 44], fill=(165, 185, 205, 255))
        bg_draw.rectangle([min(820, canvas_w - 40), 8, canvas_w - 10, canvas_h], fill=(185, 110, 95, 255))
    elif scene == "dark":
        for x in range(canvas_w):
            t = x / max(1, canvas_w - 1)
            r = int(28 + 26 * t)
            g = int(24 + 20 * (1 - t))
            b = int(22 + 18 * t)
            bg_draw.line([(x, 0), (x, canvas_h - 1)], fill=(r, g, b, 255))
        bg_draw.rectangle([180, 4, 300, 42], fill=(64, 48, 36, 255))
        bg_draw.rectangle([520, 0, 660, 38], fill=(42, 54, 62, 255))
    else:
        bg_draw.rectangle([0, 0, canvas_w, canvas_h], fill=(0, 0, 0, 255))

    overlay = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    ov = ImageDraw.Draw(overlay)

    bx = 0 if cfg.flush_edge else layout.rtss_pos_x
    by = 0 if cfg.flush_edge else layout.rtss_pos_y
    a_bg = int(layout.c7_tag[0:2], 16)
    a_div = int(layout.c8_tag[0:2], 16)

    ov.rectangle([bx, by, bx + layout.bar_w - 1, by + layout.bar_h - 1], fill=_hex_to_rgba(cfg.bg_rgb, a_bg))

    if cfg.div_alpha_pct > 0:
        dy_start = (layout.div_py + 1) if cfg.flush_edge else (by + layout.div_py)
        for dx_p in layout.screen_divs:
            dx = (0 if cfg.flush_edge else bx) + dx_p
            ov.rectangle([dx, dy_start, dx, dy_start + layout.div_h - 1], fill=(255, 255, 255, a_div))

    main_font = _load_pil_font(cfg.face, cfg.font_size)
    unit_size = max(8, round(cfg.font_size * cfg.s1_pct / 100))
    unit_font = _load_pil_font(cfg.face, unit_size)

    ty = by + (layout.text_py + (1 if cfg.flush_edge else 0)) - 2
    unit_ty = ty + max(1, round((cfg.font_size - unit_size) * 0.25))

    c_val = _hex_to_rgba(cfg.c4, 255)
    c_unit = _hex_to_rgba(cfg.c6, 255)
    outline_px = 1 if cfg.outline_factor > 0 else 0

    def draw_str(x_cur: float, y_cur: float, txt: str, fnt: object, col: tuple[int, int, int, int]) -> float:
        if outline_px:
            for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                ov.text((x_cur + ox, y_cur + oy), txt, font=fnt, fill=(0, 0, 0, 200))
        ov.text((x_cur, y_cur), txt, font=fnt, fill=col)
        try:
            return float(ov.textlength(txt, font=fnt))
        except Exception:
            return len(txt) * cfg.font_size * 0.6

    ns = max(1, min(4, int(cfg.name_spaces)))
    ds = max(1, min(4, int(cfg.data_spaces)))
    color_map = {"c0": cfg.c0, "c1": cfg.c1, "c2": cfg.c2, "c5": cfg.c5}
    for idx, grp in enumerate(layout.groups):
        gx = layout.screen_groups[idx]
        cur_x = (0 if cfg.flush_edge else bx) + gx
        c_grp = _hex_to_rgba(color_map.get(grp.color_key, cfg.c0), 255)
        cur_x += draw_str(cur_x, ty, grp.preview_label + (" " * ns), main_font, c_grp)
        for s_idx, sensor in enumerate(grp.sensors):
            cur_x += draw_str(cur_x, ty, sensor.val, main_font, c_val)
            if sensor.unit:
                cur_x += draw_str(cur_x, unit_ty, sensor.unit, unit_font, c_unit)
            if s_idx < len(grp.sensors) - 1:
                cur_x += draw_str(cur_x, ty, " " * ds, main_font, c_val)

    return Image.alpha_composite(bg, overlay).convert("RGB")


def find_active_rtss_profiles(rtss_profiles_dir: Path) -> list[Path]:
    result: list[Path] = []
    global_p = rtss_profiles_dir / "Global"
    if global_p.is_file():
        result.append(global_p)
    for p in sorted(rtss_profiles_dir.glob("*.cfg")):
        lines = _split_ini_lines(p.read_bytes())
        hook = _parse_section(lines, "Hooking")
        if hook.get("EnableHooking", "").strip() == "1":
            result.append(p)
    return result


def create_backups(
    files: list[Path],
    ab_cfg_path: Path,
    backups_root: Path = BACKUPS_ROOT,
) -> Path:
    initial_dir = backups_root / "initial"
    manifest_file = initial_dir / "manifest.json"
    if not manifest_file.is_file():
        initial_dir.mkdir(parents=True, exist_ok=True)
        rtss_names: list[str] = []
        for f in files:
            if not f.is_file():
                continue
            if f == ab_cfg_path:
                shutil.copy2(f, initial_dir / "MSIAfterburner.cfg")
            else:
                shutil.copy2(f, initial_dir / f"rtss_{f.name}")
                rtss_names.append(f.name)
        manifest_file.write_text(
            json.dumps(
                {
                    "created_at": datetime.now().isoformat(),
                    "ab_cfg_name": "MSIAfterburner.cfg",
                    "rtss_files": rtss_names,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    rolling_root = backups_root / "rolling"
    rolling_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:19]
    dest = rolling_root / stamp
    suffix = 1
    while dest.exists():
        dest = rolling_root / f"{stamp}_{suffix}"
        suffix += 1
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        if f.is_file():
            target_name = f.name if f == ab_cfg_path else f"rtss_{f.name}"
            shutil.copy2(f, dest / target_name)

    existing = sorted([d for d in rolling_root.iterdir() if d.is_dir()], key=lambda d: d.name)
    for old_d in existing[:-10]:
        shutil.rmtree(old_d, ignore_errors=True)
    return dest


def has_initial_backup(backups_root: Path = BACKUPS_ROOT) -> bool:
    return (backups_root / "initial" / "manifest.json").is_file()


def _find_afterburner_scheduled_task(ab_exe: Path | None) -> str | None:
    if os.name != "nt":
        return None
    try:
        rc, out, _ = _run_silent(["schtasks", "/query", "/tn", "MSIAfterburner", "/xml"], timeout_s=4.0)
        if rc == 0 and "MSIAfterburner.exe" in out:
            if ab_exe is None or str(ab_exe).lower() in out.lower() or "MSIAfterburner.exe" in out:
                return "MSIAfterburner"
    except Exception:
        pass
    return None


def _close_afterburner_windows_gracefully() -> None:
    if os.name != "nt":
        return
    WM_CLOSE = 0x0010
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def _cb(hwnd: int, _lparam: int) -> bool:
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            hp = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if hp:
                try:
                    buf = ctypes.create_unicode_buffer(512)
                    sz = wintypes.DWORD(len(buf))
                    if k32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(sz)):
                        if os.path.basename(buf.value).lower() == "msiafterburner.exe":
                            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                finally:
                    k32.CloseHandle(hp)
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(_cb), 0)
    except Exception:
        pass


def _stop_afterburner(ab_exe: Path | None) -> tuple[str, str | None]:
    """Stop MSIAfterburner if running. Returns (method, task_name)."""
    if not is_process_running("MSIAfterburner.exe"):
        return "not_running", None

    task_name = _find_afterburner_scheduled_task(ab_exe)
    if task_name:
        _run_silent(["schtasks", "/end", "/tn", task_name], timeout_s=8.0)
        for _ in range(20):
            if not is_process_running("MSIAfterburner.exe"):
                return "schtasks", task_name
            time.sleep(0.25)

    _close_afterburner_windows_gracefully()
    for _ in range(16):
        if not is_process_running("MSIAfterburner.exe"):
            return "graceful", task_name
        time.sleep(0.25)

    _run_silent(["taskkill", "/IM", "MSIAfterburner.exe", "/F"], timeout_s=8.0)
    for _ in range(12):
        if not is_process_running("MSIAfterburner.exe"):
            return "taskkill", task_name
        time.sleep(0.25)

    raise RuntimeError("Could not stop running MSIAfterburner.exe; aborted before modifying MSIAfterburner.cfg.")


def _start_afterburner(ab_exe: Path | None, task_name: str | None) -> None:
    if task_name:
        rc, out, err = _run_silent(["schtasks", "/run", "/tn", task_name], timeout_s=8.0)
        if rc == 0:
            return
        if not ab_exe or not ab_exe.is_file():
            raise RuntimeError(f"Failed to start scheduled task '{task_name}': {err or out}")

    if not ab_exe or not ab_exe.is_file():
        raise RuntimeError("MSIAfterburner.exe not found to relaunch.")

    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [str(ab_exe), "/s"],
        cwd=str(ab_exe.parent),
        creationflags=flags,
        close_fds=True,
    )


def _call_rtss_update_profiles(hooks_dll: Path) -> int:
    dll = ctypes.WinDLL(str(hooks_dll))
    fn = dll.UpdateProfiles
    fn.restype = ctypes.c_int
    return int(fn())


def _verify_shared_memory_contains(c7_tag: str, timeout_s: float = 4.5) -> tuple[bool, int]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            m = mmap.mmap(-1, 64, tagname="RTSSSharedMemoryV2", access=mmap.ACCESS_READ)
            hdr = struct.unpack("<9I", m[:36])
            m.close()
            _, _, dwAppEntrySize, dwAppArrOffset, dwAppArrSize, dwOSDEntrySize, dwOSDArrOffset, dwOSDArrSize, dwOSDFrame = hdr
            total = max(dwOSDArrOffset + dwOSDArrSize * dwOSDEntrySize, dwAppArrOffset + dwAppArrSize * dwAppEntrySize)
            m = mmap.mmap(-1, total, tagname="RTSSSharedMemoryV2", access=mmap.ACCESS_READ)
            data = m[:]
            m.close()
            for i in range(dwOSDArrSize):
                off = dwOSDArrOffset + i * dwOSDEntrySize
                chunk = data[off : off + dwOSDEntrySize]
                owner = chunk[256:512].split(b"\x00", 1)[0]
                if owner == b"MSIAfterburner":
                    idx = chunk.find(b"<C0=", 512)
                    start = idx if idx != -1 else 512
                    ex_raw = chunk[start:].split(b"\x00", 1)[0]
                    if f"<C7={c7_tag}>".encode("latin1") in ex_raw:
                        return True, int(dwOSDFrame)
        except Exception:
            pass
        time.sleep(0.3)
    return False, 0


def build_ab_layout_updates(layout: ComputedLayout, include_graph_defaults: bool = False) -> dict[str, str]:
    updates = {
        "FormatHeader": layout.format_header,
        "ValueAlignmentTag": "<A>",
        "UnitsAlignmentTag": "",
        "GroupColorTag": GROUP_COLOR_TAG,
        "ValueColorTag": "<C4>",
        "AlarmColorTag": "<C3>",
        "UnitsColorTag": "<C6>",
        "GraphColorTag": GRAPH_COLOR_TAG,
        "GroupSizeTag": "",
        "IndexSizeTag": "<S0>",
        "ValueSizeTag": "<S1>:70,71,72,74,75",
        "UnitsSizeTag": "<S1>",
        "GraphSizeTag": "<S1>",
        "PrologSeparator": layout.prolog_separator,
        "Prolog0Separator": '""',
        "Prolog1Separator": '""',
        "Prolog2Separator": '""',
        "GroupDataSeparator": layout.group_data_separator,
        "GroupNameSeparator": layout.group_name_separator,
        "EpilogSeparator": '""',
        "GroupSeparator": layout.group_separator,
    }
    if include_graph_defaults:
        updates.update(
            {
                "GraphSeparator": "",
                "GraphWidth": "-32",
                "GraphWidthEmbedded": "-4",
                "GraphHeight": "-2",
                "GraphMargin": "1",
                "GraphStyle": "0",
                "GraphLabel": "3",
                "GraphPlacement": "2",
            }
        )
    return updates


@dataclass
class ApplyResult:
    rtss_count: int
    frame: int
    backup_dir: Path
    live_reloaded: bool
    note: str


def apply_config(
    cfg: OsdConfig,
    paths: InstallPaths,
    *,
    groups: list[OsdGroupSpec] | None = None,
    backups_root: Path = BACKUPS_ROOT,
    sandbox_mode: bool = False,
) -> ApplyResult:
    if not paths.is_valid_ab() or not paths.is_valid_rtss():
        raise FileNotFoundError("Invalid MSI Afterburner or RTSS paths.")

    assert paths.ab_cfg is not None and paths.rtss_profiles_dir is not None

    active_groups = groups if groups is not None else parse_ab_osd_groups(paths.ab_cfg)[0]
    layout = compute_layout(cfg, groups=active_groups)
    rtss_profiles = find_active_rtss_profiles(paths.rtss_profiles_dir)
    backup_dir = create_backups([paths.ab_cfg, *rtss_profiles], paths.ab_cfg, backups_root=backups_root)

    face_clean = cfg.face.encode("latin1", "replace").decode("latin1")[:31]
    osd_updates = {
        "EnableOSD": "1",
        "BaseColor": f"FF{cfg.c4.upper()}",
        "PositionX": str(layout.rtss_pos_x),
        "PositionY": str(layout.rtss_pos_y),
        "ZoomRatio": "1",
    }
    font_updates = {
        "Height": f"-{cfg.font_size}",
        "Weight": "600",
        "Face": face_clean,
        "Load": "",
        "OutlinePassCount": "1" if cfg.outline_factor > 0 else "0",
        "OutlineFactor": str(max(0, min(255, cfg.outline_factor))),
        "CharsetSize0": "100",
        "CharsetSize1": str(cfg.s1_pct),
    }

    for prof in rtss_profiles:
        lines = _split_ini_lines(prof.read_bytes())
        lines = _upsert_ini_section_keys(lines, "OSD", osd_updates, create_if_missing=True)
        lines = _upsert_ini_section_keys(lines, "Font", font_updates, create_if_missing=True)
        prof.write_bytes("\r\n".join(lines).encode("latin1"))

    rtss_online = (not sandbox_mode) and is_process_running("RTSS.exe")
    if rtss_online and paths.rtss_hooks_dll and paths.rtss_hooks_dll.is_file():
        upd_ret = _call_rtss_update_profiles(paths.rtss_hooks_dll)
        if upd_ret != 1:
            raise RuntimeError(f"RTSSHooks64.dll!UpdateProfiles() returned {upd_ret} (expected 1)")

    ab_was_running = False
    task_name: str | None = None
    if not sandbox_mode:
        method, task_name = _stop_afterburner(paths.ab_exe)
        ab_was_running = method != "not_running"

    try:
        ab_lines = _split_ini_lines(paths.ab_cfg.read_bytes())
        has_layout1 = any(ln.strip() == "[OSDLayout1]" for ln in ab_lines)
        ab_lines = _upsert_ini_section_keys(
            ab_lines, "Settings", {"OSDLayout": "1"}, create_if_missing=True
        )
        ab_updates = build_ab_layout_updates(layout, include_graph_defaults=not has_layout1)
        ab_lines = _upsert_ini_section_keys(
            ab_lines, "OSDLayout1", ab_updates, create_if_missing=True
        )
        paths.ab_cfg.write_bytes("\r\n".join(ab_lines).encode("latin1"))
    finally:
        if ab_was_running and not sandbox_mode:
            _start_afterburner(paths.ab_exe, task_name)

    if ab_was_running and rtss_online:
        ok, frame = _verify_shared_memory_contains(layout.c7_tag)
        if not ok:
            raise RuntimeError("MSIAfterburner restarted, but RTSSSharedMemoryV2 did not reflect new tags within 4.5s.")
        return ApplyResult(len(rtss_profiles), frame, backup_dir, True, "live")

    note = "sandbox" if sandbox_mode else "offline; start AB/RTSS to display"
    return ApplyResult(len(rtss_profiles), 0, backup_dir, False, note)


def restore_initial_backup(
    paths: InstallPaths,
    *,
    backups_root: Path = BACKUPS_ROOT,
    sandbox_mode: bool = False,
) -> int:
    initial_dir = backups_root / "initial"
    manifest_file = initial_dir / "manifest.json"
    if not manifest_file.is_file():
        raise FileNotFoundError(str(manifest_file))
    if not paths.ab_cfg or not paths.rtss_profiles_dir:
        raise FileNotFoundError("Invalid target paths.")

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    ab_src = initial_dir / manifest.get("ab_cfg_name", "MSIAfterburner.cfg")
    if not ab_src.is_file():
        raise FileNotFoundError(str(ab_src))

    ab_was_running = False
    task_name: str | None = None
    if not sandbox_mode:
        method, task_name = _stop_afterburner(paths.ab_exe)
        ab_was_running = method != "not_running"

    restored = 0
    try:
        shutil.copy2(ab_src, paths.ab_cfg)
        restored += 1
        for rname in manifest.get("rtss_files", []):
            src_r = initial_dir / f"rtss_{rname}"
            if src_r.is_file():
                shutil.copy2(src_r, paths.rtss_profiles_dir / rname)
                restored += 1

        rtss_online = (not sandbox_mode) and is_process_running("RTSS.exe")
        if rtss_online and paths.rtss_hooks_dll and paths.rtss_hooks_dll.is_file():
            _call_rtss_update_profiles(paths.rtss_hooks_dll)
    finally:
        if ab_was_running and not sandbox_mode:
            _start_afterburner(paths.ab_exe, task_name)

    return restored


if HAS_TK:

    class OsdTunerApp:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.lang = detect_default_lang()
            self.paths = discover_install_paths()
            self.cfg, self.groups = load_current_config(self.paths, allow_live_mahm=True)
            self._selected_group_idx: int = 0
            self._photo_ref: object | None = None
            self._after_preview_id: str | None = None

            self._init_vars()
            self._build_ui()
            self._apply_language_texts()
            self._sync_vars_from_cfg()
            self._refresh_paths_banner()
            self._schedule_preview()

        def tr(self, key: str, **kw: object) -> str:
            tmpl = I18N[self.lang].get(key, I18N["en"].get(key, key))
            return tmpl.format(**kw) if kw else tmpl

        def _init_vars(self) -> None:
            self.var_scene = tk.StringVar(value="bright")
            self.var_ab_dir = tk.StringVar()
            self.var_rtss_dir = tk.StringVar()
            self.var_runtime_badge = tk.StringVar()
            self.var_alpha = tk.DoubleVar()
            self.var_bg_rgb = tk.StringVar()
            self.var_flush = tk.BooleanVar()
            self.var_pos_x = tk.StringVar()
            self.var_pos_y = tk.StringVar()
            self.var_name_spaces = tk.StringVar()
            self.var_data_spaces = tk.StringVar()
            self.var_bar_h = tk.DoubleVar()
            self.var_pad_x = tk.DoubleVar()
            self.var_gap_before = tk.DoubleVar()
            self.var_gap_after = tk.DoubleVar()
            self.var_div_alpha = tk.DoubleVar()
            self.var_face = tk.StringVar()
            self.var_font_size = tk.DoubleVar()
            self.var_s1_pct = tk.DoubleVar()
            self.var_outline = tk.DoubleVar()
            self.var_status = tk.StringVar()
            self.var_alpha_hex_lbl = tk.StringVar()
            self.var_geom_lbl = tk.StringVar()
            self.color_vars = {k: tk.StringVar() for k in DEFAULT_COLORS}
            self._i18n_widgets: list[tuple[object, str, str]] = []

        def _bind_i18n(self, widget: object, key: str, prop: str = "text") -> None:
            self._i18n_widgets.append((widget, prop, key))

        def _build_ui(self) -> None:
            self.root.geometry("1200x880")
            self.root.minsize(1040, 780)
            style = ttk.Style()
            try:
                style.theme_use("vista")
            except Exception:
                pass

            main = ttk.Frame(self.root, padding=10)
            main.pack(fill=tk.BOTH, expand=True)

            # 0. Detected Paths Bar + Language Toggle
            self.paths_box = ttk.LabelFrame(main, text="", padding=6)
            self.paths_box.pack(fill=tk.X, pady=(0, 6))
            self._bind_i18n(self.paths_box, "paths_frame")

            pr = ttk.Frame(self.paths_box)
            pr.pack(fill=tk.X)
            lbl_ab = ttk.Label(pr, text="", width=11)
            lbl_ab.pack(side=tk.LEFT)
            self._bind_i18n(lbl_ab, "ab_dir")
            ttk.Entry(pr, textvariable=self.var_ab_dir, state="readonly", width=36).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
            btn_ab = ttk.Button(pr, text="", width=8, command=self._browse_ab_dir)
            btn_ab.pack(side=tk.LEFT, padx=(0, 10))
            self._bind_i18n(btn_ab, "browse")

            lbl_rt = ttk.Label(pr, text="", width=6)
            lbl_rt.pack(side=tk.LEFT)
            self._bind_i18n(lbl_rt, "rtss_dir")
            ttk.Entry(pr, textvariable=self.var_rtss_dir, state="readonly", width=36).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
            btn_rt = ttk.Button(pr, text="", width=8, command=self._browse_rtss_dir)
            btn_rt.pack(side=tk.LEFT, padx=(0, 8))
            self._bind_i18n(btn_rt, "browse")

            btn_redetect = ttk.Button(pr, text="", width=11, command=self._redetect_paths)
            btn_redetect.pack(side=tk.LEFT, padx=(0, 6))
            self._bind_i18n(btn_redetect, "redetect")

            self.btn_lang = ttk.Button(pr, text="", width=9, command=self._toggle_language)
            self.btn_lang.pack(side=tk.RIGHT)
            self._bind_i18n(self.btn_lang, "lang_btn")

            ttk.Label(self.paths_box, textvariable=self.var_runtime_badge, foreground="#3B5B8C").pack(anchor=tk.W, pady=(4, 0))

            # 1. Preview Frame
            prev_box = ttk.LabelFrame(main, text="", padding=8)
            prev_box.pack(fill=tk.X, pady=(0, 8))
            self._bind_i18n(prev_box, "preview_frame")

            scene_bar = ttk.Frame(prev_box)
            scene_bar.pack(fill=tk.X, pady=(0, 6))
            for val, key in [("bright", "scene_bright"), ("dark", "scene_dark"), ("black", "scene_black")]:
                rb = ttk.Radiobutton(scene_bar, text="", value=val, variable=self.var_scene, command=self._schedule_preview)
                rb.pack(side=tk.LEFT, padx=(0, 14))
                self._bind_i18n(rb, key)
            ttk.Label(scene_bar, textvariable=self.var_geom_lbl, foreground="#2D5F9A").pack(side=tk.RIGHT)

            self.preview_canvas = tk.Canvas(prev_box, height=90, bg="#111111", highlightthickness=1, highlightbackground="#444444")
            self.preview_canvas.pack(fill=tk.X)
            xscroll = ttk.Scrollbar(prev_box, orient=tk.HORIZONTAL, command=self.preview_canvas.xview)
            self.preview_canvas.configure(xscrollcommand=xscroll.set)
            xscroll.pack(fill=tk.X)

            order_bar = ttk.Frame(prev_box)
            order_bar.pack(fill=tk.X, pady=(6, 0))
            lbl_ord = ttk.Label(order_bar, text="")
            lbl_ord.pack(side=tk.LEFT, padx=(0, 6))
            self._bind_i18n(lbl_ord, "order_lbl")

            self.group_pills_frame = ttk.Frame(order_bar)
            self.group_pills_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

            btn_ab_ord = ttk.Button(order_bar, text="", width=14, command=self._apply_ab_order)
            btn_ab_ord.pack(side=tk.RIGHT, padx=(4, 0))
            self._bind_i18n(btn_ab_ord, "order_ab")
            btn_st_ord = ttk.Button(order_bar, text="", width=12, command=self._apply_steam_order)
            btn_st_ord.pack(side=tk.RIGHT, padx=(4, 0))
            self._bind_i18n(btn_st_ord, "order_steam")
            ttk.Button(order_bar, text="▶", width=3, command=lambda: self._move_group(1)).pack(side=tk.RIGHT, padx=(2, 0))
            ttk.Button(order_bar, text="◀", width=3, command=lambda: self._move_group(-1)).pack(side=tk.RIGHT, padx=(4, 0))

            # 2. Controls Grid
            cols = ttk.Frame(main)
            cols.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
            cols.columnconfigure(0, weight=1)
            cols.columnconfigure(1, weight=1)

            left = ttk.Frame(cols)
            left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

            bg_box = ttk.LabelFrame(left, text="", padding=10)
            bg_box.pack(fill=tk.X, pady=(0, 8))
            self._bind_i18n(bg_box, "bg_frame")

            r0 = ttk.Frame(bg_box)
            r0.pack(fill=tk.X, pady=3)
            l_alpha = ttk.Label(r0, text="", width=20)
            l_alpha.pack(side=tk.LEFT)
            self._bind_i18n(l_alpha, "bg_alpha")
            ttk.Scale(r0, from_=0, to=100, variable=self.var_alpha, orient=tk.HORIZONTAL, command=lambda _v: self._on_slider(self.var_alpha)).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
            ttk.Label(r0, textvariable=self.var_alpha_hex_lbl, width=14).pack(side=tk.RIGHT)

            r1 = ttk.Frame(bg_box)
            r1.pack(fill=tk.X, pady=4)
            l_rgb = ttk.Label(r1, text="", width=20)
            l_rgb.pack(side=tk.LEFT)
            self._bind_i18n(l_rgb, "bg_rgb")
            self.btn_bg_color = tk.Button(r1, text=" ", width=16, command=self._pick_bg_color, relief=tk.GROOVE)
            self.btn_bg_color.pack(side=tk.LEFT, padx=(6, 8))
            l_hint = ttk.Label(r1, text="", foreground="#555555")
            l_hint.pack(side=tk.LEFT)
            self._bind_i18n(l_hint, "alpha_hint")

            preset_row = ttk.Frame(bg_box)
            preset_row.pack(fill=tk.X, pady=(6, 2))
            for key, a_pct, rgb in PRESETS:
                btn = ttk.Button(preset_row, text="", command=lambda a=a_pct, c=rgb: self._apply_preset(a, c))
                btn.pack(side=tk.LEFT, padx=(0, 5), fill=tk.X, expand=True)
                self._bind_i18n(btn, f"preset_{key}")

            geom_box = ttk.LabelFrame(left, text="", padding=10)
            geom_box.pack(fill=tk.BOTH, expand=True)
            self._bind_i18n(geom_box, "geom_frame")

            gr0 = ttk.Frame(geom_box)
            gr0.pack(fill=tk.X, pady=2)
            chk = ttk.Checkbutton(gr0, text="", variable=self.var_flush, command=self._on_flush_toggle)
            chk.pack(side=tk.LEFT)
            self._bind_i18n(chk, "flush_chk")

            gr1 = ttk.Frame(geom_box)
            gr1.pack(fill=tk.X, pady=3)
            l_fxy = ttk.Label(gr1, text="", width=20)
            l_fxy.pack(side=tk.LEFT)
            self._bind_i18n(l_fxy, "float_xy")
            self.spin_px = ttk.Spinbox(gr1, from_=1, to=120, width=6, textvariable=self.var_pos_x, command=self._schedule_preview)
            self.spin_px.pack(side=tk.LEFT, padx=(6, 4))
            self.spin_px.bind("<KeyRelease>", lambda _e: self._schedule_preview())
            l_top = ttk.Label(gr1, text="")
            l_top.pack(side=tk.LEFT)
            self._bind_i18n(l_top, "top_lbl")
            self.spin_py = ttk.Spinbox(gr1, from_=1, to=120, width=6, textvariable=self.var_pos_y, command=self._schedule_preview)
            self.spin_py.pack(side=tk.LEFT, padx=4)
            self.spin_py.bind("<KeyRelease>", lambda _e: self._schedule_preview())
            ttk.Label(gr1, text="px").pack(side=tk.LEFT)

            gr_intra = ttk.Frame(geom_box)
            gr_intra.pack(fill=tk.X, pady=3)
            l_intra = ttk.Label(gr_intra, text="", width=20)
            l_intra.pack(side=tk.LEFT)
            self._bind_i18n(l_intra, "intra_spaces")
            spin_ns = ttk.Spinbox(gr_intra, from_=1, to=4, width=5, textvariable=self.var_name_spaces, command=self._schedule_preview)
            spin_ns.pack(side=tk.LEFT, padx=(6, 4))
            spin_ns.bind("<KeyRelease>", lambda _e: self._schedule_preview())
            l_isep = ttk.Label(gr_intra, text="")
            l_isep.pack(side=tk.LEFT)
            self._bind_i18n(l_isep, "intra_sep")
            spin_ds = ttk.Spinbox(gr_intra, from_=1, to=4, width=5, textvariable=self.var_data_spaces, command=self._schedule_preview)
            spin_ds.pack(side=tk.LEFT, padx=4)
            spin_ds.bind("<KeyRelease>", lambda _e: self._schedule_preview())
            l_iunit = ttk.Label(gr_intra, text="")
            l_iunit.pack(side=tk.LEFT)
            self._bind_i18n(l_iunit, "intra_unit")

            self._add_slider_row(geom_box, "bar_h", self.var_bar_h, 20, 56)
            self._add_slider_row(geom_box, "pad_x", self.var_pad_x, 4, 40)
            self._add_slider_row(geom_box, "gap_before", self.var_gap_before, 4, 40)
            self._add_slider_row(geom_box, "gap_after", self.var_gap_after, 4, 36)
            self._add_slider_row(geom_box, "div_alpha", self.var_div_alpha, 0, 65)

            right = ttk.Frame(cols)
            right.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

            font_box = ttk.LabelFrame(right, text="", padding=10)
            font_box.pack(fill=tk.X, pady=(0, 8))
            self._bind_i18n(font_box, "font_frame")

            fr0 = ttk.Frame(font_box)
            fr0.pack(fill=tk.X, pady=4)
            l_face = ttk.Label(fr0, text="", width=20)
            l_face.pack(side=tk.LEFT)
            self._bind_i18n(l_face, "face_lbl")
            faces = list(get_available_faces())
            if self.cfg.face not in faces:
                faces.insert(0, self.cfg.face)
            self.cb_face = ttk.Combobox(fr0, textvariable=self.var_face, values=faces, state="readonly")
            self.cb_face.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
            self.cb_face.bind("<<ComboboxSelected>>", lambda _e: self._schedule_preview())

            self._add_slider_row(font_box, "font_size", self.var_font_size, 12, 28)
            self._add_slider_row(font_box, "s1_pct", self.var_s1_pct, 55, 95)
            self._add_slider_row(font_box, "outline", self.var_outline, 0, 128)

            col_box = ttk.LabelFrame(right, text="", padding=10)
            col_box.pack(fill=tk.BOTH, expand=True)
            self._bind_i18n(col_box, "color_frame")

            self.color_btns: dict[str, tk.Button] = {}
            grid_f = ttk.Frame(col_box)
            grid_f.pack(fill=tk.X)
            grid_f.columnconfigure(0, weight=1)
            grid_f.columnconfigure(1, weight=1)
            for idx, (key, i18n_key) in enumerate([
                ("c2", "c2_lbl"), ("c1", "c1_lbl"), ("c0", "c0_lbl"),
                ("c5", "c5_lbl"), ("c4", "c4_lbl"), ("c6", "c6_lbl"),
            ]):
                cell = ttk.Frame(grid_f, padding=3)
                cell.grid(row=idx // 2, column=idx % 2, sticky="ew")
                btn = tk.Button(cell, text="", relief=tk.GROOVE, command=lambda k=key: self._pick_fg_color(k))
                btn.pack(fill=tk.X)
                self.color_btns[key] = btn
                self._bind_i18n(btn, i18n_key)

            btn_reset = ttk.Button(col_box, text="", command=self._reset_colors)
            btn_reset.pack(anchor=tk.E, pady=(8, 0))
            self._bind_i18n(btn_reset, "reset_colors")

            # 3. Generated Tag Preview
            tag_box = ttk.LabelFrame(main, text="", padding=8)
            tag_box.pack(fill=tk.X, pady=(0, 8))
            self._bind_i18n(tag_box, "tag_frame")
            self.txt_tags = tk.Text(tag_box, height=3, wrap=tk.NONE, font=("Consolas", 9), state=tk.DISABLED, bg="#F6F7F9")
            self.txt_tags.pack(fill=tk.X)

            # 4. Actions & Status
            act_bar = ttk.Frame(main)
            act_bar.pack(fill=tk.X)
            b_apply = ttk.Button(act_bar, text="", command=self._on_apply)
            b_apply.pack(side=tk.LEFT, padx=(0, 8))
            self._bind_i18n(b_apply, "apply_btn")
            b_reload = ttk.Button(act_bar, text="", command=self._on_reload)
            b_reload.pack(side=tk.LEFT, padx=(0, 8))
            self._bind_i18n(b_reload, "reload_btn")
            self.btn_restore = ttk.Button(act_bar, text="", command=self._on_restore)
            self.btn_restore.pack(side=tk.RIGHT)
            self._bind_i18n(self.btn_restore, "restore_btn")

            ttk.Label(main, textvariable=self.var_status, foreground="#1E6B38").pack(anchor=tk.W, pady=(6, 0))

        def _add_slider_row(self, parent: ttk.Frame, i18n_key: str, var: tk.DoubleVar, frm: int, to: int) -> None:
            row = ttk.Frame(parent)
            row.pack(fill=tk.X, pady=3)
            lbl = ttk.Label(row, text="", width=20)
            lbl.pack(side=tk.LEFT)
            self._bind_i18n(lbl, i18n_key)
            val_lbl = ttk.Label(row, width=5, anchor=tk.E)
            val_lbl.pack(side=tk.RIGHT)
            sc = ttk.Scale(
                row, from_=frm, to=to, variable=var, orient=tk.HORIZONTAL,
                command=lambda _v, vr=var, lb=val_lbl: self._on_slider(vr, lb),
            )
            sc.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
            val_lbl.configure(text=str(round(var.get())))
            var.trace_add("write", lambda *_a, vr=var, lb=val_lbl: lb.configure(text=str(round(vr.get()))))

        def _apply_language_texts(self) -> None:
            self.root.title(self.tr("title"))
            for widget, prop, key in self._i18n_widgets:
                widget.configure(**{prop: self.tr(key)})  # type: ignore[attr-defined]
            self.var_status.set(self.tr("status_loaded", groups=len(self.groups)))
            self._refresh_paths_banner()

        def _toggle_language(self) -> None:
            self.lang = "en" if self.lang == "zh" else "zh"
            self._apply_language_texts()
            self._update_preview_now()

        def _refresh_paths_banner(self) -> None:
            self.var_ab_dir.set(str(self.paths.ab_dir) if self.paths.ab_dir else self.tr("status_missing"))
            self.var_rtss_dir.set(str(self.paths.rtss_dir) if self.paths.rtss_dir else self.tr("status_missing"))
            rtss_txt = self.tr("rtss_online" if is_process_running("RTSS.exe") else "rtss_offline")
            ab_txt = self.tr("ab_online" if is_process_running("MSIAfterburner.exe") else "ab_offline")
            ready_txt = self.tr("status_ready" if (self.paths.is_valid_ab() and self.paths.is_valid_rtss()) else "status_missing")
            self.var_runtime_badge.set(f"{ready_txt}  |  {rtss_txt}  |  {ab_txt}")
            self.btn_restore.configure(state=tk.NORMAL if has_initial_backup() else tk.DISABLED)

        def _browse_ab_dir(self) -> None:
            chosen = filedialog.askdirectory(title=self.tr("pick_ab_title"), parent=self.root)
            if not chosen:
                return
            p = Path(chosen)
            if not (p / "Profiles" / "MSIAfterburner.cfg").is_file():
                messagebox.showerror(self.tr("invalid_dir_title"), self.tr("invalid_ab_dir"), parent=self.root)
                return
            self.paths.ab_dir = p
            save_custom_paths(self.paths)
            self._on_reload()

        def _browse_rtss_dir(self) -> None:
            chosen = filedialog.askdirectory(title=self.tr("pick_rtss_title"), parent=self.root)
            if not chosen:
                return
            p = Path(chosen)
            if not (p / "Profiles" / "Global").is_file():
                messagebox.showerror(self.tr("invalid_dir_title"), self.tr("invalid_rtss_dir"), parent=self.root)
                return
            self.paths.rtss_dir = p
            save_custom_paths(self.paths)
            self._on_reload()

        def _redetect_paths(self) -> None:
            if PATHS_CONFIG_FILE.is_file():
                PATHS_CONFIG_FILE.unlink(missing_ok=True)
            self.paths = discover_install_paths()
            self._on_reload()

        def _sync_vars_from_cfg(self) -> None:
            self.var_alpha.set(self.cfg.bg_alpha_pct)
            self.var_bg_rgb.set(self.cfg.bg_rgb)
            self.var_flush.set(self.cfg.flush_edge)
            self.var_pos_x.set(str(self.cfg.pos_x))
            self.var_pos_y.set(str(self.cfg.pos_y))
            self.var_name_spaces.set(str(self.cfg.name_spaces))
            self.var_data_spaces.set(str(self.cfg.data_spaces))
            self.var_bar_h.set(self.cfg.bar_h)
            self.var_pad_x.set(self.cfg.pad_x)
            self.var_gap_before.set(self.cfg.gap_before)
            self.var_gap_after.set(self.cfg.gap_after)
            self.var_div_alpha.set(self.cfg.div_alpha_pct)
            self.var_face.set(self.cfg.face)
            self.var_font_size.set(self.cfg.font_size)
            self.var_s1_pct.set(self.cfg.s1_pct)
            self.var_outline.set(self.cfg.outline_factor)
            for k, v in self.color_vars.items():
                v.set(getattr(self.cfg, k))
            self._toggle_pos_inputs()
            self._rebuild_group_pills()

        @staticmethod
        def _safe_int(val: object, default: int, min_v: int, max_v: int) -> int:
            try:
                n = round(float(str(val).strip()))
                return max(min_v, min(max_v, n))
            except Exception:
                return default

        def _read_vars_to_cfg(self) -> OsdConfig:
            return OsdConfig(
                bg_alpha_pct=round(float(self.var_alpha.get())),
                bg_rgb=self.var_bg_rgb.get().strip("#").upper()[:6].ljust(6, "0"),
                flush_edge=bool(self.var_flush.get()),
                pos_x=self._safe_int(self.var_pos_x.get(), 1, 1, 500),
                pos_y=self._safe_int(self.var_pos_y.get(), 1, 1, 500),
                name_spaces=self._safe_int(self.var_name_spaces.get(), 1, 1, 4),
                data_spaces=self._safe_int(self.var_data_spaces.get(), 1, 1, 4),
                bar_h=round(float(self.var_bar_h.get())),
                pad_x=round(float(self.var_pad_x.get())),
                gap_before=round(float(self.var_gap_before.get())),
                gap_after=round(float(self.var_gap_after.get())),
                div_alpha_pct=round(float(self.var_div_alpha.get())),
                face=self.var_face.get(),
                font_size=round(float(self.var_font_size.get())),
                s1_pct=round(float(self.var_s1_pct.get())),
                outline_factor=round(float(self.var_outline.get())),
                c0=self.color_vars["c0"].get(),
                c1=self.color_vars["c1"].get(),
                c2=self.color_vars["c2"].get(),
                c4=self.color_vars["c4"].get(),
                c5=self.color_vars["c5"].get(),
                c6=self.color_vars["c6"].get(),
            )

        def _rebuild_group_pills(self) -> None:
            for w in self.group_pills_frame.winfo_children():
                w.destroy()
            if not self.groups:
                return
            self._selected_group_idx = max(0, min(self._selected_group_idx, len(self.groups) - 1))
            for i, grp in enumerate(self.groups):
                sel = i == self._selected_group_idx
                btn = tk.Button(
                    self.group_pills_frame,
                    text=f" {i + 1}.{grp.preview_label} ",
                    relief=tk.SOLID if sel else tk.GROOVE,
                    bd=1,
                    bg="#2D5F9A" if sel else "#E9EDF2",
                    fg="#FFFFFF" if sel else "#222222",
                    activebackground="#234B7A",
                    activeforeground="#FFFFFF",
                    command=lambda idx=i: self._select_group(idx),
                )
                btn.pack(side=tk.LEFT, padx=2)

        def _select_group(self, idx: int) -> None:
            if 0 <= idx < len(self.groups):
                self._selected_group_idx = idx
                self._rebuild_group_pills()

        def _move_group(self, delta: int) -> None:
            n = len(self.groups)
            idx = self._selected_group_idx
            nxt = idx + delta
            if 0 <= idx < n and 0 <= nxt < n:
                self.groups[idx], self.groups[nxt] = self.groups[nxt], self.groups[idx]
                self._selected_group_idx = nxt
                self._rebuild_group_pills()
                self._schedule_preview()

        def _apply_steam_order(self) -> None:
            self.groups = order_groups(self.groups, "steam")
            self._selected_group_idx = 0
            self._rebuild_group_pills()
            self._schedule_preview()

        def _apply_ab_order(self) -> None:
            self.groups = order_groups(self.groups, "ab")
            self._selected_group_idx = 0
            self._rebuild_group_pills()
            self._schedule_preview()

        def _on_slider(self, var: tk.DoubleVar, lbl: ttk.Label | None = None) -> None:
            if lbl is not None:
                lbl.configure(text=str(round(float(var.get()))))
            self._schedule_preview()

        def _on_flush_toggle(self) -> None:
            self._toggle_pos_inputs()
            self._schedule_preview()

        def _toggle_pos_inputs(self) -> None:
            st = tk.DISABLED if self.var_flush.get() else tk.NORMAL
            self.spin_px.configure(state=st)
            self.spin_py.configure(state=st)

        def _apply_preset(self, alpha_pct: int, rgb: str) -> None:
            self.var_alpha.set(alpha_pct)
            self.var_bg_rgb.set(rgb)
            self._schedule_preview()

        def _pick_bg_color(self) -> None:
            chosen = colorchooser.askcolor(color="#" + self.var_bg_rgb.get(), title=self.tr("pick_bg_title"), parent=self.root)
            if chosen and chosen[1]:
                self.var_bg_rgb.set(chosen[1].lstrip("#").upper())
                self._schedule_preview()

        def _pick_fg_color(self, key: str) -> None:
            chosen = colorchooser.askcolor(color="#" + self.color_vars[key].get(), title=f"{self.tr('pick_fg_title')} {key.upper()}", parent=self.root)
            if chosen and chosen[1]:
                self.color_vars[key].set(chosen[1].lstrip("#").upper())
                self._schedule_preview()

        def _reset_colors(self) -> None:
            for k, v in DEFAULT_COLORS.items():
                self.color_vars[k].set(v)
            self._schedule_preview()

        def _schedule_preview(self) -> None:
            if self._after_preview_id is not None:
                self.root.after_cancel(self._after_preview_id)
            self._after_preview_id = self.root.after(25, self._update_preview_now)

        def _update_color_swatches(self, cfg: OsdConfig) -> None:
            r, g, b = int(cfg.bg_rgb[0:2], 16), int(cfg.bg_rgb[2:4], 16), int(cfg.bg_rgb[4:6], 16)
            fg_txt = "#FFFFFF" if (0.299 * r + 0.587 * g + 0.114 * b) < 140 else "#111111"
            self.btn_bg_color.configure(bg="#" + cfg.bg_rgb, fg=fg_txt, text=f" #{cfg.bg_rgb} ")
            for k, btn in self.color_btns.items():
                hx = self.color_vars[k].get()
                cr, cg, cb = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
                cfg_txt = "#FFFFFF" if (0.299 * cr + 0.587 * cg + 0.114 * cb) < 140 else "#111111"
                btn.configure(bg="#" + hx, fg=cfg_txt)

        def _update_preview_now(self) -> None:
            self._after_preview_id = None
            cfg = self._read_vars_to_cfg()
            layout = compute_layout(cfg, groups=self.groups)
            self.var_alpha_hex_lbl.set(f"{cfg.bg_alpha_pct}% (0x{layout.c7_tag[:2]})")
            edge_txt = self.tr("geom_flush") if cfg.flush_edge else self.tr("geom_float", x=layout.rtss_pos_x, y=layout.rtss_pos_y)
            self.var_geom_lbl.set(self.tr("geom_summary", w=layout.bar_w, h=layout.bar_h, edge=edge_txt, cap=layout.cap_h, groups=len(layout.groups)))
            self._update_color_swatches(cfg)

            img = render_preview_pil(cfg, groups=self.groups, scene=self.var_scene.get())
            self._photo_ref = ImageTk.PhotoImage(img)
            self.preview_canvas.delete("all")
            self.preview_canvas.create_image(0, 0, anchor=tk.NW, image=self._photo_ref)
            self.preview_canvas.configure(scrollregion=(0, 0, img.width, img.height), height=min(100, img.height))

            self.txt_tags.configure(state=tk.NORMAL)
            self.txt_tags.delete("1.0", tk.END)
            self.txt_tags.insert(
                tk.END,
                f"C7={layout.c7_tag} | C8={layout.c8_tag} | RTSS Pos=({layout.rtss_pos_x},{layout.rtss_pos_y}) | "
                f"NameSep={layout.group_name_separator} | DataSep={layout.group_data_separator}\n"
                f"PrologSeparator={layout.prolog_separator}\n"
                f"GroupSeparator={layout.group_separator}",
            )
            self.txt_tags.configure(state=tk.DISABLED)

        def _on_reload(self) -> None:
            self.cfg, self.groups = load_current_config(self.paths, allow_live_mahm=True)
            self._selected_group_idx = 0
            self._sync_vars_from_cfg()
            self._refresh_paths_banner()
            self._schedule_preview()
            self.var_status.set(self.tr("status_reloaded"))

        def _on_apply(self) -> None:
            if not (self.paths.is_valid_ab() and self.paths.is_valid_rtss()):
                messagebox.showerror(self.tr("apply_fail_title"), self.tr("err_missing_paths"), parent=self.root)
                return
            cfg = self._read_vars_to_cfg()
            self.var_status.set(self.tr("status_applying"))
            self.root.update_idletasks()
            try:
                res = apply_config(cfg, self.paths, groups=self.groups)
                self._refresh_paths_banner()
                if res.live_reloaded:
                    self.var_status.set(self.tr("status_applied_live", n=res.rtss_count, frame=res.frame, bak=res.backup_dir.name))
                else:
                    self.var_status.set(self.tr("status_applied_offline", n=res.rtss_count, note=res.note, bak=res.backup_dir.name))
            except Exception as exc:
                self.var_status.set(f"{self.tr('apply_fail_title')}: {exc}")
                messagebox.showerror(self.tr("apply_fail_title"), str(exc), parent=self.root)

        def _on_restore(self) -> None:
            if not has_initial_backup():
                messagebox.showinfo(self.tr("restore_fail_title"), self.tr("restore_missing"), parent=self.root)
                return
            if not messagebox.askyesno(self.tr("restore_btn"), self.tr("restore_confirm"), parent=self.root):
                return
            try:
                cnt = restore_initial_backup(self.paths)
                self._on_reload()
                self.var_status.set(self.tr("status_restored", n=cnt))
            except Exception as exc:
                self.var_status.set(f"{self.tr('restore_fail_title')}: {exc}")
                messagebox.showerror(self.tr("restore_fail_title"), str(exc), parent=self.root)


def run_self_test() -> int:
    paths = discover_install_paths()
    assert I18N["zh"].keys() == I18N["en"].keys(), "I18N keys diverged between zh and en"

    canon = canonical_groups()
    base_cfg = OsdConfig(face="Segoe UI Variable Text Semibold")
    layout_canon = compute_layout(base_cfg, groups=canon)
    assert layout_canon.c7_tag == "B011141A", layout_canon.c7_tag
    assert layout_canon.prolog_separator == '"<P=0,0><L0><M=-1,-1><C7><B=1170,30><C><L1>"'
    assert layout_canon.group_separator == (
        "<P=14,2>:50;<P=236,7><C8><B=1,14><C><P=248,2>:80;"
        "<P=518,7><C8><B=1,14><C><P=530,2>:0;"
        "<P=796,7><C8><B=1,14><C><P=808,2>:31;"
        "<P=1026,7><C8><B=1,14><C><P=1038,2>:91"
    ), layout_canon.group_separator

    for test_cfg in [
        OsdConfig(font_size=14, bar_h=24, flush_edge=True),
        OsdConfig(font_size=22, bar_h=38, flush_edge=True),
        OsdConfig(font_size=18, bar_h=30, flush_edge=False, pos_x=8, pos_y=6),
    ]:
        tl = compute_layout(test_cfg, groups=canon)
        assert tl.rtss_pos_x >= 1 and tl.rtss_pos_y >= 1
        assert tl.bar_w > 0 and tl.bar_h > 0
        assert all(p >= 0 for p in tl.p_groups)
        assert all(d >= 0 for d in tl.d_x)
        assert tl.text_py >= 0 and tl.div_py >= 0

    for scene in ("bright", "dark", "black"):
        im = render_preview_pil(base_cfg, groups=canon, scene=scene)
        assert im.width >= layout_canon.bar_w and im.height >= layout_canon.bar_h

    ab_ok = paths.is_valid_ab()
    rtss_ok = paths.is_valid_rtss()
    rtss_count = len(find_active_rtss_profiles(paths.rtss_profiles_dir)) if rtss_ok and paths.rtss_profiles_dir else 0
    print(
        f"SELF_TEST_OK | AB_Found={ab_ok} | RTSS_Found={rtss_ok} | "
        f"ActiveRTSS={rtss_count} | Bar={layout_canon.bar_w}x{layout_canon.bar_h}"
    )
    return 0


def render_assets(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = OsdConfig(face="Segoe UI Variable Text Semibold")
    canon = canonical_groups()
    for scene, name in [("bright", "preview_v10_bright.png"), ("dark", "preview_v10_dark.png")]:
        im = render_preview_pil(cfg, groups=canon, scene=scene)
        im.save(out_dir / name, format="PNG")
    print(f"RENDERED_ASSETS_OK | dir={out_dir}")
    return 0


def _ensure_dpi_and_admin() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        is_admin = True
    if not is_admin:
        ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            sys.executable,
            f'"{os.path.abspath(__file__)}"',
            os.path.dirname(os.path.abspath(__file__)),
            1,
        )
        sys.exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--render-assets", type=str, default=None)
    args, _ = parser.parse_known_args()

    if args.self_test:
        sys.exit(run_self_test())
    if args.render_assets:
        sys.exit(render_assets(Path(args.render_assets)))

    if not HAS_TK or tk is None:
        _fatal_box("SteamStyle RTSS OSD Tuner", "tkinter is not available in this Python installation.")
        sys.exit(1)

    _ensure_dpi_and_admin()
    root = tk.Tk()
    OsdTunerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
