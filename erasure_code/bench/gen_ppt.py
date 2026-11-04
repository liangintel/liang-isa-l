#!/usr/bin/env python3
"""Build the EC performance deck from Intel-Template.pptx and the data embedded in the HTML reports."""
import copy
import json
import os
import re
import statistics

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData, XyChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_LABEL_POSITION, XL_TICK_LABEL_POSITION
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt, Emu

BENCH = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BENCH, "ec_report.pptx")
EA = "Microsoft YaHei"
BLUE, DARK, GRAY, LIGHT = RGBColor(0x00, 0x68, 0xB5), RGBColor(0x26, 0x26, 0x26), RGBColor(0x52, 0x52, 0x52), RGBColor(0xE9, 0xF1, 0xF8)

PLAT = {  # report id -> tag, colour
    "srfsp": ("SRF", "0068B5"), "cwf": ("CWF", "00C7FD"), "emr06": ("EMR", "D96930"),
    "gnrsp": ("GNR", "8F5DA2"), "turin": ("Turin", "8C8C8C")}
INTEL4 = ["srfsp", "cwf", "emr06", "gnrsp"]
ALL5 = INTEL4 + ["turin"]


def embedded(name):
    html = open(os.path.join(BENCH, name)).read()
    return json.loads(re.search(r"/\*DATA_BEGIN\*/(.*?)/\*DATA_END\*/", html, re.S).group(1))


MAIN = embedded("ec_report.html")
K63 = embedded("ec_report_k6_p3.html")


def nt_results(host):
    """Median of the 3 'cold6' reps for the base and non-temporal builds."""
    r = {"base": [], "nt": []}
    for line in open(os.path.join(BENCH, "results", f"nt_exp_{host}.txt")):
        m = re.match(r"(base|nt) cold6 rep=\d .*enc=(\d+) dec=(\d+)", line)
        if m:
            r[m.group(1)].append((int(m.group(2)), int(m.group(3))))
    return {k: (statistics.median(x[0] for x in v), statistics.median(x[1] for x in v)) for k, v in r.items()}


# ---------- text helpers ----------
def font(f, size=None, bold=None, color=None):
    if size:
        f.size = Pt(size)
    if bold is not None:
        f.bold = bold
    if color is not None:
        f.color.rgb = color
    rPr = f._rPr
    for tag in ("a:latin", "a:ea"):
        e = rPr.find(qn(tag))
        if e is not None:
            rPr.remove(e)
    latin = etree.SubElement(rPr, qn("a:latin"), typeface="Intel Clear")
    latin.addnext(etree.Element(qn("a:ea"), typeface=EA))
    # a:latin/a:ea must precede a:cs, a:sym, a:hlinkClick ...
    for tag in ("a:cs", "a:sym", "a:hlinkClick", "a:hlinkMouseOver", "a:rtl", "a:extLst"):
        e = rPr.find(qn(tag))
        if e is not None:
            rPr.remove(e)
            rPr.append(e)


def write(tf, items, size=16, color=None):
    """items: str or (text, level); text may contain **bold** segments."""
    tf.clear()
    tf.word_wrap = True
    first = True
    for it in items:
        text, lvl = (it, 0) if isinstance(it, str) else it
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.level = lvl
        p.space_after = Pt(4 if lvl else 6)
        for i, seg in enumerate(re.split(r"\*\*", text)):
            if not seg:
                continue
            r = p.add_run()
            r.text = seg
            font(r.font, size - 2 * lvl, bold=(i % 2 == 1), color=color)


def textbox(slide, x, y, w, h, items, size=14, color=None, align=None):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    write(tb.text_frame, items, size, color)
    if align:
        for p in tb.text_frame.paragraphs:
            p.alignment = align
    return tb


def ph(slide, idx):
    return next(p for p in slide.placeholders if p.placeholder_format.idx == idx)


def take(slide, idx):
    """Remove a placeholder and return its box in inches, so a chart or table can go there."""
    p = ph(slide, idx)
    box = tuple(Emu(v).inches for v in (p.left, p.top, p.width, p.height))
    p._element.getparent().remove(p._element)
    return box


