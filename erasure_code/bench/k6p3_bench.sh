#!/bin/bash
# isa-l EC 6+3 冷数据多进程测试：在 CPUS 的每个 CPU 上同时各跑 1 个 erasure_code_perf，等价于
#   parallel -j 6 'taskset -c {1} ./erasure_code_perf -k 6 -p 3 -e 3 -s 128K --cold' ::: 1 9 17 25 33 41
# 结果写入 results/<主机名>_k6p3.txt，由 gen_k6p3_report.py 生成 ec_report_k6_p3.html
#
# 环境变量：
#   BIN=path    erasure_code_perf（默认仓库根目录下的；autotools 生成的 libtool 包装脚本也可以）
#   CPUS="..."  被测 CPU，默认 "1 9 17 25 33 41"
#   REPS=3      [1][3][4][5] 每项重复次数
#   OUT=path    结果文件
# 需要 root 或免密 sudo（perf 系统计数器、turbostat、dmidecode、MSR）
#
# 测试项：
#   [1] 原命令：6+3，-s 128K --cold
#   [2] 原命令再跑一次，同时每 200ms 采样：各被测 CPU 的 cycles/instructions（实际频率、IPC）、
#       全机内存读/写 CAS 计数、封装功耗和温度（turbostat）
#   [3] 只在第一个 CPU 上跑 1 个进程（--cold），看多进程是否互相影响
#   [4] 6 进程，-s 128K 不加 --cold：数据在缓存内，只看计算能力
#   [5] 6 进程，-s 64M 不加 --cold：源数据全部是真实内存页，对照 --cold 的零页效应

