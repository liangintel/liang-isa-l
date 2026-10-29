#!/usr/bin/env python3
"""Parse results/*.txt produced by ec_bench.sh and embed them as JSON into ec_report.html and
ec_report_en.html (between the /*DATA_BEGIN*/ and /*DATA_END*/ markers).

Usage: erasure_code/bench/gen_report_data.py
"""
import json
import os
import re

BENCH = os.path.dirname(os.path.abspath(__file__))
NUM = r"([0-9.]+)"


def triple(line):
    """encode, decode, data in MB/s from a single-core result line."""
    enc = re.search(r"encode_warm: " + NUM, line)
    dec = re.search(r"decode_warm: " + NUM, line)
    data = re.search(r"data: " + NUM, line)
    return [float(m.group(1)) if m else None for m in (enc, dec, data)]


def parse(path):
    r = {"head": {}, "s1": {}, "s2": {}, "s3": {}, "s4": {}, "s5": None, "s6": [], "s7": {}, "s8": []}
    sec = None
    for line in open(path):
        line = line.rstrip("\n")
        m = re.match(r"# (\w+):\s+(.*)", line)
        if m and sec is None:
            r["head"][m.group(1)] = m.group(2)
            continue
        m = re.match(r"--- \[(\d+)\]", line)
        if m:
            sec = m.group(1)
            continue
        if sec in ("1", "3"):
            m = re.match(r"k=(\d+)\s+p=(\d+)", line)
            if m:
                r["s" + sec][f"{m.group(1)}+{m.group(2)}"] = triple(line)
        elif sec == "2":
            m = re.match(r"chunk=(\S+)", line)
            if m:
                r["s2"][m.group(1)] = triple(line)
        elif sec == "4":
            m = re.match(r"e=(\d)", line)
            if m:
                r["s4"][m.group(1)] = triple(line)
        elif sec == "5":
            m = re.search(r"base_(encode|decode)_warm:.*= " + NUM + " MB/s", line)
            if m:
                r["s5"] = (r["s5"] or {}) | {m.group(1): float(m.group(2))}
        elif sec == "6" and line.startswith("wl="):
            kv = dict(re.findall(r"(\w+)=(\S+)", line))
            r["s6"].append({k: (v if k == "wl" else float(v) if v != "na" else None) for k, v in kv.items()})
        elif sec == "8" and line.startswith("cfg="):
            kv = dict(re.findall(r"(\w+)=(\S+)", line))
            r["s8"].append({k: (v if k in ("cfg", "cpus", "wl") else float(v)) for k, v in kv.items()})
        elif sec == "7":
            m = re.match(r"(\S+)\s+-k (\d+) -p (\d+) -e \d+(?: -s (\S+))?\s+:", line)
            if m:
                key = f"{m.group(2)}+{m.group(3)}" + (f" {m.group(4)}" if m.group(4) else "")
                mhz = re.search(r"mhz=" + NUM, line)
                ipc = re.search(r"ipc=" + NUM, line)
                r["s7"].setdefault(m.group(1), {})[key] = triple(line) + [
                    float(mhz.group(1)) if mhz else None, float(ipc.group(1)) if ipc else None]
    return r


def parse_freq(path):
    """freq_sweep.sh output: one dict per n= line; therm flags kept as a string."""
    rows = []
    for line in open(path):
        if line.startswith("n="):
            kv = dict(re.findall(r"(\w+)=(\S+)", line))
            row = {k: (v if k == "therm" else float(v) if v != "na" else None) for k, v in kv.items()}
            # turbostat occasionally reports a bogus PkgWatt when the RAPL counter wraps
            if row.get("pkgw") and row["pkgw"] > 5000:
                row["pkgw"] = None
            rows.append(row)
    return rows


def parse_hold(path):
    """Sustained-load turbostat summary table: header line starting with t(s), then one row per interval."""
    rows, head = [], None
    for line in open(path):
        cols = line.split()
        if cols and cols[0] == "t(s)":
            head = cols
        elif head and cols and cols[0].isdigit() and len(cols) == len(head):
            rows.append(dict(zip(head, map(float, cols))))
    return rows


def main():
    res = {"freq": {}, "hold": {}}
    for f in sorted(os.listdir(os.path.join(BENCH, "results"))):
        path = os.path.join(BENCH, "results", f)
        if "_freq_" in f and f.endswith(".txt"):
            res["freq"][f[:-4]] = parse_freq(path)
        elif "_hold" in f and f.endswith(".txt"):
            res["hold"][f[:-4]] = parse_hold(path)
        elif f.endswith(".txt"):
            res[f[:-4]] = parse(path)
    data = "/*DATA_BEGIN*/" + json.dumps(res, separators=(",", ":")) + "/*DATA_END*/"
    for name in ("ec_report.html", "ec_report_en.html"):
        html = os.path.join(BENCH, name)
        if not os.path.exists(html):
            continue
        text, n = re.subn(r"/\*DATA_BEGIN\*/.*?/\*DATA_END\*/", lambda _: data, open(html).read(), flags=re.S)
        if n != 1:
            raise SystemExit(f"data markers not found in {name}")
        open(html, "w").write(text)
        print(f"embedded results of {', '.join(res)} into {html}")


if __name__ == "__main__":
    main()