def new(prs, layout, title, sub=None):
    s = prs.slides.add_slide(prs.slide_layouts[layout])
    s.shapes.title.text = title
    for r in s.shapes.title.text_frame.paragraphs[0].runs:
        font(r.font)
    if sub is not None:
        write(ph(s, 29).text_frame, [sub], 15, BLUE)
    return s


# ---------- charts and tables ----------
def style_chart(ch, legend=True, size=11):
    ch.font.size = Pt(size)
    font(ch.font, size, color=DARK)
    ch.has_legend = legend
    if legend:
        ch.legend.position = XL_LEGEND_POSITION.BOTTOM
        ch.legend.include_in_layout = False
        font(ch.legend.font, size, color=DARK)


def axis_title(axis, text, size=11):
    axis.has_title = True
    axis.axis_title.text_frame.text = text
    for r in axis.axis_title.text_frame.paragraphs[0].runs:
        font(r.font, size, bold=False, color=GRAY)


def gridlines(axis):
    axis.has_major_gridlines = True
    axis.major_gridlines.format.line.color.rgb = RGBColor(0xD9, 0xD9, 0xD9)
    axis.format.line.color.rgb = RGBColor(0xBF, 0xBF, 0xBF)


def bar_chart(slide, box, cats, series, ytitle, fmt="0.0", labels=True, size=11, gap=60):
    cd = CategoryChartData()
    cd.categories = cats
    for name, vals, _ in series:
        cd.add_series(name, vals)
    x, y, w, h = box
    ch = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(x), Inches(y), Inches(w), Inches(h), cd).chart
    style_chart(ch, legend=len(series) > 1, size=size)
    plot = ch.plots[0]
    plot.gap_width = gap
    plot.overlap = -10
    for s, (_, _, col) in zip(plot.series, series):
        s.format.fill.solid()
        s.format.fill.fore_color.rgb = RGBColor.from_string(col)
    if labels:
        plot.has_data_labels = True
        dl = plot.data_labels
        dl.number_format = fmt
        dl.number_format_is_linked = False
        dl.position = XL_LABEL_POSITION.OUTSIDE_END
        font(dl.font, size - 2, color=DARK)
    gridlines(ch.value_axis)
    ch.value_axis.tick_labels.font.size = Pt(size - 1)
    ch.category_axis.tick_labels.font.size = Pt(size)
    font(ch.category_axis.tick_labels.font, size, color=DARK)
    axis_title(ch.value_axis, ytitle, size)
    return ch


def xy_chart(slide, box, series, xtitle, ytitle, log2=False, ymin=None, ymax=None, xmax=None, size=11):
    cd = XyChartData()
    for name, pts, _ in series:
        s = cd.add_series(name)
        for px, py in pts:
            s.add_data_point(px, py)
    x, y, w, h = box
    ch = slide.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER_LINES, Inches(x), Inches(y), Inches(w), Inches(h), cd).chart
    style_chart(ch, size=size)
    for s, (_, _, col) in zip(ch.plots[0].series, series):
        s.format.line.color.rgb = RGBColor.from_string(col)
        s.format.line.width = Pt(2.25)
        s.marker.format.fill.solid()
        s.marker.format.fill.fore_color.rgb = RGBColor.from_string(col)
        s.marker.format.line.color.rgb = RGBColor.from_string(col)
        s.marker.size = 5
        s.smooth = False
    xa, ya = ch.category_axis, ch.value_axis
    gridlines(ya)
    if log2:
        sc = xa._element.find(qn("c:scaling"))
        sc.insert(0, etree.Element(qn("c:logBase"), val="2"))
    if xmax:
        xa.maximum_scale = xmax
    xa.minimum_scale = 1 if log2 else 0
    if ymin is not None:
        ya.minimum_scale = ymin
    if ymax is not None:
        ya.maximum_scale = ymax
    for a in (xa, ya):
        a.tick_labels.font.size = Pt(size - 1)
    axis_title(xa, xtitle, size)
    axis_title(ya, ytitle, size)
    return ch


