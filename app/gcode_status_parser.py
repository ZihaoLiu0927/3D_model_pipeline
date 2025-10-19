#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gcode_usage_and_footer_json.py — 单次遍历 gcode：
1) 解析头部元数据（; key = value）
2) 统计各挤出头用量（支持 M82/M83、G92、M200 D…、体积/线性E）
3) 抽取尾注统计（filament used / time / cost 等）
统一输出 JSON。
"""

import sys, re, json, os
from math import pi
from typing import List, Dict, Any, Tuple, Iterable

HEADER_MAX_LINES = 2000

# -------- 头部解析（与原逻辑一致，稍作封装） --------
def _to_floats(s: str) -> List[float]:
    out = []
    for p in re.split(r"[;,]\s*", s):
        try:
            if p != "":
                out.append(float(p))
        except:
            pass
    return out

def init_header_state():
    return {
        "raw_meta": {},
        "filament_diameter": [],
        "filament_density": [],
        "use_volumetric_e_header": False,
    }

def feed_header(line: str, header_state: Dict[str, Any], line_idx: int):
    if line_idx >= HEADER_MAX_LINES:
        return
    if not line.startswith(";"):
        return
    kv_re = re.compile(r"^;+\s*([A-Za-z0-9_ ]+?)\s*=\s*(.*)$")
    m = kv_re.match(line.strip())
    if not m:
        return
    k = m.group(1).strip().lower().replace(" ", "_")
    v = m.group(2).strip()
    header_state["raw_meta"][k] = v
    if k == "filament_diameter":
        header_state["filament_diameter"] = _to_floats(v)
    elif k == "filament_density":
        header_state["filament_density"] = _to_floats(v)
    elif k == "use_volumetric_e":
        header_state["use_volumetric_e_header"] = v.strip().lower() in ("1", "true", "yes", "on")

# -------- 尾注统计提取（边读边匹配） --------
FOOTER_PATTERNS = {
    "filament_used_mm": re.compile(r";\s*filament\s+used\s*\[mm\]\s*=\s*([0-9.,+-eE]+)", re.I),
    "filament_used_cm3": re.compile(r";\s*filament\s+used\s*\[cm3\]\s*=\s*([0-9.,+-eE]+)", re.I),
    "total_filament_used_g": re.compile(r";\s*total\s+filament\s+used\s*\[g\]\s*=\s*([0-9.,+-eE]+)", re.I),
    "total_filament_cost": re.compile(r";\s*total\s+filament\s+cost\s*=\s*([0-9.,+-eE]+)", re.I),
    "total_filament_used_for_wipe_tower_g": re.compile(r";\s*total\s+filament\s+used\s+for\s+wipe\s+tower\s*\[g\]\s*=\s*([0-9.,+-eE]+)", re.I),
    "estimated_printing_time_normal": re.compile(r";\s*estimated\s+printing\s+time\s*\(normal\s+mode\)\s*=\s*(.+)", re.I),
    "estimated_first_layer_printing_time_normal": re.compile(r";\s*estimated\s+first\s+layer\s+printing\s+time\s*\(normal\s+mode\)\s*=\s*(.+)", re.I),
}

def init_footer_state():
    return {k: None for k in FOOTER_PATTERNS}

def feed_footer(line: str, footer_state: Dict[str, Any]):
    for key, pat in FOOTER_PATTERNS.items():
        m = pat.search(line)
        if m:
            val = m.group(1).strip()
            val = re.sub(r'[^\d.eE+-]+$', '', val) if "estimated" not in key else val
            footer_state[key] = val

def finalize_footer_types(footer_state: Dict[str, Any]):
    for k in ["filament_used_mm", "filament_used_cm3", "total_filament_used_g",
              "total_filament_cost", "total_filament_used_for_wipe_tower_g"]:
        v = footer_state.get(k)
        if v is not None:
            try:
                footer_state[k] = float(v)
            except ValueError:
                pass
    return footer_state

# -------- 挤出量统计（单次流式） --------
class ExtrusionTally:
    def __init__(self, diam_list: List[float], dens_list: List[float], volumetric_default: bool):
        self.diam_list = diam_list if diam_list else [1.75]
        self.dens_list = dens_list if dens_list else [1.24]
        self.current_tool = 0
        self.E_abs_mode = True
        self.volumetric = volumetric_default  # 受 M200 D… 动态影响
        self.last_E = [0.0] * 16
        self.last_E_valid = [False] * 16
        self.total_len_mm = [0.0] * 16

        # 指令匹配
        self.t_tool = re.compile(r"^\s*T(\d+)\s*(?:;.*)?$")
        self.t_g1 = re.compile(r"^\s*G0?1\b([^;]*)")
        self.t_e = re.compile(r"\bE([-+]?\d*\.?\d+)\b")
        self.t_m82 = re.compile(r"^\s*M82\b")
        self.t_m83 = re.compile(r"^\s*M83\b")
        self.t_g92 = re.compile(r"^\s*G92\b([^;]*)")
        self.t_e_only = re.compile(r"\bE([-+]?\d*\.?\d+)\b")
        self.t_m200 = re.compile(r"^\s*M200\b(.*)$")
        self.t_diam = re.compile(r"\bD([-+]?\d*\.?\d+)\b")

    def _tool_d(self, t: int) -> float:
        return self.diam_list[t] if t < len(self.diam_list) else self.diam_list[-1]

    def _tool_rho(self, t: int) -> float:
        return self.dens_list[t] if t < len(self.dens_list) else self.dens_list[-1]

    def _e_to_len_mm(self, de: float, t: int) -> float:
        if self.volumetric:
            d = self._tool_d(t)
            area = pi * (d / 2.0) ** 2
            return 0.0 if area <= 0 else de / area
        return de

    def feed_line(self, s: str):
        # M200 可能切换 volumetric（某些切片器用 D=直径 代表开启体积E）
        mm200 = self.t_m200.match(s)
        if mm200:
            md = self.t_diam.search(mm200.group(1))
            if md:
                try:
                    dval = float(md.group(1))
                    self.volumetric = dval > 0.0
                except:
                    pass
            return

        mt = self.t_tool.match(s)
        if mt:
            try:
                self.current_tool = int(mt.group(1))
            except:
                self.current_tool = 0
            return

        if self.t_m82.search(s):
            self.E_abs_mode = True
            self.last_E_valid[self.current_tool] = False
            return

        if self.t_m83.search(s):
            self.E_abs_mode = False
            return

        mg92 = self.t_g92.match(s)
        if mg92:
            me = self.t_e_only.search(mg92.group(1))
            if me:
                try:
                    self.last_E[self.current_tool] = float(me.group(1))
                    self.last_E_valid[self.current_tool] = True
                except:
                    pass
            return

        mg1 = self.t_g1.match(s)
        if not mg1:
            return
        me = self.t_e.search(mg1.group(1))
        if not me:
            return
        try:
            e_val = float(me.group(1))
        except:
            return

        if self.E_abs_mode:
            if not self.last_E_valid[self.current_tool]:
                self.last_E[self.current_tool] = e_val
                self.last_E_valid[self.current_tool] = True
                return
            delta = e_val - self.last_E[self.current_tool]
            self.last_E[self.current_tool] = e_val
        else:
            delta = e_val

        self.total_len_mm[self.current_tool] += self._e_to_len_mm(delta, self.current_tool)

    def finalize_report(self) -> Tuple[List[Dict[str, Any]], bool]:
        # 负零修正
        for t in range(16):
            if -1e-9 < self.total_len_mm[t] < 0:
                self.total_len_mm[t] = 0.0

        report = []
        for t in range(16):
            L = self.total_len_mm[t]
            if L <= 0:
                continue
            d = self._tool_d(t)
            rho = self._tool_rho(t)
            area = pi * (d / 2.0) ** 2
            vol_mm3 = L * area
            grams = (vol_mm3 / 1000.0) * rho
            report.append({
                "tool": t,
                "length_mm": round(L, 3),
                "length_m": round(L / 1000.0, 3),
                "mass_g": round(grams, 3)
            })
        return report, self.volumetric

# -------- 单文件处理：一次遍历完成全部统计 --------
def process_gcode_file(path: str) -> Dict[str, Any]:
    header_state = init_header_state()
    footer_state = init_footer_state()

    # 先占位默认值（在读取首行前），挤出统计需要知道 header volumetric 默认
    volumetric_default = False

    # 两阶段：我们仍然能做到“单次遍历文件”的同时，在前 HEADER_MAX_LINES 行内不断刷新 header 默认，
    # 并将“当前已知的 volumetric 默认”传给挤出统计器。
    extruder = ExtrusionTally(
        diam_list=[],
        dens_list=[],
        volumetric_default=volumetric_default,
    )

    with open(path, "r", errors="ignore") as f:
        for idx, line in enumerate(f):
            # 头部喂入
            feed_header(line, header_state, idx)

            # 当我们获取到头部里的直径/密度/是否体积E时，动态刷新挤出统计器配置
            # （为避免创建新对象，直接更新其属性）
            if header_state["filament_diameter"]:
                extruder.diam_list = header_state["filament_diameter"]
            if header_state["filament_density"]:
                extruder.dens_list = header_state["filament_density"]
            # 仅当还未被 M200 改写时，用 header 的 use_volumetric_e 作为初始值
            if idx < HEADER_MAX_LINES and not any(extruder.last_E_valid):
                extruder.volumetric = header_state["use_volumetric_e_header"]

            # 尾注喂入
            feed_footer(line, footer_state)

            # 挤出量统计喂入
            extruder.feed_line(line.strip())

    footer = finalize_footer_types(footer_state)
    report, volumetric_active = extruder.finalize_report()

    # 组织输出
    out = {
        "filament_diameter": header_state["filament_diameter"] or [1.75],
        "filament_density": header_state["filament_density"] or [1.24],
        "header_raw": header_state["raw_meta"],
        "extruders": report,
        "slicer_footer": footer,
    }
    return out

# -------- CLI --------
def main():
    if len(sys.argv) < 2:
        print("Usage: python gcode_usage_and_footer_json.py <file.gcode> [more.gcode ...]")
        sys.exit(1)

    paths = sys.argv[1:]
    for p in paths:
        if not os.path.exists(p):
            print(f"File not found: {p}", file=sys.stderr)
            sys.exit(1)

    if len(paths) == 1:
        result = process_gcode_file(paths[0])
    else:
        result = {p: process_gcode_file(p) for p in paths}

    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
