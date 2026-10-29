#!/bin/bash
# ISA-L 纠删码（EC）性能测试：编译 -> 正确性测试 -> 7 组性能测试，结果写入 results/<主机名>.txt
#
# 用法（在仓库任意目录下）：erasure_code/bench/ec_bench.sh
# 可用环境变量覆盖默认值：
#   CPU=2                 单核测试绑定的 CPU
#   SECTIONS="1 2 ... 7"  只跑指定的测试段
#   SCALE_N="1 2 4 ..."   第 6 段使用的物理核数列表（默认：2 的幂、首个 NUMA 节点核数、每 socket 核数、全部核数）
#   ISAS="avx2 ..."       第 7 段对比的指令集路径（默认：本机 CPU 支持的全部）
#   OUT=path              结果文件路径
#
# 带宽口径（isa-l 原生）：encode = (k+p)*chunk/时间，decode = (k+e)*chunk/时间；
# data = encode * k/(k+p)，即每秒编码的用户源数据量。

# ---------- 路径与参数 ----------
# 脚本在 <repo>/erasure_code/bench/，仓库根目录是往上两级
BENCH_DIR=$(cd "$(dirname "$0")" && pwd)
ISAL=${ISAL:-$(cd "$BENCH_DIR/../.." && pwd)}
PERF=$ISAL/erasure_code_perf
BASE=$ISAL/erasure_code_base_perf
CPU=${CPU:-2}
SECTIONS=${SECTIONS:-"1 2 3 4 5 6 7"}
OUT=${OUT:-$BENCH_DIR/results/$(hostname -s).txt}

# CPU 是否支持某个指令集标志
has_flag() { grep -qw "$1" /proc/cpuinfo; }
# 第 $1 段是否在 SECTIONS 里
want() { [[ " $SECTIONS " == *" $1 "* ]]; }