def table(slide, box, rows, widths=None, size=11, first_col_left=True, hl_rows=()):
    x, y, w, h = box
    nr, nc = len(rows), len(rows[0])
    t = slide.shapes.add_table(nr, nc, Inches(x), Inches(y), Inches(w), Inches(min(h, 0.32 * nr))).table
    if widths:
        tot = sum(widths)
        for i, cw in enumerate(widths):
            t.columns[i].width = Inches(w * cw / tot)
    for r in range(nr):
        t.rows[r].height = Inches(0.3)
        for c in range(nc):
            cell = t.cell(r, c)
            cell.margin_left = cell.margin_right = Inches(0.05)
            cell.margin_top = cell.margin_bottom = Inches(0.02)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf = cell.text_frame
            tf.word_wrap = True
            tf.text = ""
            run = tf.paragraphs[0].add_run()
            run.text = str(rows[r][c])
            hdr = r == 0
            font(run.font, size, bold=hdr or (r in hl_rows), color=RGBColor(0xFF, 0xFF, 0xFF) if hdr else DARK)
            tf.paragraphs[0].alignment = PP_ALIGN.LEFT if (c == 0 and first_col_left) or hdr else PP_ALIGN.CENTER
            cell.fill.solid()
            cell.fill.fore_color.rgb = BLUE if hdr else (LIGHT if r % 2 == 0 else RGBColor(0xFF, 0xFF, 0xFF))
    return t


def gb(v):
    return v / 1000


# ---------- deck ----------
prs = Presentation(os.path.join(BENCH, "Intel-Template.pptx"))

# 1. title (re-use the template's first slide)
s = prs.slides[0]
s.shapes.title.text = "ISA-L Erasure Code 性能评估"
for r in s.shapes.title.text_frame.paragraphs[0].runs:
    font(r.font, 40)
textbox(s, 2.07, 5.25, 10.8, 1.0, [
    "Xeon 6780E (SRF) · Xeon 6992E+C (CWF) · Xeon 8576C (EMR) · Xeon 6767P (GNR) · EPYC 9655 (Turin)",
    "isa-l 96be773 · erasure_code/bench · 2026-11"], 14, RGBColor(0xFF, 0xFF, 0xFF))

# 2. agenda
s = new(prs, 4, "内容")
write(ph(s, 28).text_frame, [
    "测试平台与方法",
    "单核：k+p、指令集路径、缓存内 vs 内存",
    "多核扩展：数据在缓存内 / 数据在内存里",
    "CWF 满载为什么降频",
    "按 vCPU 对比：E-core vs P-core 超线程",
    "6+3 --cold 6 进程对比（含 AMD Turin）：频率、内存流量、瓶颈",
    "优化实验：非临时存储（去掉 RFO）",
    "结论与建议"], 20)

# 3. platforms
s = new(prs, 5, "测试平台", "所有数据由 erasure_code/bench 下的脚本生成；吞吐均为“有效数据”= isa-l encode 带宽 × k/(k+p)")
box = take(s, 28)
table(s, box, [
    ["", "SRF", "CWF", "EMR", "GNR", "Turin"],
    ["型号", "Xeon 6780E", "Xeon 6992E+C", "Xeon 8576C", "Xeon 6767P", "EPYC 9655"],
    ["微架构", "E-core Crestmont", "E-core Darkmont", "P-core Raptor Cove", "P-core Redwood Cove", "Zen 5"],
    ["核数 / SMT", "2×36 在线 / 无", "1×288 / 无", "2×56 / SMT2", "2×64 / SMT2", "2×96 / SMT2"],
    ["最高频率", "3.0 GHz", "3.2 GHz", "4.0 GHz", "3.9（实测 3.6）GHz", "4.5 GHz"],
    ["EC 默认路径", "avx2_gfni", "avx2_gfni", "avx512_gfni", "avx512_gfni", "avx512_gfni"],
    ["内存", "16×DDR5-6400", "12×DDR5-6400", "16×DDR5-5600", "16×DDR5-6400", "24×DDR5-6400"],
    ["NUMA", "每 socket 1 节点", "SNC3", "SNC2", "SNC2", "NPS4"],
    ["功耗上限", "330 W × 2", "550 W", "385 W × 2", "350 W × 2", "400 W × 2 (TDP)"]],
    widths=[1.3, 2, 2, 2, 2, 2], size=13)