BENCH_DIR=$(cd "$(dirname "$0")" && pwd)
BIN=${BIN:-$(cd "$BENCH_DIR/../.." && pwd)/erasure_code_perf}
read -ra CPUS <<< "${CPUS:-1 9 17 25 33 41}"
REPS=${REPS:-3}
OUT=${OUT:-$BENCH_DIR/results/$(hostname -s)_k6p3.txt}
ARGS="-k 6 -p 3 -e 3"
N=${#CPUS[@]}
LIST=$(IFS=,; echo "${CPUS[*]}")
PRIV=; [[ $EUID -ne 0 ]] && PRIV="sudo -n"
# 监控程序绑到最后一个 CPU，不占用被测 CPU
MON=$(lscpu -p=CPU | grep -v '^#' | tail -1)

# 内存 CAS 计数器（每次 64B）：Intel IMC 读 umask=0xcf、写 umask=0xf0，Xeon 6（model 173/175/221）两个子通道
# 分别是 event 0x05/0x06；AMD 每个 UMC 一个 PMU，event 0x0a，rdwrmask=1 读、2 写
devs=$(ls /sys/bus/event_source/devices)
if grep -q '^uncore_imc' <<< "$devs"; then
	evs="0x05"
	[[ " 173 175 221 " == *" $(awk '/^model\s/ {print $NF; exit}' /proc/cpuinfo) "* ]] && evs="0x05 0x06"
	MEMEV=$(for e in $evs; do echo "uncore_imc/event=$e,umask=0xcf/,uncore_imc/event=$e,umask=0xf0/"; done | paste -sd,)
elif grep -q '^amd_umc' <<< "$devs"; then
	MEMEV=$(for u in $(grep '^amd_umc' <<< "$devs"); do echo "$u/event=0x0a,rdwrmask=1/,$u/event=0x0a,rdwrmask=2/"; done | paste -sd,)
fi

# 在前 n 个被测 CPU 上同时各起 1 个进程，全部结束后输出 encode/decode 之和、单进程最小/最大值，
# 以及 perf stat 统计的各进程平均频率（cycles ÷ task-clock，含进程启动和填充数据）和 IPC
run_set() {
	local n=$1 tmp c pids=()
	shift
	tmp=$(mktemp -d)
	for c in "${CPUS[@]:0:n}"; do
		$PRIV perf stat -x, -o "$tmp/st.$c" -e cycles,instructions,task-clock taskset -c "$c" "$BIN" $ARGS "$@" > "$tmp/out.$c" &
		pids+=($!)
	done
	wait "${pids[@]}"
	cat "$tmp"/out.* | awk -v n="$n" '
		/encode/ {v = $(NF-1); e += v; if (!emin || v < emin) emin = v; if (v > emax) emax = v}
		/decode/ {v = $(NF-1); d += v; if (!dmin || v < dmin) dmin = v; if (v > dmax) dmax = v}
		/Pass/ {ok++}
		END {printf "procs=%d pass=%d enc=%.0f dec=%.0f data=%.0f enc_min=%.0f enc_max=%.0f dec_min=%.0f dec_max=%.0f ",
			n, ok, e, d, e * 6 / 9, emin, emax, dmin, dmax}'
	$PRIV cat "$tmp"/st.* | awk -F, '$3 ~ /^cycles/ {c += $1} $3 ~ /^instructions/ {i += $1} $3 ~ /^task-clock/ {t += $1}
		END {if (c && t) printf "mhz=%.0f ipc=%.2f", c / t / 1000, i / c}'
	echo
	$PRIV rm -rf "$tmp"
}
reps() {
	local name=$1 r
	shift
	for ((r = 1; r <= REPS; r++)); do printf "%s rep=%d " "$name" "$r"; run_set "$@"; done
}

# 给每行加上 epoch 时间戳，用于对齐各监控数据和进程输出
stamp() { while IFS= read -r l; do echo "$EPOCHREALTIME $l"; done; }

# [2]：先采 1s 空闲基线再起进程；监控固定跑 DUR 秒（encode/decode 各约 3s，加上校准和缺页）
monitor() {
	local tmp c pids=() mons=() t0 DUR=12
	tmp=$(mktemp -d)
	$PRIV taskset -c "$MON" perf stat -x, -I 200 -A -C "$LIST" -e cycles,instructions -- sleep $DUR 2>&1 >/dev/null |
		taskset -c "$MON" bash -c "$(declare -f stamp); stamp" > "$tmp/cyc" &
	mons+=($!)
	if [[ -n $MEMEV ]]; then
		$PRIV taskset -c "$MON" perf stat -x, -I 200 -a -e "$MEMEV" -- sleep $DUR 2>&1 >/dev/null |
			taskset -c "$MON" bash -c "$(declare -f stamp); stamp" > "$tmp/mem" &
		mons+=($!)
	fi
	$PRIV taskset -c "$MON" turbostat --quiet -S --show Bzy_MHz,PkgWatt,PkgTmp --interval 0.2 \
		--num_iterations $((DUR * 5)) 2>/dev/null | taskset -c "$MON" bash -c "$(declare -f stamp); stamp" > "$tmp/tb" &
	mons+=($!)
	sleep 1
	t0=$EPOCHREALTIME
	for c in "${CPUS[@]}"; do
		taskset -c "$c" stdbuf -oL "$BIN" $ARGS -s 128K --cold | stamp > "$tmp/out.$c" &
		pids+=($!)
	done
	wait "${pids[@]}"
	wait "${mons[@]}"
	echo "t0 $t0"
	for c in "${CPUS[@]}"; do
		awk -v c="$c" '/encode|decode/ {printf "ev cpu=%s %s %s %s\n", c, $1, ($2 ~ /encode/ ? "encode" : "decode"), $(NF-1)}' "$tmp/out.$c"
	done
	# 每行是 "epoch perf的CSV"；perf -I 的 CSV：相对时间,[CPUn,]计数,单位,事件,...
	awk '{ep = $1; sub(/^[^ ]+ +/, ""); n = split($0, f, ",")}
		f[2] ~ /^CPU/ && f[3] ~ /^[0-9]+$/ {printf "cyc %s %s %s %s %s\n", ep, f[1], substr(f[2], 4), f[5], f[3]}' "$tmp/cyc"
	[[ -n $MEMEV ]] && awk '{ep = $1; sub(/^[^ ]+ +/, ""); split($0, f, ",")}
		f[2] ~ /^[0-9]+$/ {
			t = f[1]
			if (!(t in e)) {e[t] = ep; ord[++n] = t}
			if ($0 ~ /umask=0xcf|rdwrmask=1/) rd[t] += f[2]; else wr[t] += f[2]
		} END {for (i = 1; i <= n; i++) {t = ord[i]; printf "mem %s %s %.0f %.0f\n", e[t], t, rd[t] * 64, wr[t] * 64}}' "$tmp/mem"
	awk 'NR == 1 {$1 = "tbh"; print; next} {$1 = "tb " $1; print}' "$tmp/tb"
	rm -rf "$tmp"
}

msr() { $PRIV dd if=/dev/cpu/0/msr bs=8 count=1 skip=$(($1)) iflag=skip_bytes 2>/dev/null | od -An -tx8 | tr -d ' '; }

[[ -x $BIN ]] || { echo "BIN not found: $BIN"; exit 1; }
mkdir -p "$(dirname "$OUT")"
$PRIV modprobe msr 2>/dev/null

{
echo "# host:    $(hostname -s)   date: $(date -Is)"
echo "# cpu:     $(lscpu | sed -n 's/^Model name: *//p' | head -1)"
echo "# bios:    $(lscpu | sed -n 's/^BIOS Model name: *//p' | head -1 | xargs)"
echo "# topo:    $(lscpu | grep -E '^(Socket|Core|Thread|NUMA node\(s\))' | sed 's/  \+/ /g' | paste -sd ';')"
echo "# cache:   $(lscpu | grep -E '^L[23] cache' | sed 's/  \+/ /g' | paste -sd ';')"
echo "# flags:   $(grep -owE 'avx2|avx512f|avx512bw|avx512vl|gfni|vpclmulqdq' /proc/cpuinfo | sort -u | xargs)"
echo "# freq:    max $(lscpu | sed -n 's/^CPU max MHz: *//p') MHz; base $(cat /sys/devices/system/cpu/cpu0/cpufreq/base_frequency 2>/dev/null) kHz; driver $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver 2>/dev/null); governor $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null); no_turbo $(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null); boost $(cat /sys/devices/system/cpu/cpufreq/boost 2>/dev/null)"
grep -q GenuineIntel /proc/cpuinfo && echo "# trl:     ratios $(msr 0x1AD) cores $(msr 0x1AE)"
echo "# memory:  $($PRIV dmidecode -t memory 2>/dev/null | grep -E '^\s+(Size: [0-9]|Type: DDR|Configured Memory Speed: [0-9])' | sed 's/^[[:space:]]*//' | sort | uniq -c | sed 's/^ *//' | paste -sd ';')"
echo "# thp:     enabled $(cat /sys/kernel/mm/transparent_hugepage/enabled); use_zero_page $(cat /sys/kernel/mm/transparent_hugepage/use_zero_page 2>/dev/null)"
echo "# sw:      kernel $(uname -r); $(gcc --version | head -1); $(nasm -v 2>/dev/null)"
echo "# isal:    $(git -C "$(dirname "$(readlink -f "$BIN")")" log -1 --format='%h %s' 2>/dev/null); bin $BIN"
echo "# place:   $(lscpu -p=CPU,CORE,SOCKET,NODE,CACHE | grep -v '^#' | awk -F, -v l=",$LIST," 'index(l, "," $1 ",") {split($5, c, ":"); printf "%s:%s:%s:%s:%s ", $1, $3, $4, c[3], c[4]}')"
echo "# memev:   ${MEMEV:0:120}"
echo "# reps:    $REPS; cpus $LIST; monitor cpu $MON"

echo "--- [1] $N procs, 6+3 e=3, -s 128K --cold ---"
reps cold $N -s 128K --cold
echo "--- [2] monitored run of [1] ---"
monitor
echo "--- [3] 1 proc on cpu ${CPUS[0]}, -s 128K --cold ---"
reps cold1 1 -s 128K --cold
echo "--- [4] $N procs, -s 128K warm (no --cold) ---"
reps warm $N -s 128K
echo "--- [5] $N procs, -s 64M warm (all buffers real memory) ---"
reps dram $N -s 64M
} 2>&1 | tee "$OUT"
