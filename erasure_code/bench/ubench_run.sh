#!/bin/bash
# 运行 ec_ubench.c：各条 GFNI 内核指令的吞吐，以及 ec_encode_data 6+3 在 L1 内的每周期字节数
# 用法：ubench_run.sh <isa-l 源码根目录>；结果打印到屏幕。PORTEV="事件列表" 时对 kernel 额外统计这些 PMU 事件
SRC=$1
DIR=$(cd "$(dirname "$0")" && pwd)
PRIV=; [[ $EUID -ne 0 ]] && PRIV="sudo -n"
B=/tmp/isal_ub
rm -rf $B && mkdir -p $B
tar -C "$SRC" --exclude=.git --exclude=.libs --exclude='*.o' --exclude='*.lo' --exclude=bin -cf - . | tar -xf - -C $B
make -C $B -f Makefile.unx -j"$(nproc)" lib > /tmp/ub_build.log 2>&1 || { tail /tmp/ub_build.log; exit 1; }
if grep -qw avx512f /proc/cpuinfo; then F="-mavx2 -mavx512f -mavx512bw -mavx512vl -mgfni"; Z=1; else F="-mavx2 -mgfni"; Z=; fi
gcc -O2 $F -I$B -I$B/include "$DIR/ec_ubench.c" $B/bin/isa-l.a -o /tmp/ec_ubench || exit 1

echo "# host $(hostname -s); $(lscpu | sed -n 's/^Model name: *//p' | head -1)"
N=50000000
for t in aff_y xor_y bc_y ld_y affxor_y ${Z:+aff_z xor_z bc_z ld_z affxor_z}; do
	$PRIV perf stat -x, -e cycles,instructions,task-clock -o /tmp/ub.st taskset -c 1 /tmp/ec_ubench $t $N
	$PRIV cat /tmp/ub.st | awk -F, -v t=$t -v n=$N '$3 ~ /^cycles/ {c = $1} $3 ~ /^instructions/ {i = $1} $3 ~ /^task-clock/ {ms = $1}
		END {printf "%-9s per_cycle=%.2f cycles_per_op=%.3f ipc=%.2f mhz=%.0f\n", t, 12 * n / c, c / (12 * n), i / c, c / ms / 1000}'
done
K=400000
$PRIV perf stat -x, -e cycles,instructions,task-clock${PORTEV:+,$PORTEV} -o /tmp/ub.st taskset -c 1 /tmp/ec_ubench kernel $K > /tmp/ub.out
$PRIV cat /tmp/ub.st | awk -F, -v b="$(sed -n 's/bytes=//p' /tmp/ub.out)" '
	$3 ~ /^cycles/ {c = $1} $3 ~ /^instructions/ {i = $1} $3 ~ /^task-clock/ {ms = $1}
	$3 !~ /^(cycles|instructions|task-clock)/ && $1 ~ /^[0-9]+$/ {ev[$3] = $1}
	END {printf "kernel    src_bytes_per_cycle=%.2f ipc=%.2f mhz=%.0f\n", b / c, i / c, c / ms / 1000
		for (e in ev) printf "  %s per_cycle=%.2f\n", e, ev[e] / c}'