textbox(s, 0.62, 5.6, 12, 1.0, [
    "SNC / NPS：BIOS 把一个 socket 按内存控制器切成多个 NUMA 节点（SNC2 = 2 个，NPS4 = 4 个）；本节点内存延迟更低，但一个节点只用本节点的内存通道。",
    "SRF 每个 4 核 L2 模块只开 1 核；SRF 节点 1 的内存被一个虚拟机占满，跨 socket 的进程只能用远端内存。"], 12, GRAY)

# 4. key findings
s = new(prs, 4, "关键结论")
NT = {h: nt_results(h) for h in ("gnrsp", "cwf", "turin")}
write(ph(s, 28).text_frame, [
    "**单核看指令集**：AVX-512 + GFNI 让 P-core 单核 8+4 有效数据达 EMR 50、GNR 37 GB/s，是 CWF（21）的 1.8–2.4 倍、SRF（13）的约 3–4 倍",
    "**整机纯计算看核数 × 功耗上限**：GNR 4.7 TB/s ＞ CWF 4.3（单 socket）＞ EMR 3.9 ＞ SRF 1.0；CWF/EMR 满载撞 PL1，频率降到 70%–73%",
    "**数据在内存里看内存带宽和 RFO**：每 socket CWF 246 ＞ EMR 179 ＞ GNR 175 ＞ SRF 122 GB/s；E-core 写校验块有额外的 RFO 读流量",
    "**SMT 对 EC 无收益**：缓存内下降 2%–6%，只在内存未饱和时 +12%–17%",
    "**6+3 --cold 6 进程**：Turin 186 ＞ CWF 125 ＞ GNR 116 ≈ EMR 114 ＞ SRF 88 GB/s；所有平台都没有降频，差异来自单核访存能力",
    f"**非临时存储**（isa-l 编译选项 EC_ALIGNED_ADDR）去掉 RFO：GNR cold encode {gb(NT['gnrsp']['base'][0]):.0f} → {gb(NT['gnrsp']['nt'][0]):.0f} GB/s，超过原版 Turin；但 Turin 用后也翻倍到 {gb(NT['turin']['nt'][0]):.0f}"], 17)

# 5. section
s = prs.slides.add_slide(prs.slide_layouts[14])
s.shapes.title.text = "单核性能"
write(ph(s, 29).text_frame, ["k+p、指令集路径、缓存内 vs 内存"], 16, RGBColor(0xFF, 0xFF, 0xFF))

# 6. k+p sweep
s = new(prs, 5, "单核：不同 k+p 配置", "单核，数据在缓存内，chunk = 128 KB ÷ (k+p)；有效数据 GB/s")
box = take(s, 28)
kps = list(MAIN["srfsp"]["s1"].keys())
bar_chart(s, box, kps, [(PLAT[h][0], [gb(MAIN[h]["s1"][k][2]) for k in kps], PLAT[h][1]) for h in INTEL4], "有效数据 GB/s", gap=50, size=11)

# 7. why P-core faster
s = new(prs, 6, "单核：为什么 P-core 快", "8+4，缓存内；每周期字节 = 有效数据 ÷ 频率")
box = take(s, 27)
rows = [["", "SRF", "CWF", "EMR", "GNR"]]
dp = {"srfsp": "avx2_gfni", "cwf": "avx2_gfni", "emr06": "avx512_gfni", "gnrsp": "avx512_gfni"}
v = {h: MAIN[h]["s7"][dp[h]]["8+4"] for h in INTEL4}
rows += [["默认路径"] + [dp[h] for h in INTEL4],
         ["有效数据 GB/s"] + [f"{gb(v[h][2]):.1f}" for h in INTEL4],
         ["频率 MHz"] + [f"{v[h][3]:,.0f}" for h in INTEL4],
         ["IPC"] + [f"{v[h][4]:.2f}" for h in INTEL4],
         ["每周期字节"] + [f"{v[h][2] / v[h][3]:.1f}" for h in INTEL4],
         ["GFNI ÷ 查表路径"] + [f"{MAIN[h]['s7'][dp[h]]['8+4'][0] / MAIN[h]['s7']['avx2']['8+4'][0]:.1f}×" for h in INTEL4]]
