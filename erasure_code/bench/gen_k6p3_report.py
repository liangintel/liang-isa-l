#!/usr/bin/env python3
"""Parse results/*_k6p3.txt (from k6p3_bench.sh) and embed the data into ec_report_k6_p3.html."""
import json
import os
import re
import statistics

BENCH = os.path.dirname(os.path.abspath(__file__))
HOSTS = ["srfsp", "cwf", "emr06", "gnrsp", "turin"]
REPORT = os.path.join(BENCH, "ec_report_k6_p3.html")


def kv(s):
    return {k: float(v) if re.fullmatch(r"[-\d.]+", v) else v for k, v in re.findall(r"(\w+)=(\S+)", s)}


def parse(path):
    r = {"env": {}, "reps": {}, "t0": None, "ev": [], "cyc": [], "mem": [], "tb": [], "tbh": []}
    for line in open(path):
        line = line.rstrip("\n")
        if line.startswith("# "):
            k, _, v = line[2:].partition(":")
            r["env"][k.strip()] = v.strip()
        elif re.match(r"^(cold|cold1|warm|dram) rep=", line):
            name = line.split()[0]
            r["reps"].setdefault(name, []).append(kv(line))
        elif line.startswith("t0 "):
            r["t0"] = float(line.split()[1])
        elif line.startswith("ev "):
            _, cpu, ep, ph, bw = line.split()
            r["ev"].append((int(cpu.split("=")[1]), float(ep), ph, float(bw)))
        elif line.startswith("cyc "):
            _, ep, rel, cpu, ev, cnt = line.split()
            r["cyc"].append((float(ep), float(rel), int(cpu), ev, float(cnt)))
        elif line.startswith("mem "):
            _, ep, rel, rd, wr = line.split()
            r["mem"].append((float(ep), float(rel), float(rd), float(wr)))
        elif line.startswith("tbh "):
            r["tbh"] = line.split()[1:]
        elif line.startswith("tb "):
            p = line.split()
            try:
                r["tb"].append((float(p[1]), dict(zip(r["tbh"], map(float, p[2:])))))
            except ValueError:
                pass
    return r