# ---------- 自动识别拓扑（第 6 段用） ----------
# PHYS：每个物理核的第一个硬件线程；SIB：同一物理核的 SMT 兄弟线程（无 SMT 时为空）
# 按 CPU 编号顺序排列，即先填满 socket 0 的各 NUMA 节点，再到 socket 1
mapfile -t PHYS < <(lscpu -p=CPU,CORE | grep -v '^#' | awk -F, '!seen[$2]++ {print $1}')
mapfile -t SIB < <(lscpu -p=CPU,CORE | grep -v '^#' | awk -F, 'seen[$2]++ == 1 {print $1}')
CORES=${#PHYS[@]}
SOCK0=$(lscpu -p=CORE,SOCKET | grep -v '^#' | awk -F, '$2 == 0 && !seen[$1]++' | wc -l)
# socket 0 第一个 NUMA 节点里的物理核数（开 SNC 时小于 SOCK0），用来观察单节点内存带宽饱和点
NODE0=$(lscpu -p=CORE,SOCKET,NODE | grep -v '^#' |
	awk -F, '$2 == 0 && !seen[$1]++ {if (!set) {node = $3; set = 1} if ($3 == node) c++} END {print c}')
if [[ -z $SCALE_N ]]; then
	SCALE_N=$( { for ((n = 1; n < CORES; n *= 2)); do echo $n; done; echo "$NODE0"; echo "$SOCK0"; echo "$CORES"; } | sort -nu | xargs)
fi
# 有 SMT 时第 6 段额外测每个物理核跑 2 个进程
TPCS="1"; ((${#SIB[@]} > 0)) && TPCS="1 2"

# ---------- 自动选择第 7 段的指令集路径 ----------
# 只选本机支持的，否则强制调用会因非法指令崩溃
if [[ -z $ISAS ]]; then
	has_flag avx2 && ISAS="avx2"
	has_flag avx2 && has_flag gfni && ISAS+=" avx2_gfni"
	has_flag avx512f && has_flag avx512bw && ISAS+=" avx512"
	has_flag avx512f && has_flag avx512bw && has_flag gfni && ISAS+=" avx512_gfni"
fi

# ---------- 单核跑一次 erasure_code_perf ----------
# 参数原样传给 erasure_code_perf（-k -p -e -s），输出一行：encode、decode 和 data
# 可用 PERF=... 临时指定其他二进制（第 7 段用）；PSTAT=1 时用 perf stat 追加实际频率 mhz 和每周期指令数 ipc
run() {
	local a=("$@") i k p st=/tmp/ec_bench_pstat.$$ pfx=()
	# 从参数里取出 k、p，用于计算 data
	for ((i = 0; i < ${#a[@]}; i++)); do
		[[ ${a[i]} == -k ]] && k=${a[i+1]}
		[[ ${a[i]} == -p ]] && p=${a[i+1]}
	done
	[[ -n $PSTAT ]] && pfx=($PRIV perf stat -x, -o "$st" -e cycles,instructions,task-clock)
	"${pfx[@]}" taskset -c "$CPU" "$PERF" "$@" | awk -v k="$k" -v p="$p" '
		/encode|decode/ {printf "%s %s %s ", $1, $(NF-1), $NF}
		/encode/ {d = $(NF-1) * k / (k + p)}
		END {printf "data: %.2f MB/s", d}'
	# task-clock 单位为 ms，cycles/ms/1000 即 MHz
	[[ -n $PSTAT ]] && $PRIV cat "$st" | awk -F, '$3 ~ /^cycles/ {c = $1} $3 ~ /^instructions/ {n = $1} $3 ~ /^task-clock/ {t = $1}
		END {if (c && t) printf " mhz=%.0f ipc=%.2f", c / t / 1000, n / c}'
	echo
	[[ -n $PSTAT ]] && $PRIV rm -f "$st"
}

# ---------- 第 6 段的系统计数器 ----------
# 需要 root 或免密 sudo：内存带宽用 perf 读内存控制器 CAS 计数，功耗用 turbostat（perf 的 RAPL 在 SRF 上读数不对）
PRIV=; [[ $EUID -ne 0 ]] && PRIV="sudo -n"
# CAS 读 umask=0xcf、写 umask=0xf0，每次 64B。Xeon 6（GNR=173、SRF=175、CWF=221）的 DDR5 两个子通道
# 分别是 event 0x05/0x06，较旧内核的 cas_count_read 别名只含 0x05，会少算一半
MEMEVS="0x05"
[[ " 173 175 221 " == *" $(awk '/^model\s/ {print $NF; exit}' /proc/cpuinfo) "* ]] && MEMEVS="0x05 0x06"
MEMEV=$(for e in $MEMEVS; do echo "uncore_imc/event=$e,umask=0xcf/,uncore_imc/event=$e,umask=0xf0/"; done | paste -sd,)

# 第 6 段的一个测试点：在前 n 个物理核上各跑 tpc 个进程（tpc=2 时第二个进程在 SMT 兄弟线程上）
# 用 encode 计时 8s 的版本（LONG），启动 5s 后采样 3s，此时所有进程（含 dram 的数据填充较慢者）都在 encode 阶段：
#   mhz = 所用 CPU 的 cycles / 运行时间，即实际平均频率
#   rd/wr = 全机内存读/写带宽 GB/s；pkgw = 全部 socket 封装功耗之和 W
# 所有进程报出 encode 结果后即结束它们，不跑 decode：多进程内存受限时先结束者会让剩余进程的 decode 偏高
LONG=$ISAL/bin/erasure_code_perf.long
scale_point() {
	local wl=$1 tpc=$2 n=$3 sz=$4 i cpus=() pids=() meas=() tmp list
	for ((i = 0; i < n; i++)); do
		cpus+=("${PHYS[i]}")
		((tpc == 2)) && cpus+=("${SIB[i]}")
	done
	list=$(IFS=,; echo "${cpus[*]}")
	tmp=$(mktemp -d)
	for i in "${cpus[@]}"; do
		taskset -c "$i" stdbuf -oL "$LONG" -k 8 -p 4 -e 4 $sz > "$tmp/out.$i" &
		pids+=($!)
	done
	sleep 5
	$PRIV perf stat -x, -C "$list" -e cycles -- sleep 3 2> "$tmp/cyc" & meas+=($!)
	$PRIV perf stat -x, -a -e "$MEMEV" -- sleep 3 2> "$tmp/mem" & meas+=($!)
	$PRIV turbostat --quiet --show PkgWatt --interval 3 --num_iterations 1 > "$tmp/pwr" 2>/dev/null & meas+=($!)
	wait "${meas[@]}"
	until (($(grep -l encode "$tmp"/out.* | wc -l) == ${#cpus[@]})); do sleep 0.5; done
	kill "${pids[@]}" 2>/dev/null
	wait "${pids[@]}" 2>/dev/null
	local tput mhz mem pwr
	tput=$(cat "$tmp"/out.* | awk '/encode/ {e += $(NF-1)} END {printf "enc=%.0f data=%.0f", e, e * 8 / 12}')
	mhz=$(awk -F, '$3 == "cycles" && $4 > 0 {printf "%.0f", $1 / $4 * 1000}' "$tmp/cyc")
	mem=$(awk -F, '/umask=0xcf/ {r += $1} /umask=0xf0/ {w += $1}
		END {printf "rd=%.1f wr=%.1f", r * 64 / 1e9 / 3, w * 64 / 1e9 / 3}' "$tmp/mem")
	pwr=$(awk 'NR == 2 {print $1}' "$tmp/pwr")
	printf "wl=%-5s tpc=%s cores=%-3s procs=%-3s %s mhz=%s %s pkgw=%s\n" \
		"$wl" "$tpc" "$n" "${#cpus[@]}" "$tput" "${mhz:-na}" "$mem" "${pwr:-na}"
	rm -rf "$tmp"
}

# ---------- 编译与正确性测试 ----------
# 用 Makefile.unx 编译（不依赖 autotools/libtool），生成 bin/isa-l.a 和仓库根目录下的测试程序
# 编译失败或 erasure_code_test 不通过则直接退出，不跑性能测试
echo "Building isa-l in $ISAL ..."
make -C "$ISAL" -f Makefile.unx -j"$(nproc)" lib erasure_code_perf erasure_code_base_perf erasure_code_test \
	> /tmp/ec_bench_build.log 2>&1 || { tail -20 /tmp/ec_bench_build.log; exit 1; }
"$ISAL/erasure_code_test" > /tmp/ec_bench_test.log 2>&1 || { tail -5 /tmp/ec_bench_test.log; exit 1; }
mkdir -p "$(dirname "$OUT")"

# 以下所有输出同时打印到屏幕并写入 $OUT
{
# ---------- 环境信息（以 # 开头），便于不同机器结果对比 ----------
# memory 行需要 root 或免密 sudo 才能读 dmidecode，否则为空
echo "# host:    $(hostname -s)   date: $(date -Is)"
echo "# cpu:     $(lscpu | sed -n 's/^Model name: *//p' | head -1)"
echo "# topo:    $(lscpu | grep -E '^(Socket|Core|Thread|NUMA node\(s\))' | sed 's/  \+/ /g' | paste -sd ';')"
echo "# flags:   $(grep -owE 'avx2|avx512f|avx512bw|avx512vl|gfni|vpclmulqdq' /proc/cpuinfo | sort -u | xargs)"
echo "# freq:    max $(lscpu | sed -n 's/^CPU max MHz: *//p') MHz, governor $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
echo "# memory:  $($PRIV dmidecode -t memory 2>/dev/null | grep -E '^\s+(Size: [0-9]|Type: DDR|Configured Memory Speed: [0-9])' | sed 's/^[[:space:]]*//' | sort | uniq -c | sed 's/^ *//' | paste -sd ';')"
echo "# sw:      kernel $(uname -r); $(gcc --version | head -1); $(nasm -v); isa-l $(git -C "$ISAL" rev-parse --short HEAD 2>/dev/null)"
echo "# bench:   single-core cpu $CPU; $CORES physical cores ($SOCK0 per socket, $NODE0 in first NUMA node), SMT siblings ${#SIB[@]}; scale cores: $SCALE_N; procs per core: $TPCS; isas: $ISAS"
echo "# correctness: $(tail -1 /tmp/ec_bench_test.log)"

# [1] 不同 k+p 配置：默认 chunk 下总工作集约 128KB，数据在缓存内，反映纯计算能力；decode 丢 p 块
if want 1; then
echo "--- [1] k+p sweep, warm cache (default ~128KB working set), decode e=p ---"
for kp in "4 2" "6 3" "8 3" "8 4" "10 4" "12 4" "16 4" "20 4" "24 8"; do
	set -- $kp
	printf "k=%-2s p=%-2s : " $1 $2; run -k $1 -p $2 -e $2
done
fi

# [2] 8+4 下 chunk 从 4K 到 1M：观察工作集超出 L1/L2 后的下降
if want 2; then
echo "--- [2] 8+4 chunk size sweep, warm (e=4) ---"
for s in 4K 16K 64K 256K 1M; do
	printf "chunk=%-5s : " $s; run -k 8 -p 4 -e 4 -s $s
done
fi

# [3] 数据在内存里：64M chunk，工作集约 1GB，远大于各机器的 L3（GNR/CWF 的 L3 为 336~576MB）
# 不用 erasure_code_perf 自带的 --cold：它只初始化了第一个 chunk，其余源数据读的是零页，结果偏乐观
if want 3; then
echo "--- [3] DRAM-bound: chunk 64M (working set ~1GB >> L3), decode e=p ---"
for kp in "6 3" "8 4" "12 4" "16 4"; do
	set -- $kp
	printf "k=%-2s p=%-2s : " $1 $2; run -k $1 -p $2 -e $2 -s 64M
done
fi

# [4] 8+4 下丢 1~4 块：decode 计算量与丢失块数 e 成正比
if want 4; then
echo "--- [4] 8+4 decode with fewer erasures, warm ---"
for e in 1 2 3 4; do
	printf "e=%s : " $e; run -k 8 -p 4 -e $e
done
fi

# [5] 纯 C 标量实现作基线：erasure_code_base_perf 固定 10+4，不接受参数，按原样输出
if want 5; then
echo "--- [5] scalar baseline (ec_encode_data_base, 10+4 fixed) ---"
taskset -c "$CPU" "$BASE" | grep -E "perf|_warm|_cold"
fi

# [6] 多核扩展：从 1 个物理核到全部物理核，每核起 1 个（有 SMT 时再测 2 个）独立的 8+4 进程
#   cache：默认 chunk，数据在各核自己的缓存里，只受算力和频率影响
#   dram ：16M chunk（每进程工作集 256MB）。进程少时在 L3 大的机器上部分命中 L3，进程多时数据来自内存，观察内存带宽饱和
if want 6; then
echo "--- [6] multi-core scaling, 8+4: wl=cache (default chunk) / dram (16M chunk); tpc = processes per physical core ---"
echo "# enc/data: summed encode MB/s; mhz: avg actual MHz of used CPUs; rd/wr: system DRAM GB/s; pkgw: total package W"
gcc -O2 -DBENCHMARK_TIME=8 -I"$ISAL/include" "$ISAL/erasure_code/erasure_code_perf.c" "$ISAL/bin/isa-l.a" -o "$LONG"
for wl in cache dram; do
	[[ $wl == dram ]] && sz="-s 16M" || sz=""
	for tpc in $TPCS; do
		for n in $SCALE_N; do
			scale_point $wl $tpc $n "$sz"
		done
	done
done
fi

# [7] 指令集路径对比：ec_encode_data 默认按 CPUID 自动选最优实现，这里绕过自动选择：
# 用 -D 把 ec_encode_data/ec_init_tables 替换成指定版本，重新编译 erasure_code_perf 到 bin/ 下
# GFNI 路径需要 ec_init_tables_gfni 生成的系数表，其他路径用 ec_init_tables_base
if want 7; then
echo "--- [7] forced ISA path comparison (single core) ---"
for isa in $ISAS; do
	bin=$ISAL/bin/erasure_code_perf.$isa
	[[ $isa == *gfni ]] && init=ec_init_tables_gfni || init=ec_init_tables_base
	gcc -O2 -Wno-deprecated-declarations -I"$ISAL/include" \
		-Dec_encode_data=ec_encode_data_$isa -Dec_init_tables=$init \
		"$ISAL/erasure_code/erasure_code_perf.c" "$ISAL/bin/isa-l.a" -o "$bin" || continue
	for args in "-k 8 -p 4 -e 4" "-k 10 -p 4 -e 4" "-k 10 -p 4 -e 4 -s 64M"; do
		printf "%-12s %-22s : " $isa "$args"; PERF=$bin PSTAT=1 run $args
	done
done
fi
} 2>&1 | tee "$OUT"