table(s, (box[0], box[1] + 0.2, box[2], box[3]), rows, widths=[1.6, 1.1, 1.1, 1.2, 1.2], size=12)
write(ph(s, 28).text_frame, [
    "**GFNI 是基础**：比查表路径快 2.4–3.1 倍；没有 GFNI 时 AVX-512 比 AVX2 没有优势",
    "**AVX-512 + GFNI**：P-core 每周期约 12–13 字节，是 CWF 的 2 倍、SRF 的 3 倍",
    "**GNR 比 EMR 慢约 25%**：AVX-512 + GFNI 时频率只有 3.09 GHz（EMR 3.79），IPC 也低 9%",
    "**CWF 比 SRF 快约 55%**：几乎全部来自 IPC（2.95 vs 2.09），频率只高 7%"], 15)

# 8. cache vs DRAM
s = new(prs, 6, "单核：数据在缓存内 vs 在内存里", "8+4；缓存内 chunk 10,880 B，内存里 chunk 64 MB；有效数据 GB/s")
box = take(s, 27)
bar_chart(s, box, [PLAT[h][0] for h in INTEL4], [
    ("缓存内", [gb(MAIN[h]["s1"]["8+4"][2]) for h in INTEL4], "0068B5"),
    ("内存里（64 MB）", [gb(MAIN[h]["s3"]["8+4"][2]) for h in INTEL4], "00C7FD")], "有效数据 GB/s")
write(ph(s, 28).text_frame, [
    "数据在内存里时，单核吞吐取决于**单核能同时挂起多少内存请求**，不再取决于算力",
    "EMR 从 50 降到 11.8 GB/s（−76%），GNR 从 37 降到 12.6",
    "CWF 单核 14.5 GB/s 反而最高，P-core 的算力优势基本消失",
    "chunk 建议控制在 64 KB 以内，让 k+p 个块留在 L2（EMR/GNR 的 L2 为 2 MB/核）"], 15)

# 9. section
s = prs.slides.add_slide(prs.slide_layouts[14])
s.shapes.title.text = "多核扩展"
write(ph(s, 29).text_frame, ["从 1 个物理核到全部物理核，每核 1 个 8+4 进程"], 16, RGBColor(0xFF, 0xFF, 0xFF))

# 10. multi-core cache
s = new(prs, 6, "多核：数据在缓存内（纯计算）", "8+4，每核 1 个进程；横轴为物理核数（对数刻度）")
box = take(s, 27)
s6 = lambda h, wl: [r for r in MAIN[h]["s6"] if r["wl"] == wl and r["tpc"] == 1]
xy_chart(s, box, [(PLAT[h][0], [(r["cores"], r["data"] / 1e6) for r in s6(h, "cache")], PLAT[h][1]) for h in INTEL4],
         "物理核数", "有效数据 TB/s", log2=True, xmax=512)
rows = [["", "满载核数", "TB/s", "频率 单核→满载", "功耗 / PL1", "扩展效率"]]
for h in INTEL4:
    a, b = s6(h, "cache")[0], s6(h, "cache")[-1]
    pl = {"srfsp": "330×2", "cwf": "550", "emr06": "385×2", "gnrsp": "350×2"}[h]
    rows.append([PLAT[h][0], f"{b['cores']:.0f}", f"{b['data'] / 1e6:.2f}", f"{a['mhz'] / 1000:.2f}→{b['mhz'] / 1000:.2f}", f"{b['pkgw']:.0f} / {pl}",
                 f"{b['data'] / (a['data'] * b['cores']) * 100:.0f}%"])
b2 = take(s, 28)
table(s, (b2[0], b2[1], b2[2], 1.6), rows, widths=[0.8, 1, 0.8, 1.5, 1.3, 1], size=11)
textbox(s, b2[0], b2[1] + 1.85, b2[2], 2.5, [
    "CWF、EMR 满载撞到 PL1，频率降到 73% / 70%，扩展效率正好也是 72% / 70%",
    "GNR 的 AVX-512 代码本来就限在约 3.09 GHz，满载频率不变，整机反而比 EMR 快 21%",
    "能效：CWF 7.8 GB/s/W ＞ GNR 6.7 ＞ EMR 5.1 ＞ SRF 3.0"], 13)

