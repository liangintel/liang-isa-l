#!/bin/bash
# 实验：用非临时存储（-DEC_ALIGNED_ADDR，校验块用 vmovntdq 直接写内存、不做 RFO）重跑 6+3 --cold 6 进程测试
# 用法：nt_exp.sh <isa-l 源码根目录> <原 erasure_code_perf>
SRC=$1 BASE=$2
CPUS=(1 9 17 25 33 41)
ARGS="-k 6 -p 3 -e 3"
PRIV=; [[ $EUID -ne 0 ]] && PRIV="sudo -n"
B=/tmp/isal_nt
rm -rf $B && mkdir -p $B
tar -C "$SRC" --exclude=.git --exclude=.libs --exclude='*.o' --exclude='*.lo' --exclude=bin -cf - . | tar -xf - -C $B
make -C $B -f Makefile.unx -j"$(nproc)" D=EC_ALIGNED_ADDR erasure_code_perf > /tmp/nt_build.log 2>&1 || { tail /tmp/nt_build.log; exit 1; }
NT=$B/erasure_code_perf

devs=$(ls /sys/bus/event_source/devices)
if grep -q '^uncore_imc' <<< "$devs"; then
	evs="0x05"; [[ " 173 175 221 " == *" $(awk '/^model\s/ {print $NF; exit}' /proc/cpuinfo) "* ]] && evs="0x05 0x06"
	MEMEV=$(for e in $evs; do echo "uncore_imc/event=$e,umask=0xcf/,uncore_imc/event=$e,umask=0xf0/"; done | paste -sd,)
else
	MEMEV=$(for u in $(grep '^amd_umc' <<< "$devs"); do echo "$u/event=0x0a,rdwrmask=1/,$u/event=0x0a,rdwrmask=2/"; done | paste -sd,)
fi

# $1=二进制 $2=进程数 其余为参数；MEM=1 时在 encode 阶段（启动后 1.0–2.5 s）采内存读写带宽
run() {
	local bin=$1 n=$2 c tmp pids=() mem=
	shift 2
	tmp=$(mktemp -d)
	for c in "${CPUS[@]:0:n}"; do taskset -c "$c" "$bin" $ARGS "$@" > "$tmp/out.$c" & pids+=($!); done
	if [[ -n $MEM ]]; then
		sleep 1
		mem=$($PRIV perf stat -x, -a -e "$MEMEV" -- sleep 1.5 2>&1 | awk -F, '/umask=0xcf|rdwrmask=1/ {r += $1} /umask=0xf0|rdwrmask=2/ {w += $1}
			END {printf " enc_rd=%.1f enc_wr=%.1f", r * 64 / 1.5e9, w * 64 / 1.5e9}')
	fi
	wait "${pids[@]}"
	cat "$tmp"/out.* | awk -v n="$n" '/encode/ {e += $(NF-1)} /decode/ {d += $(NF-1)} /Pass/ {ok++}
		END {printf "procs=%d pass=%d enc=%.0f dec=%.0f per_enc=%.0f", n, ok, e, d, e / n}'
	echo "$mem"
	rm -rf "$tmp"
}

echo "# host $(hostname -s)  nt=$NT"
echo "check: $(taskset -c 1 $NT $ARGS -s 128K | tail -1)"
for v in base nt; do
	bin=$BASE; [[ $v == nt ]] && bin=$NT
	for r in 1 2 3; do echo "$v cold6 rep=$r $(run $bin 6 -s 128K --cold)"; done
	echo "$v cold6 mem $(MEM=1 run $bin 6 -s 128K --cold)"
	echo "$v cold1 $(run $bin 1 -s 128K --cold)"
	echo "$v warm6 $(run $bin 6 -s 128K)"
	echo "$v dram6 $(run $bin 6 -s 64M)"
done
