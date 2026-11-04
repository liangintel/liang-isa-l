#!/bin/bash
# 用 perf 采样 ec_ubench kernel（6+3，数据在 L1）的热点，按指令助记符汇总：指令数占比、周期占比
# 需先运行 ubench_run.sh 生成 /tmp/ec_ubench
PRIV=; [[ $EUID -ne 0 ]] && PRIV="sudo -n"
cd /tmp || exit 1
rec() { $PRIV perf record -q -e "$1" -o "/tmp/ann_$2.data" taskset -c 1 /tmp/ec_ubench kernel 300000 > /dev/null 2>&1; }
mix() {
	$PRIV perf annotate -i "/tmp/ann_$1.data" --stdio --percent-type global-period 2>/dev/null |
		awk '$1 ~ /^[0-9.]+$/ && $2 == ":" {m = $4; p[m] += $1} END {for (m in p) printf "%s %.1f\n", m, p[m]}'
}
cev=cycles:pp; rec $cev cyc || { cev=cycles; rec $cev cyc; }
iev=instructions:pp; rec $iev ins || { iev=instructions; rec $iev ins; }
echo "# host $(hostname -s); events $cev / $iev"
$PRIV perf report -i /tmp/ann_cyc.data --stdio --sort sym 2>/dev/null | grep -E '^ +[0-9]' | head -3
join -a1 -a2 -e 0 -o 0,1.2,2.2 <(mix ins | sort) <(mix cyc | sort) |
	awk '$2 + $3 >= 0.5 {printf "%-16s %6.1f %6.1f\n", $1, $3, $2}' | sort -k2 -rn |
	awk 'BEGIN {print "mnemonic         cycles%  instr%"} {print}'
