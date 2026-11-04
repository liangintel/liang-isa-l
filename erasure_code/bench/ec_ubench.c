// Micro-benchmarks for the instructions in isa-l's GFNI dot-product kernels.
// Usage: ec_ubench <test> [iterations]; run under `perf stat -e cycles,instructions`.
// Each loop iteration issues 12 independent copies of the instruction (no dependency chains),
// so cycles / (12 * iterations) is the reciprocal throughput.
// Test "kernel" calls ec_encode_data (6+3, 4 KB per block, all in L1) and reports source bytes per cycle.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <erasure_code.h>

static unsigned char buf[4096] __attribute__((aligned(64)));

#define REP12(x) x(0) x(1) x(2) x(3) x(4) x(5) x(6) x(7) x(8) x(9) x(10) x(11)
#define LOOP(body, clobber...)                                                                     \
	for (long i = 0; i < n; i++)                                                               \
		__asm__ volatile(REP12(body) : : "r"(buf) : "memory", clobber)

#define ZREGS "zmm0", "zmm1", "zmm2", "zmm3", "zmm4", "zmm5", "zmm6", "zmm7", "zmm8", "zmm9", "zmm10", "zmm11"
#define YREGS "ymm0", "ymm1", "ymm2", "ymm3", "ymm4", "ymm5", "ymm6", "ymm7", "ymm8", "ymm9", "ymm10", "ymm11"

#define AFF_Z(i) "vgf2p8affineqb $0, %%zmm14, %%zmm15, %%zmm" #i "\n\t"
#define AFF_Y(i) "vgf2p8affineqb $0, %%ymm14, %%ymm15, %%ymm" #i "\n\t"
#define XOR_Z(i) "vpxorq %%zmm14, %%zmm15, %%zmm" #i "\n\t"
#define XOR_Y(i) "vpxor %%ymm14, %%ymm15, %%ymm" #i "\n\t"
#define BC_Z(i) "vpbroadcastq " #i "*8(%0), %%zmm" #i "\n\t"
#define BC_Y(i) "vpbroadcastq " #i "*8(%0), %%ymm" #i "\n\t"
#define LD_Z(i) "vmovdqu64 " #i "*64(%0), %%zmm" #i "\n\t"
#define LD_Y(i) "vmovdqu " #i "*32(%0), %%ymm" #i "\n\t"
// affine + xor pairs as in the kernel (6 pairs = 12 instructions, no loop-carried chain)
#define AX_Z(i) "vgf2p8affineqb $0, %%zmm14, %%zmm15, %%zmm" #i "\n\t" "vpxorq %%zmm" #i ", %%zmm13, %%zmm" #i "\n\t"
#define AX_Y(i) "vgf2p8affineqb $0, %%ymm14, %%ymm15, %%ymm" #i "\n\t" "vpxor %%ymm" #i ", %%ymm13, %%ymm" #i "\n\t"
#define REP6(x) x(0) x(1) x(2) x(3) x(4) x(5)
#define LOOP6(body, clobber...)                                                                    \
	for (long i = 0; i < n; i++)                                                               \
		__asm__ volatile(REP6(body) : : "r"(buf) : "memory", clobber)

int main(int argc, char **argv)
{
	const char *t = argc > 1 ? argv[1] : "";
	long n = argc > 2 ? atol(argv[2]) : 200000000;

	if (0) ;
#ifdef __AVX512F__
	else if (!strcmp(t, "aff_z")) LOOP(AFF_Z, ZREGS);
	else if (!strcmp(t, "xor_z")) LOOP(XOR_Z, ZREGS);
	else if (!strcmp(t, "bc_z")) LOOP(BC_Z, ZREGS);
	else if (!strcmp(t, "ld_z")) LOOP(LD_Z, ZREGS);
	else if (!strcmp(t, "affxor_z")) LOOP6(AX_Z, ZREGS);
#endif
	else if (!strcmp(t, "aff_y")) LOOP(AFF_Y, YREGS);
	else if (!strcmp(t, "xor_y")) LOOP(XOR_Y, YREGS);
	else if (!strcmp(t, "bc_y")) LOOP(BC_Y, YREGS);
	else if (!strcmp(t, "ld_y")) LOOP(LD_Y, YREGS);
	else if (!strcmp(t, "affxor_y")) LOOP6(AX_Y, YREGS);
	else if (!strcmp(t, "kernel")) {
		enum { K = 6, P = 3, LEN = 4096 };
		unsigned char a[(K + P) * K], g[K * P * 32], *s[K], *d[P];
		for (int i = 0; i < K; i++) memset(s[i] = aligned_alloc(64, LEN), i + 1, LEN);
		for (int i = 0; i < P; i++) d[i] = aligned_alloc(64, LEN);
		gf_gen_rs_matrix(a, K + P, K);
		ec_init_tables(K, P, &a[K * K], g);
		for (long i = 0; i < n; i++) ec_encode_data(LEN, K, P, g, s, d);
		printf("bytes=%ld\n", (long) K * LEN * n);
	} else {
		fprintf(stderr, "unknown test\n");
		return 1;
	}
	return 0;
}
