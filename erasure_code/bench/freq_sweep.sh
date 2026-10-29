#!/bin/bash
# EC throughput, actual frequency, package power/temperature vs number of active physical cores.
# Usage: erasure_code/bench/freq_sweep.sh   (needs root or passwordless sudo for perf/turbostat/msr)
# Env overrides:
#   NS="1 2 ... "   core counts to test (default: every count from 1 to all physical cores)
#   WL=ec|avx2|base  workload: default ISA dispatch (ec), forced avx2 table-lookup path, or scalar C
#   T=4              encode benchmark seconds per point (sampled from 1.5 s for 2 s)
#   ORDER=packed|spread  packed: cores 0..n-1 in CPU order; spread: n cores evenly spaced over all cores
#   COOL=1           idle seconds before each point (large values measure a cold start instead of a heat-soaked chip)
#   OUT=path         result file (default results/<host>_freq_<WL>[_spread].txt)

BENCH_DIR=$(cd "$(dirname "$0")" && pwd)
ISAL=${ISAL:-$(cd "$BENCH_DIR/../.." && pwd)}
WL=${WL:-ec}
T=${T:-4}
ORDER=${ORDER:-packed}
COOL=${COOL:-1}
OUT=${OUT:-$BENCH_DIR/results/$(hostname -s)_freq_$WL$([[ $ORDER == spread ]] && echo _spread).txt}
PRIV=; [[ $EUID -ne 0 ]] && PRIV="sudo -n"

mapfile -t PHYS < <(lscpu -p=CPU,CORE | grep -v '^#' | awk -F, '!seen[$2]++ {print $1}')
NS=${NS:-$(seq 1 ${#PHYS[@]} | xargs)}

# Build a long-running variant of the chosen workload
make -C "$ISAL" -f Makefile.unx -j"$(nproc)" lib > /tmp/freq_sweep_build.log 2>&1 || { tail /tmp/freq_sweep_build.log; exit 1; }
BIN=$ISAL/bin/freq_sweep.$WL
SRC=$ISAL/erasure_code/erasure_code_perf.c
case $WL in
ec)   K=8; P=4; DEFS=(); ARGS=(-k 8 -p 4 -e 4) ;;
avx2) K=8; P=4; DEFS=(-Dec_encode_data=ec_encode_data_avx2 -Dec_init_tables=ec_init_tables_base); ARGS=(-k 8 -p 4 -e 4) ;;
base) K=10; P=4; DEFS=(); ARGS=(); SRC=$ISAL/erasure_code/erasure_code_base_perf.c ;;
*)    echo "unknown WL=$WL"; exit 1 ;;
esac
gcc -O2 -Wno-deprecated-declarations -DBENCHMARK_TIME="$T" "${DEFS[@]}" -I"$ISAL/include" "$SRC" "$ISAL/bin/isa-l.a" -o "$BIN" || exit 1
$PRIV modprobe msr 2>/dev/null

# IA32_PACKAGE_THERM_STATUS (0x1b1): bit0 thermal, bit2 PROCHOT, bit10 power limit notification, 22:16 degrees below TjMax
pkg_therm() { $PRIV dd if=/dev/cpu/0/msr bs=8 count=1 skip=$((0x1b1)) iflag=skip_bytes status=none 2>/dev/null | od -An -tu8 | tr -d ' '; }
throttle_cnt() { cat /sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count 2>/dev/null || echo 0; }

mkdir -p "$(dirname "$OUT")"
{
echo "# host: $(hostname -s)  cpu: $(lscpu | sed -n 's/^Model name: *//p')  date: $(date -Is)"
echo "# workload: $WL (k=$K p=$P), core order: $ORDER, idle ${COOL}s before each point, encode ${T}s per point, sampled 1.5s..3.5s; $(${PRIV} cat /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw 2>/dev/null | awk '{printf "PL1=%dW", $1/1e6}')"
echo "# n: active physical cores; enc/data: summed MB/s; mhz: avg actual MHz of used CPUs; pkgw: package W (all sockets); pkgtmp: max package C"
echo "# therm: OR of IA32_PACKAGE_THERM_STATUS bits seen (T=thermal P=PROCHOT L=power limit); thr: package throttle events during the point"
for n in $NS; do
	sleep "$COOL"
	tmp=$(mktemp -d) pids=() meas=() cpus=()
	for ((i = 0; i < n; i++)); do
		[[ $ORDER == spread ]] && cpus+=("${PHYS[i * ${#PHYS[@]} / n]}") || cpus+=("${PHYS[i]}")
	done
	list=$(IFS=,; echo "${cpus[*]}")
	thr0=$(throttle_cnt)
	for c in "${cpus[@]}"; do
		taskset -c "$c" stdbuf -oL "$BIN" "${ARGS[@]}" > "$tmp/out.$c" &
		pids+=($!)
	done
	sleep 1.5
	$PRIV perf stat -x, -C "$list" -e cycles -- sleep 2 2> "$tmp/cyc" & meas+=($!)
	$PRIV turbostat --quiet --show PkgWatt,PkgTmp --interval 2 --num_iterations 1 > "$tmp/pwr" 2>/dev/null & meas+=($!)
	bits=0
	for s in 1 2 3 4; do
		v=$(pkg_therm); bits=$((bits | ${v:-0}))
		sleep 0.45
	done
	wait "${meas[@]}"
	until (($(grep -l encode "$tmp"/out.* | wc -l) == n)); do sleep 0.2; done
	kill "${pids[@]}" 2>/dev/null
	wait "${pids[@]}" 2>/dev/null
	thr=$(($(throttle_cnt) - thr0))
	flags=$( ((bits & 1)) && echo -n T; ((bits & 4)) && echo -n P; ((bits & 1024)) && echo -n L)
	enc=$(cat "$tmp"/out.* | awk '/encode/ {e += $(NF-1)} END {printf "%.0f", e}')
	data=$(awk -v e="$enc" -v k=$K -v p=$P 'BEGIN {printf "%.0f", e * k / (k + p)}')
	mhz=$(awk -F, '$3 == "cycles" && $4 > 0 {printf "%.0f", $1 / $4 * 1000}' "$tmp/cyc")
	# turbostat orders columns itself, so pick them by header name
	read -r pkgw pkgtmp < <(awk 'NR == 1 {for (i = 1; i <= NF; i++) c[$i] = i} NR == 2 {print $c["PkgWatt"], $c["PkgTmp"]}' "$tmp/pwr")
	printf "n=%-3d enc=%-8s data=%-8s mhz=%-5s pkgw=%-7s pkgtmp=%-3s therm=%-3s thr=%d\n" \
		"$n" "$enc" "$data" "${mhz:-na}" "${pkgw:-na}" "${pkgtmp:-na}" "${flags:--}" "$thr"
	rm -rf "$tmp"
done
} 2>&1 | tee "$OUT"