def median_rep(reps):
    """Rep with the median encode sum, plus the min/max encode and decode over reps."""
    if not reps:
        return None
    s = sorted(reps, key=lambda x: x["enc"])
    m = dict(s[len(s) // 2])
    m["enc_lo"], m["enc_hi"] = s[0]["enc"], s[-1]["enc"]
    ds = sorted(x["dec"] for x in reps)
    m["dec_lo"], m["dec_hi"] = ds[0], ds[-1]
    m["dec"] = statistics.median(ds)
    m["n"] = len(reps)
    return m


def monitor(r):
    t0 = r["t0"]
    # per-CPU perf intervals: (end time rel. t0, dt, cycles, instructions)
    by = {}
    for ep, rel, cpu, ev, cnt in r["cyc"]:
        d = by.setdefault(cpu, {}).setdefault(rel, {"ep": ep})
        d[ev] = cnt
    cpus = sorted(by)
    iv = {}
    for cpu in cpus:
        prev = 0.0
        for rel in sorted(by[cpu]):
            d = by[cpu][rel]
            iv.setdefault(cpu, []).append((d["ep"] - t0, rel - prev, d.get("cycles", 0), d.get("instructions", 0)))
            prev = rel
    mem, prev = [], 0.0
    for ep, rel, rd, wr in r["mem"]:
        mem.append((ep - t0, rel - prev, rd, wr))
        prev = rel
    tb, prev = [], None
    for ep, d in r["tb"]:
        if prev is not None:
            tb.append((ep - t0, ep - prev, d))
        prev = ep

    ev = {}
    for cpu, ep, ph, bw in r["ev"]:
        ev.setdefault(cpu, {})[ph] = (ep - t0, bw)
    enc_t = [ev[c]["encode"][0] for c in ev]
    dec_t = [ev[c]["decode"][0] for c in ev]
    # skip calibration and first-touch page faults (~0.3 s) at the start of each phase
    win = {"idle": (-0.9, -0.05), "encode": (0.6, min(enc_t) - 0.05), "decode": (max(enc_t) + 0.6, min(dec_t) - 0.05)}

    def inside(t, dt, w):
        return t - dt >= w[0] - 1e-3 and t <= w[1] + 1e-3

    phases = {}
    for ph, w in win.items():
        p = {"win": [round(w[0], 2), round(w[1], 2)], "cpu": {}}
        tc = ti = tt = 0.0
        for cpu in cpus:
            sel = [x for x in iv[cpu] if inside(x[0], x[1], w)]
            c, i, t = sum(x[2] for x in sel), sum(x[3] for x in sel), sum(x[1] for x in sel)
            if t:
                p["cpu"][cpu] = {"mhz": c / t / 1e6, "ipc": i / c if c else None}
            tc, ti, tt = tc + c, ti + i, tt + t
        p["mhz"] = tc / tt / 1e6 if tt else None
        p["ipc"] = ti / tc if tc else None
        sel = [x for x in mem if inside(x[0], x[1], w)]
        t = sum(x[1] for x in sel)
        p["rd"] = sum(x[2] for x in sel) / t / 1e9 if t else None
        p["wr"] = sum(x[3] for x in sel) / t / 1e9 if t else None
        sel = [x[2] for x in tb if inside(x[0], x[1], w)]
        p["pkgw"] = statistics.mean(d["PkgWatt"] for d in sel) if sel and "PkgWatt" in sel[0] else None
        p["tmp"] = max(d["PkgTmp"] for d in sel) if sel and "PkgTmp" in sel[0] else None
        p["bzy"] = statistics.mean(d["Bzy_MHz"] for d in sel) if sel and "Bzy_MHz" in sel[0] else None
        phases[ph] = p

    # time series: mean / min / max MHz over the test CPUs, memory GB/s, package W
    rows = {}
    for cpu in cpus:
        for k, (t, dt, c, _) in enumerate(iv[cpu]):
            rows.setdefault(k, []).append((t - dt / 2, c / dt / 1e6 if dt else 0))
    tl_mhz = [[round(statistics.mean(x[0] for x in v), 2), round(statistics.mean(x[1] for x in v)),
               round(min(x[1] for x in v)), round(max(x[1] for x in v))] for _, v in sorted(rows.items())]
    tl_mem = [[round(t - dt / 2, 2), round(rd / dt / 1e9, 2), round(wr / dt / 1e9, 2)] for t, dt, rd, wr in mem if dt]
    tl_pw = [[round(t - dt / 2, 2), d.get("PkgWatt"), d.get("PkgTmp")] for t, dt, d in tb if d.get("PkgWatt", 0) < 5000]
    per = {c: {"enc": ev[c]["encode"][1], "dec": ev[c]["decode"][1],
               "enc_t": round(ev[c]["encode"][0], 2), "dec_t": round(ev[c]["decode"][0], 2)} for c in sorted(ev)}
    return {"phases": phases, "tl_mhz": tl_mhz, "tl_mem": tl_mem, "tl_pw": tl_pw, "per": per,
            "enc": sum(v["enc"] for v in per.values()), "dec": sum(v["dec"] for v in per.values()),
            "enc_end": [round(min(enc_t), 2), round(max(enc_t), 2)], "dec_end": [round(min(dec_t), 2), round(max(dec_t), 2)]}


def main():
    out = {}
    for h in HOSTS:
        path = os.path.join(BENCH, "results", f"{h}_k6p3.txt")
        if not os.path.exists(path):
            continue
        r = parse(path)
        out[h] = {"env": r["env"], **{k: median_rep(v) for k, v in r["reps"].items()}, "mon": monitor(r)}
    html = open(REPORT).read()
    data = json.dumps(out, separators=(",", ":"))
    html = re.sub(r"/\*DATA_BEGIN\*/.*?/\*DATA_END\*/", lambda _: f"/*DATA_BEGIN*/{data}/*DATA_END*/", html, flags=re.S)
    open(REPORT, "w").write(html)
    print("embedded", ", ".join(out), "into", REPORT)


if __name__ == "__main__":
    main()