# 11. multi-core DRAM
s = new(prs, 6, "多核：数据在内存里", "8+4，每进程 chunk 16 MB（工作集 256 MB）；横轴为物理核数（对数刻度）")
box = take(s, 27)
xy_chart(s, box, [(PLAT[h][0], [(r["cores"], gb(r["data"])) for r in s6(h, "dram")], PLAT[h][1]) for h in INTEL4],
         "物理核数", "有效数据 GB/s", log2=True, xmax=512)
write(ph(s, 28).text_frame, [
    "**受内存带宽限制**：每 socket 有效数据 CWF 246 ＞ EMR 179 ＞ GNR 175 ＞ SRF 122 GB/s",
    "实测带宽利用率：CWF 81%、EMR 74%、GNR 63%、SRF 59%",
    "**台阶**：SNC 下进程填满一个 NUMA 节点后才进入下一个节点",
    "**RFO**：写校验块前先读入缓存行，E-core 每源字节约 2.0 字节内存流量；P-core 带宽紧张时约 1.5",
    "EMR/GNR 每节点约 16 核、CWF 约 32–64 核就接近内存上限"], 15)

# 12. CWF frequency
s = new(prs, 6, "CWF 满载为什么降频", "8+4 缓存内，按核数逐点扫描：实际频率（perf cycles）")
box = take(s, 27)
F = MAIN["freq"]
xy_chart(s, box, [
    ("连续运行（被烤热）", [(r["n"], r["mhz"]) for r in F["cwf_freq_ec"]], "D96930"),
    ("冷启动", [(r["n"], r["mhz"]) for r in F["cwf_freq_ec_cold"]], "0068B5"),
    ("分散绑核", [(r["n"], r["mhz"]) for r in F["cwf_freq_ec_spread"]], "00C7FD")],
    "活动物理核数", "MHz", ymin=1800, ymax=3400, xmax=300)
write(ph(s, 28).text_frame, [
    "三个上限依次起作用：",
    ("**睿频表**：≤18 核 3.2 GHz，≥55 核 2.9 GHz，全核睿频本来就更低", 1),
    ("**功耗 PL1 = 550 W**：EC 每核功耗高，288 核跑 2.9 GHz 约需 1,100 W；全核压在 550 W、2.33 GHz", 1),
    ("**温度 TjMax 107 °C**：连续运行后 41–44 核就触发热节流，288 核只剩 1.98 GHz", 1),
    "SRF 的睿频表所有档位都是 3.0 GHz，所以看起来“满载不降频”",
    "建议检查参考平台散热配置；EC 多数受内存带宽限制，无需 288 核全开"], 15)

# 13. vCPU
s = new(prs, 6, "按 vCPU 对比：E-core vs P-core 超线程", "10+4；P-core 的 2 个 vCPU = 同一物理核的 2 个超线程；有效数据 GB/s")
cats = ["1 vCPU", "2 vCPU", "3 vCPU", "4 vCPU"]
s8 = lambda h, wl: [next(r["data"] for r in MAIN[h]["s8"] if r["cfg"] == f"v{n}" and r["wl"] == wl) / 1000 for n in range(1, 5)]
for idx, wl, t in ((27, "cache", "缓存内"), (28, "dram", "内存里（64 MB）")):
    box = take(s, idx)
    textbox(s, box[0], box[1] - 0.05, box[2], 0.35, [f"**{t}**"], 14)
    bar_chart(s, (box[0], box[1] + 0.3, box[2], box[3] - 0.3), cats, [(PLAT[h][0], s8(h, wl), PLAT[h][1]) for h in INTEL4], "GB/s", fmt="0", size=10, gap=40)

# 14. section
s = prs.slides.add_slide(prs.slide_layouts[14])
s.shapes.title.text = "6+3 --cold 6 进程对比"
write(ph(s, 29).text_frame, ["parallel -j 6 'taskset -c {1} ./erasure_code_perf -k 6 -p 3 -e 3 -s 128K --cold' ::: 1 9 17 25 33 41"], 13, RGBColor(0xFF, 0xFF, 0xFF))

