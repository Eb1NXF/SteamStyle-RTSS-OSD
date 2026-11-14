# -*- coding: utf-8 -*-
"""Exhaustive verification suite for SteamStyle-RTSS-OSD.

All write tests operate exclusively inside ephemeral tempfile directories
and never modify live Afterburner/RTSS installations or open GUI windows.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "steam_osd_tuner.pyw"

_SPEC = importlib.util.spec_from_file_location("steam_osd_tuner", str(SCRIPT_PATH))
assert _SPEC and _SPEC.loader
tuner = importlib.util.module_from_spec(_SPEC)
sys.modules["steam_osd_tuner"] = tuner
_SPEC.loader.exec_module(tuner)


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _make_sample_rtss_global(face: str = "Unispace", height: str = "-8", limit: str = "0") -> bytes:
    lines = [
        "[OSD]",
        "EnableOSD=1",
        "BaseColor=FFFF8000",
        "PositionX=1",
        "PositionY=1",
        "ZoomRatio=2",
        "[Framerate]",
        f"Limit={limit}",
        "[Hooking]",
        "EnableHooking=1",
        "HookLoadLibrary=0",
        "[Font]",
        f"Height={height}",
        "Weight=700",
        f"Face={face}",
        "Load=",
        "",
    ]
    return "\r\n".join(lines).encode("latin1")


def _make_sample_rtss_disabled(limit: str = "144") -> bytes:
    lines = [
        "[OSD]",
        "BaseColor=FFFF0000",
        "PositionX=50",
        "PositionY=50",
        "ZoomRatio=3",
        "[Framerate]",
        f"Limit={limit}",
        "[Hooking]",
        "EnableHooking=0",
        "[Font]",
        "Height=-8",
        "Face=Unispace",
        "",
    ]
    return "\r\n".join(lines).encode("latin1")


def _make_stock_ab_cfg(
    *,
    include_layout1: bool = False,
    osd_layout_val: str = "0",
    tab_padded: bool = True,
    fahrenheit: int = 0,
    enabled_sources: list[tuple[str, str]] | None = None,
    stale_sources: list[str] | None = None,
) -> bytes:
    sep = "\t\t= " if tab_padded else "="
    if enabled_sources is None:
        enabled_sources = [
            ("Framerate", ""),
            ("Framerate 1% Low", ""),
            ("GPU temperature", ""),
            ("GPU usage", ""),
            ("Memory usage", "VRAM"),
            ("Core clock", ""),
            ("Memory clock", "VRAM"),
            ("Power", ""),
            ("CPU temperature", ""),
            ("CPU usage", ""),
            ("CPU clock", ""),
            ("CPU power", ""),
            ("RAM usage", ""),
            ("Frametime", ""),
        ]

    all_sources = [
        "Framerate",
        "Framerate 1% Low",
        "Framerate 0.1% Low",
        "GPU temperature",
        "GPU usage",
        "Fan speed",
        "Memory usage",
        "Core clock",
        "Memory clock",
        "Power",
        "CPU temperature",
        "CPU usage",
        "CPU clock",
        "CPU power",
        "RAM usage",
        "Commit charge",
        "Frametime",
        "CPU1 temperature",
    ]
    for s_name, _ in enabled_sources:
        if s_name not in all_sources:
            all_sources.append(s_name)

    sources_str = ",".join("+" + s for s in all_sources)
    enabled_map = dict(enabled_sources)

    lines = [
        "[Settings]",
        f"Fahrenheit{sep}{fahrenheit}",
        f"OSDLayout{sep}{osd_layout_val}",
        f"SwAutoFanControl{sep}1",
        f"Sources{sep}{sources_str}",
        "",
    ]

    if include_layout1:
        lines.extend(
            [
                "[OSDLayout1]",
                "FormatHeader=<C0=008040><C1=0080C0><C2=C08080><C3=FF0000><C4=FFFFFF><A0=-1><A1=2><S0=-50><S1=50>",
                r"ValueAlignmentTag=<A>;<A4>\r:50,51",
                "UnitsAlignmentTag=<A1>",
                "PrologSeparator=\"\"",
                "GroupNameSeparator=\"  \"",
                "GroupSeparator=<P=0,0>:50;<P=126,0>:80",
                "",
            ]
        )

    for src in all_sources:
        show = "1" if src in enabled_map else "0"
        grp = enabled_map.get(src, "")
        lines.extend(
            [
                f"[Source {src}]",
                f"ShowInOSD={show}",
                f"Group={grp}",
                "Name=",
                "OSDItemType=0",
                "",
            ]
        )

    for stale in stale_sources or []:
        lines.extend(
            [
                f"[Source {stale}]",
                "ShowInOSD=1",
                "Group=",
                "Name=",
                "OSDItemType=0",
                "",
            ]
        )

    lines.append("; preserved celsius marker \xb0C footer")
    return "\r\n".join(lines).encode("latin1")


class TestCoreLayoutAndBinaryRules(unittest.TestCase):
    def test_canonical_v10_parity(self) -> None:
        cfg = tuner.OsdConfig(face="Segoe UI Variable Text Semibold")
        canon = tuner.canonical_groups()
        layout = tuner.compute_layout(cfg, groups=canon)

        self.assertEqual(layout.c7_tag, "B011141A")
        self.assertEqual(layout.c8_tag, "2EFFFFFF")
        self.assertEqual(layout.rtss_pos_x, 1)
        self.assertEqual(layout.rtss_pos_y, 1)
        self.assertEqual(layout.bar_w, 1170)
        self.assertEqual(layout.bar_h, 30)
        self.assertEqual(layout.prolog_separator, '"<P=0,0><L0><M=-1,-1><C7><B=1170,30><C><L1>"')
        self.assertEqual(
            layout.group_separator,
            "<P=14,2>:50;<P=236,7><C8><B=1,14><C><P=248,2>:80;"
            "<P=518,7><C8><B=1,14><C><P=530,2>:0;"
            "<P=796,7><C8><B=1,14><C><P=808,2>:31;"
            "<P=1026,7><C8><B=1,14><C><P=1038,2>:91",
        )

    def test_rtss_binary_invariants_sweep(self) -> None:
        canon = tuner.canonical_groups()
        combos = 0
        for face in tuner.DEFAULT_FACES:
            for font_size in (12, 16, 18, 22, 28):
                for s1_pct in (55, 72, 95):
                    for flush_edge in (True, False):
                        for div_alpha in (0, 18, 65):
                            combos += 1
                            cfg = tuner.OsdConfig(
                                face=face,
                                font_size=font_size,
                                s1_pct=s1_pct,
                                flush_edge=flush_edge,
                                pos_x=0 if flush_edge else 12,
                                pos_y=0 if flush_edge else 8,
                                bar_h=max(20, font_size + 12),
                                div_alpha_pct=div_alpha,
                                outline_factor=32 if div_alpha == 65 else 0,
                            )
                            layout = tuner.compute_layout(cfg, groups=canon)
                            self.assertGreaterEqual(layout.rtss_pos_x, 1)
                            self.assertGreaterEqual(layout.rtss_pos_y, 1)
                            self.assertLessEqual(len(cfg.face.encode("latin1")), 31)
                            self.assertRegex(
                                layout.prolog_separator,
                                r'^"<P=0,0><L0>(<M=-1,-1>)?<C7><B=\d+,\d+><C><L1>"$',
                            )
                            if flush_edge:
                                self.assertIn("<M=-1,-1>", layout.prolog_separator)
                            else:
                                self.assertNotIn("<M=-1,-1>", layout.prolog_separator)

                            p_coords = re.findall(r"<P=(-?\d+),(-?\d+)>", layout.prolog_separator + layout.group_separator)
                            for px_s, py_s in p_coords:
                                self.assertGreaterEqual(int(px_s), 0)
                                self.assertGreaterEqual(int(py_s), 0)

                            if div_alpha == 0:
                                self.assertNotIn("<C8><B=1,", layout.group_separator)
                            else:
                                self.assertEqual(layout.group_separator.count("<C8><B=1,"), len(canon) - 1)

                            # Group start positions must advance strictly past previous divider
                            for idx in range(len(layout.screen_divs)):
                                self.assertLess(layout.screen_groups[idx], layout.screen_divs[idx])
                                self.assertLess(layout.screen_divs[idx], layout.screen_groups[idx + 1])
        self.assertGreaterEqual(combos, 120)

    def test_s1_unit_scale_adjusts_group_width(self) -> None:
        canon = tuner.canonical_groups()
        l_small = tuner.compute_layout(tuner.OsdConfig(s1_pct=55), groups=canon)
        l_mid = tuner.compute_layout(tuner.OsdConfig(s1_pct=72), groups=canon)
        l_large = tuner.compute_layout(tuner.OsdConfig(s1_pct=95), groups=canon)
        self.assertLess(l_small.bar_w, l_mid.bar_w)
        self.assertLess(l_mid.bar_w, l_large.bar_w)

    def test_stock_framerate_order_width_parity(self) -> None:
        canon = tuner.canonical_groups()
        fps_canon = canon[0]
        by_name = {s.src_name: s for s in fps_canon.sensors}
        stock_fps = tuner.OsdGroupSpec(
            id_hex="50",
            label_raw="<APP>",
            preview_label="D3D12",
            color_key="c2",
            sensors=(by_name["Framerate"], by_name["Frametime"], by_name["Framerate 1% Low"]),
            order_tier=0,
            appearance_idx=0,
        )
        cfg = tuner.OsdConfig()
        _, _, scale = tuner.measure_gdi_metrics(cfg.face, cfg.font_size)
        w_canon = tuner._measure_group_width(fps_canon, cfg.face, cfg.font_size, cfg.s1_pct, scale)
        w_stock = tuner._measure_group_width(stock_fps, cfg.face, cfg.font_size, cfg.s1_pct, scale)
        self.assertEqual(w_stock, w_canon)
        self.assertEqual(
            tuner.compute_layout(cfg, groups=[stock_fps, *canon[1:]]).bar_w,
            tuner.compute_layout(cfg, groups=canon).bar_w,
        )


class TestDynamicSensorParser(unittest.TestCase):
    def test_minimal_two_group_without_temperatures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ab_cfg = Path(tmp) / "MSIAfterburner.cfg"
            ab_cfg.write_bytes(
                _make_stock_ab_cfg(
                    enabled_sources=[("Frametime", ""), ("GPU usage", ""), ("CPU usage", "")]
                )
            )
            groups, is_dyn = tuner.parse_ab_osd_groups(ab_cfg)
            self.assertTrue(is_dyn)
            self.assertEqual([g.id_hex for g in groups], ["51", "90", "30"])
            layout = tuner.compute_layout(tuner.OsdConfig(), groups=groups)
            self.assertIn(":51;", layout.group_separator)
            self.assertIn(":90;", layout.group_separator)
            self.assertTrue(layout.group_separator.endswith(":30"))
            self.assertLess(layout.bar_w, 550)

    def test_heavy_six_group_fahrenheit_and_stale_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ab_cfg = Path(tmp) / "MSIAfterburner.cfg"
            ab_cfg.write_bytes(
                _make_stock_ab_cfg(
                    fahrenheit=1,
                    enabled_sources=[
                        ("Framerate", ""),
                        ("Framerate 1% Low", ""),
                        ("Framerate 0.1% Low", ""),
                        ("Frametime", ""),
                        ("CPU temperature", ""),
                        ("CPU usage", ""),
                        ("GPU temperature", ""),
                        ("Fan speed", ""),
                        ("Memory usage", ""),
                        ("Memory clock", ""),
                        ("RAM usage", ""),
                        ("Commit charge", ""),
                        ("CPU1 temperature", "SYS"),
                    ],
                    stale_sources=["GPU1 temperature", "GPU1 usage"],
                )
            )
            groups, _ = tuner.parse_ab_osd_groups(ab_cfg)
            # Stale GPU1 not in Sources= must be excluded; Memory defaults to MEM; custom SYS in tier 5
            labels = [g.label_raw for g in groups]
            self.assertEqual(labels, ["<APP>", "CPU", "GPU", "MEM", "RAM", "SYS"])
            self.assertTrue(any(" °F" in s.unit for g in groups for s in g.sensors))
            layout = tuner.compute_layout(tuner.OsdConfig(), groups=groups)
            self.assertGreater(layout.bar_w, 900)
            self.assertEqual(len(layout.screen_divs), 5)
            img = tuner.render_preview_pil(tuner.OsdConfig(), groups=groups, scene="bright")
            self.assertGreaterEqual(img.width, layout.bar_w)

    def test_zero_enabled_sensors_graceful_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ab_cfg = Path(tmp) / "MSIAfterburner.cfg"
            ab_cfg.write_bytes(_make_stock_ab_cfg(enabled_sources=[]))
            groups, is_dyn = tuner.parse_ab_osd_groups(ab_cfg)
            self.assertTrue(is_dyn)
            self.assertEqual(groups, [])
            layout = tuner.compute_layout(tuner.OsdConfig(), groups=groups)
            self.assertEqual(len(layout.groups), 5)

    def test_preserves_existing_custom_group_separator_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ab_dir = root / "AB"
            rtss_dir = root / "RTSS"
            (ab_dir / "Profiles").mkdir(parents=True)
            (rtss_dir / "Profiles").mkdir(parents=True)
            (rtss_dir / "Profiles" / "Global").write_bytes(_make_sample_rtss_global())

            raw = _make_stock_ab_cfg(include_layout1=False).decode("latin1")
            raw += (
                "\r\n[OSDLayout1]\r\n"
                "FormatHeader=<C0=008040><C1=0080C0><C2=C08080><A0=-1>\r\n"
                'GroupDataSeparator=" "\r\n'
                'GroupNameSeparator="  "\r\n'
                "GroupSeparator=<P=0,0>:50;<P=120,0>:0;<P=250,0>:31;<P=380,0>:80;<P=480,0>:91\r\n"
            )
            ab_cfg = ab_dir / "Profiles" / "MSIAfterburner.cfg"
            ab_cfg.write_bytes(raw.encode("latin1"))

            groups_auto, _ = tuner.parse_ab_osd_groups(ab_cfg, order_mode="auto")
            self.assertEqual([g.id_hex for g in groups_auto], ["50", "0", "31", "80", "91"])

            paths = tuner.InstallPaths(ab_dir=ab_dir, rtss_dir=rtss_dir)
            loaded_cfg, loaded_groups = tuner.load_current_config(paths)
            self.assertEqual([g.id_hex for g in loaded_groups], ["50", "0", "31", "80", "91"])
            self.assertEqual(loaded_cfg.name_spaces, 2)
            self.assertEqual(loaded_cfg.data_spaces, 1)

            groups_steam = tuner.order_groups(groups_auto, "steam")
            self.assertEqual([g.id_hex for g in groups_steam], ["50", "80", "0", "31", "91"])

            groups_ab = tuner.order_groups(groups_auto, "ab")
            self.assertEqual([g.id_hex for g in groups_ab], ["50", "0", "31", "80", "91"])

    def test_stock_ab_without_sources_key_uses_canonical_mahm_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ab_cfg = Path(tmp) / "MSIAfterburner.cfg"
            raw_lines = [
                "[Settings]",
                "Fahrenheit=0",
                "OSDLayout=0",
                "",
                "[Source CPU clock]",
                "ShowInOSD=1",
                "Group=",
                "",
                "[Source CPU power]",
                "ShowInOSD=1",
                "Group=",
                "",
                "[Source Frametime]",
                "ShowInOSD=1",
                "Group=",
                "",
                "[Source Framerate]",
                "ShowInOSD=1",
                "Group=",
                "",
                "[Source Framerate 1% Low]",
                "ShowInOSD=1",
                "Group=",
                "",
                "[Source Memory usage]",
                "ShowInOSD=1",
                "Group=",
                "",
                "[Source Memory clock]",
                "ShowInOSD=1",
                "Group=",
                "",
                "[Source CPU usage]",
                "ShowInOSD=1",
                "Group=",
                "",
                "[Source CPU temperature]",
                "ShowInOSD=1",
                "Group=",
                "",
            ]
            ab_cfg.write_bytes(("\r\n".join(raw_lines) + "\r\n").encode("latin1"))

            groups, is_dyn = tuner.parse_ab_osd_groups(ab_cfg)
            self.assertTrue(is_dyn)
            self.assertEqual([g.label_raw for g in groups], ["<APP>", "CPU", "MEM"])
            fps_group, cpu_group, mem_group = groups
            self.assertEqual([s.src_name for s in fps_group.sensors], ["Framerate", "Frametime", "Framerate 1% Low"])
            self.assertEqual(cpu_group.id_hex, "80")
            self.assertEqual(
                [s.src_name for s in cpu_group.sensors],
                ["CPU temperature", "CPU usage", "CPU clock", "CPU power"],
            )
            self.assertEqual(mem_group.id_hex, "31")
            self.assertEqual([s.src_name for s in mem_group.sensors], ["Memory usage", "Memory clock"])


class TestSandboxedApplyBackupAndRestore(unittest.TestCase):
    def _create_sandbox(
        self,
        root: Path,
        *,
        ab_bytes: bytes,
    ) -> tuple[tuner.InstallPaths, Path, list[Path], list[Path]]:
        ab_dir = root / "Afterburner"
        rtss_dir = root / "RTSS"
        (ab_dir / "Profiles").mkdir(parents=True)
        (rtss_dir / "Profiles").mkdir(parents=True)

        ab_cfg = ab_dir / "Profiles" / "MSIAfterburner.cfg"
        ab_cfg.write_bytes(ab_bytes)

        g_p = rtss_dir / "Profiles" / "Global"
        g_p.write_bytes(_make_sample_rtss_global("Unispace", "-8", "0"))
        game_a = rtss_dir / "Profiles" / "GameA.exe.cfg"
        game_a.write_bytes(_make_sample_rtss_global("Arial", "-9", "165"))
        game_b = rtss_dir / "Profiles" / "GameB.exe.cfg"
        game_b.write_bytes(_make_sample_rtss_disabled("120"))

        return (
            tuner.InstallPaths(ab_dir=ab_dir, rtss_dir=rtss_dir),
            root / "backups",
            [g_p, game_a],
            [game_b],
        )

    def test_fresh_stock_without_osdlayout1_apply_reload_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw_initial_ab = _make_stock_ab_cfg(include_layout1=False, osd_layout_val="0", tab_padded=True)
            paths, bak_root, enabled_rtss, disabled_rtss = self._create_sandbox(
                Path(tmp), ab_bytes=raw_initial_ab
            )
            orig_hashes = {p: _sha256(p) for p in [paths.ab_cfg, *enabled_rtss, *disabled_rtss] if p}

            custom_cfg = tuner.OsdConfig(
                bg_alpha_pct=58,
                bg_rgb="181C24",
                flush_edge=True,
                bar_h=34,
                pad_x=18,
                gap_before=14,
                gap_after=10,
                div_alpha_pct=25,
                face="Segoe UI Semibold",
                font_size=20,
                s1_pct=68,
                outline_factor=16,
                c0="60D080",
                c4="E8ECF2",
            )
            res1 = tuner.apply_config(custom_cfg, paths, backups_root=bak_root, sandbox_mode=True)
            self.assertEqual(res1.rtss_count, 2)
            self.assertTrue(tuner.has_initial_backup(bak_root))

            # Verify binary invariants: no UTF-8 BOM, pure CRLF, preserved °C 0xB0, OSDLayout=1
            assert paths.ab_cfg is not None
            written_ab = paths.ab_cfg.read_bytes()
            self.assertFalse(written_ab.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\xb0C", written_ab)
            self.assertNotIn(b"\n", written_ab.replace(b"\r\n", b""))

            ab_lines = tuner._split_ini_lines(written_ab)
            settings_sec = tuner._parse_section(ab_lines, "Settings")
            layout1_sec = tuner._parse_section(ab_lines, "OSDLayout1")
            self.assertEqual(settings_sec.get("OSDLayout"), "1")
            self.assertEqual(settings_sec.get("SwAutoFanControl"), "1")
            for req_key in (
                "FormatHeader",
                "ValueAlignmentTag",
                "UnitsAlignmentTag",
                "GroupColorTag",
                "ValueColorTag",
                "AlarmColorTag",
                "UnitsColorTag",
                "PrologSeparator",
                "GroupDataSeparator",
                "GroupNameSeparator",
                "GroupSeparator",
                "GraphWidth",
            ):
                self.assertIn(req_key, layout1_sec)
            self.assertEqual(layout1_sec["UnitsAlignmentTag"], "")
            self.assertEqual(layout1_sec["ValueAlignmentTag"], "<A>")

            # Disabled RTSS profile must be untouched; enabled RTSS must keep Limit=
            for dp in disabled_rtss:
                self.assertEqual(_sha256(dp), orig_hashes[dp])
            game_a_lines = tuner._split_ini_lines(enabled_rtss[1].read_bytes())
            self.assertEqual(tuner._parse_section(game_a_lines, "Framerate").get("Limit"), "165")
            self.assertEqual(tuner._parse_section(game_a_lines, "Font").get("CharsetSize1"), "68")

            # Round-trip reload must recover every field faithfully
            loaded_cfg, loaded_groups = tuner.load_current_config(paths)
            self.assertEqual(len(loaded_groups), 5)
            self.assertEqual(loaded_cfg.bg_alpha_pct, 58)
            self.assertEqual(loaded_cfg.bg_rgb, "181C24")
            self.assertTrue(loaded_cfg.flush_edge)
            self.assertEqual(loaded_cfg.bar_h, 34)
            self.assertEqual(loaded_cfg.pad_x, 18)
            self.assertEqual(loaded_cfg.gap_before, 14)
            self.assertEqual(loaded_cfg.gap_after, 10)
            self.assertEqual(loaded_cfg.div_alpha_pct, 25)
            self.assertEqual(loaded_cfg.face, "Segoe UI Semibold")
            self.assertEqual(loaded_cfg.font_size, 20)
            self.assertEqual(loaded_cfg.s1_pct, 68)
            self.assertEqual(loaded_cfg.outline_factor, 16)
            self.assertEqual(loaded_cfg.c0, "60D080")
            self.assertEqual(loaded_cfg.c4, "E8ECF2")

            # 12 subsequent applies -> initial backup stays immutable, rolling keeps max 10
            for i in range(12):
                cfg_i = tuner.OsdConfig(bg_alpha_pct=40 + i, face="Segoe UI Semibold")
                tuner.apply_config(cfg_i, paths, backups_root=bak_root, sandbox_mode=True)
            rolling_dirs = [d for d in (bak_root / "rolling").iterdir() if d.is_dir()]
            self.assertEqual(len(rolling_dirs), 10)
            self.assertEqual(_sha256(bak_root / "initial" / "MSIAfterburner.cfg"), hashlib.sha256(raw_initial_ab).hexdigest())

            # Full restore must return byte-for-byte original SHA-256
            cnt = tuner.restore_initial_backup(paths, backups_root=bak_root, sandbox_mode=True)
            self.assertEqual(cnt, 3)
            for p, h in orig_hashes.items():
                self.assertEqual(_sha256(p), h)

            # Re-applying immediately after restore also succeeds
            res_reapply = tuner.apply_config(tuner.OsdConfig(), paths, backups_root=bak_root, sandbox_mode=True)
            self.assertEqual(res_reapply.rtss_count, 2)

    def test_bom_and_lf_sanitization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bom_lf_ab = b"\xef\xbb\xbf" + _make_stock_ab_cfg(include_layout1=True).replace(b"\r\n", b"\n")
            paths, bak_root, _, _ = self._create_sandbox(Path(tmp), ab_bytes=bom_lf_ab)
            tuner.apply_config(tuner.OsdConfig(), paths, backups_root=bak_root, sandbox_mode=True)
            assert paths.ab_cfg is not None
            out = paths.ab_cfg.read_bytes()
            self.assertFalse(out.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\n", out.replace(b"\r\n", b""))

    def test_live_machine_readonly_sandbox_clone(self) -> None:
        """If Afterburner & RTSS are installed on this host, clone live configs to temp and test."""
        live = tuner.discover_install_paths()
        if not (live.is_valid_ab() and live.is_valid_rtss()):
            self.skipTest("MSI Afterburner / RTSS not installed on this test runner")
        assert live.ab_cfg and live.rtss_profiles_dir

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ab_dir = root / "AB"
            rtss_dir = root / "RTSS"
            (ab_dir / "Profiles").mkdir(parents=True)
            shutil.copytree(live.rtss_profiles_dir, rtss_dir / "Profiles")
            shutil.copy2(live.ab_cfg, ab_dir / "Profiles" / "MSIAfterburner.cfg")

            sb_paths = tuner.InstallPaths(ab_dir=ab_dir, rtss_dir=rtss_dir)
            disabled_cfgs = [
                p
                for p in (rtss_dir / "Profiles").glob("*.cfg")
                if tuner._parse_section(tuner._split_ini_lines(p.read_bytes()), "Hooking").get("EnableHooking") != "1"
            ]
            disabled_before = {p: _sha256(p) for p in disabled_cfgs}

            res = tuner.apply_config(tuner.OsdConfig(), sb_paths, backups_root=root / "bak", sandbox_mode=True)
            self.assertEqual(res.rtss_count, len(tuner.find_active_rtss_profiles(live.rtss_profiles_dir)))
            for p, h in disabled_before.items():
                self.assertEqual(_sha256(p), h)

            restored = tuner.restore_initial_backup(sb_paths, backups_root=root / "bak", sandbox_mode=True)
            self.assertEqual(restored, 1 + res.rtss_count)
            self.assertEqual(_sha256(ab_dir / "Profiles" / "MSIAfterburner.cfg"), _sha256(live.ab_cfg))

    def test_intra_group_name_and_data_spaces_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw_initial_ab = _make_stock_ab_cfg(include_layout1=False, osd_layout_val="0", tab_padded=True)
            paths, bak_root, _enabled_rtss, _disabled_rtss = self._create_sandbox(Path(tmp), ab_bytes=raw_initial_ab)

            base_cfg = tuner.OsdConfig(name_spaces=1, data_spaces=1)
            wide_cfg = tuner.OsdConfig(name_spaces=2, data_spaces=3, pad_x=15, gap_before=16, gap_after=12)
            base_layout = tuner.compute_layout(base_cfg, groups=tuner.canonical_groups())
            wide_layout = tuner.compute_layout(wide_cfg, groups=tuner.canonical_groups())
            self.assertGreater(wide_layout.bar_w, base_layout.bar_w)

            tuner.apply_config(wide_cfg, paths, backups_root=bak_root, sandbox_mode=True)
            assert paths.ab_cfg is not None
            ab_lines = tuner._split_ini_lines(paths.ab_cfg.read_bytes())
            layout1_sec = tuner._parse_section(ab_lines, "OSDLayout1")
            self.assertEqual(layout1_sec.get("GroupNameSeparator"), '"  "')
            self.assertEqual(layout1_sec.get("GroupDataSeparator"), '"   "')

            loaded_cfg, _ = tuner.load_current_config(paths)
            self.assertEqual(loaded_cfg.name_spaces, 2)
            self.assertEqual(loaded_cfg.data_spaces, 3)
            self.assertEqual(loaded_cfg.pad_x, 15)
            self.assertEqual(loaded_cfg.gap_before, 16)
            self.assertEqual(loaded_cfg.gap_after, 12)


class TestPreviewsI18NAndRepoHygiene(unittest.TestCase):
    def test_all_scenes_presets_and_i18n_keys(self) -> None:
        self.assertEqual(set(tuner.I18N["zh"].keys()), set(tuner.I18N["en"].keys()))
        canon = tuner.canonical_groups()
        for _name, alpha, rgb in tuner.PRESETS:
            for scene in ("bright", "dark", "black"):
                cfg = tuner.OsdConfig(bg_alpha_pct=alpha, bg_rgb=rgb)
                im = tuner.render_preview_pil(cfg, groups=canon, scene=scene)
                self.assertEqual(im.mode, "RGB")
                self.assertGreaterEqual(im.width, 1170)
                self.assertGreaterEqual(im.height, 72)

    def test_custom_paths_file_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p_file = Path(tmp) / "paths.json"
            p_ab = Path(tmp) / "AB"
            p_rt = Path(tmp) / "RTSS"
            (p_ab / "Profiles").mkdir(parents=True)
            (p_rt / "Profiles").mkdir(parents=True)
            (p_ab / "Profiles" / "MSIAfterburner.cfg").write_bytes(_make_stock_ab_cfg())
            (p_rt / "Profiles" / "Global").write_bytes(_make_sample_rtss_global())

            tuner.save_custom_paths(tuner.InstallPaths(ab_dir=p_ab, rtss_dir=p_rt), p_file)
            loaded = tuner.discover_install_paths(paths_file=p_file)
            self.assertEqual(loaded.ab_dir, p_ab)
            self.assertEqual(loaded.rtss_dir, p_rt)

    def test_launch_bat_self_test(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows batch launcher test only runs on Windows")
        bat_path = REPO_ROOT / "launch.bat"
        self.assertTrue(bat_path.is_file())
        proc = subprocess.run(
            ["cmd.exe", "/c", "call", str(bat_path), "--self-test"],
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"launch.bat --self-test failed (rc={proc.returncode}): {proc.stdout!r} {proc.stderr!r}",
        )
        self.assertIn(b"SELF_TEST_OK", proc.stdout)

    def test_repo_clean_tree_and_zero_personal_info(self) -> None:
        self.assertTrue((REPO_ROOT / ".gitattributes").is_file(), ".gitattributes must exist")

        # Encoded patterns avoid repeating personal tokens verbatim in source
        encoded_patterns = [
            "dDk4OTA=",          # user account
            "aHNpZWg=",          # surname
            "X3Rvb2xib3g=",      # custom toolbox dir
            "X3Rvb2xzcG9ydGFibGU=",
            "X2dhbWVib3g=",
            "cGMtMDExOA==",      # internal case id
            "aDpcYWk=",          # local workspace drive (backslash)
            "aDovYWk=",          # local workspace drive (slash)
            "6Zu76IWm",          # workspace zh root
            "YmY2LmV4ZQ==",      # specific local game exe
        ]
        forbidden = [base64.b64decode(e).decode("utf-8") for e in encoded_patterns]

        for p in REPO_ROOT.rglob("*"):
            rel = p.relative_to(REPO_ROOT).as_posix()
            self.assertNotIn("__pycache__", rel)
            self.assertFalse(p.name.endswith((".pyc", ".pyo")), f"Unexpected bytecode file: {rel}")
            self.assertFalse(rel.startswith("backups"), f"Unexpected runtime backups folder: {rel}")
            self.assertNotEqual(p.name, "paths.json", "Runtime paths.json should not be present")

            if p.is_file() and p.suffix.lower() not in (".png", ".jpg", ".ico"):
                raw_bytes = p.read_bytes()
                self.assertNotIn(b"\r\r\n", raw_bytes, f"Double CR (\\r\\r\\n) found in {rel}")
                if p.suffix.lower() in (".bat", ".cmd"):
                    self.assertIn(b"\r\n", raw_bytes, f"Batch file must use CRLF: {rel}")
                    self.assertNotIn(b"\n", raw_bytes.replace(b"\r\n", b""), f"Bare LF found in batch file: {rel}")
                text = raw_bytes.decode("utf-8")
                low = text.lower()
                for tok in forbidden:
                    self.assertNotIn(tok, low, f"Personal/local token leaked in {rel}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