# 15. 6-process results
s = new(prs, 6, "6 进程结果：总和与换算到单进程", "有效数据 GB/s；每项运行 3 遍，取 3 遍中居中的那一遍")
for idx, t, f in ((27, "6 进程之和", lambda h: gb(K63[h]["cold"]["data"])), (28, "单进程（6 进程之和 ÷ 6）", lambda h: gb(K63[h]["cold"]["data"]) / 6)):
    box = take(s, idx)
    textbox(s, box[0], box[1] - 0.05, box[2], 0.35, [f"**{t}**"], 14)
    bar_chart(s, (box[0], box[1] + 0.3, box[2], box[3] - 0.3), [t], [(PLAT[h][0], [f(h)], PLAT[h][1]) for h in ALL5], "GB/s", size=11, gap=30)\
        .category_axis.tick_label_position = XL_TICK_LABEL_POSITION.NONE

# 16. frequency & power
s = new(prs, 6, "6 进程：频率、功耗、温度", "perf cycles 每 200 ms 采样；turbostat 封装功耗（全部 socket 之和）")
box = take(s, 27)
P = lambda h, p: K63[h]["mon"]["phases"][p]
spec = {"srfsp": "3.0", "cwf": "3.2", "emr06": "4.0", "gnrsp": "3.9 (3.6)", "turin": "4.5"}
rows = [["", "最高 GHz", "encode MHz", "decode MHz", "缓存内 MHz", "功耗 空载→测试 W"]]
for h in ALL5:
    rows.append([PLAT[h][0], spec[h], f"{P(h, 'encode')['mhz']:,.0f}", f"{P(h, 'decode')['mhz']:,.0f}", f"{K63[h]['warm']['mhz']:,.0f}",
                 f"{P(h, 'idle')['pkgw']:.0f} → {P(h, 'encode')['pkgw']:.0f}"])
table(s, (box[0], box[1] + 0.2, box[2], box[3]), rows, widths=[0.8, 1, 1.1, 1.1, 1.1, 1.5], size=12)
write(ph(s, 28).text_frame, [
    "**没有降频**：都跑在 6 核负载下的最高睿频附近，功耗远低于上限，最高温度只有 CWF 65 °C",
    "**SRF encode 2.85 GHz**：IPC 同时升高，每秒指令数和吞吐不变，按指令负载的频率上限（推测）",
    "**P-core 缓存内降到 AVX-512 频率档**：EMR 3.79、GNR 3.10 GHz；cold 测试受访存限制、IPC 低，能跑最高频",
    "Turin 频率只比 EMR 高 11%，每进程吞吐却高 63%：**频率不是主因**"], 15)

# 17. --cold & RFO
s = new(prs, 6, "--cold 实际读写了什么", "encode 阶段全机内存读写带宽（内存控制器 CAS 计数）")
box = take(s, 27)
rows = [["", "encode GB/s", "内存读", "内存写", "预测（读=写=encode×3/9）"]]
for h in ALL5:
    p = P(h, "encode")
    e = K63[h]["mon"]["enc"] / 1000
    rows.append([PLAT[h][0], f"{e:.0f}", f"{p['rd']:.1f}", f"{p['wr']:.1f}", f"{e * 3 / 9:.1f}"])
table(s, (box[0], box[1] + 0.2, box[2], box[3]), rows, widths=[0.8, 1.1, 1, 1, 2], size=12)
write(ph(s, 28).text_frame, [
    "源缓冲区除前 128 KB 外从未写过，读到的是常驻缓存的**内核零页**，源数据不从内存读",
    "**RFO（Read For Ownership）**：普通写指令写不在缓存里的地址时，要先把整条 64 B 缓存行读进来再改写，所以内存读 ≈ 内存写",
    "RFO 在数据返回前占用核心的缺失处理缓冲，每核同时进行的写有限 → **单核写带宽被内存延迟卡住**",
    "decode 还要读 1 个真实的校验块，流量 +17%，所以除 GNR 外 decode 慢 12%–26%"], 14)

# 18. bottleneck
s = new(prs, 6, "6 进程：各平台的瓶颈", "每进程 encode（isa-l 口径，GB/s）")
box = take(s, 27)
avg = lambda h, k: gb(K63[h][k]["enc"]) / K63[h][k]["procs"]
bar_chart(s, box, [PLAT[h][0] for h in ALL5], [
    ("缓存内（计算上限）", [avg(h, "warm") for h in ALL5], "0068B5"),
    ("只跑 1 个进程 cold", [avg(h, "cold1") for h in ALL5], "00C7FD"),
    ("6 进程 cold（原命令）", [avg(h, "cold") for h in ALL5], "D96930")], "GB/s", fmt="0", size=10, gap=40)
write(ph(s, 28).text_frame, [
    "**SRF**：计算受限（cold = 缓存内 24.5）；CPU 41 跨 socket 用远端内存，吞吐减半",
    "**CWF**：接近计算上限（cold 是缓存内的 81%）",
    "**EMR / GNR**：算力是 CWF 的 1.9–2.4 倍，但单核 cold 只有缓存内的 39%–49%，受单核访存并发限制",
    "**Turin**：算力最强（127 GB/s/核），单核访存吞吐约为 Intel 的 2 倍；6 进程时 NPS4 节点（3 通道）带宽用到约 60%"], 14)

# 19. non-temporal store experiment
s = new(prs, 6, "优化实验：非临时存储去掉 RFO", "make -f Makefile.unx D=EC_ALIGNED_ADDR：校验块用 vmovntdq 直接写内存；6+3 --cold 6 进程 encode 之和 GB/s")
box = take(s, 27)
hs = ["cwf", "gnrsp", "turin"]
bar_chart(s, box, [PLAT[h][0] for h in hs], [
    ("原版", [gb(NT[h]["base"][0]) for h in hs], "8C8C8C"),
    ("非临时存储", [gb(NT[h]["nt"][0]) for h in hs], "0068B5")], "encode GB/s", fmt="0", gap=60)
up = lambda h: NT[h]["nt"][0] / NT[h]["base"][0] - 1
write(ph(s, 28).text_frame, [
    f"**GNR** {gb(NT['gnrsp']['base'][0]):.0f} → {gb(NT['gnrsp']['nt'][0]):.0f} GB/s（+{up('gnrsp') * 100:.0f}%），超过原版 Turin；内存读从 52 降到 0.4 GB/s，变成计算受限",
    f"**CWF** +{up('cwf') * 100:.0f}%，达到计算上限",
    f"**Turin** 也 +{up('turin') * 100:.0f}%：这不是 Intel 独有的优化，同样软件下 Turin 仍领先",
    "**代价**：数据还要被读时变慢约 20%；要求 64 B 对齐，否则崩溃；是全库编译期开关，生产中需要按场景分发"], 15)

# 20. conclusions
s = new(prs, 4, "结论与建议")
write(ph(s, 28).text_frame, [
    "**chunk ≤ 64 KB**，让 k+p 个块留在 L2；大块流式编码按“每 socket 120–250 GB/s 有效数据”规划，不必为 EC 预留大量核",
    "**P-core**：单核算力强，适合小块、缓存内的编码；SMT 线程对 EC 无收益，按物理核规划",
    "**E-core**：整机吞吐和能效高（CWF 7.8 GB/s/W），注意睿频表和 PL1；访存密集时把进程分散到不同 L2 模块",
    "**写内存为主的场景**：评估非临时存储（EC_ALIGNED_ADDR），可消除 RFO，GNR 提升 2.3 倍",
    "**跨平台对比**：按 NUMA 节点均匀分配 CPU；--cold 只测了写内存，真实冷数据用 -s 64M 不加 --cold",
    "**后续**：整机 / 整 socket 对比（含 Turin、MRDIMM 平台），以及按场景选择存储方式的分发逻辑"], 17)

# 21. end
prs.slides.add_slide(prs.slide_layouts[28])

prs.save(OUT)
print("saved", OUT, len(prs.slides), "slides")
