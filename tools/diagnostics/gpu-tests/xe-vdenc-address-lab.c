// 实验专用：补齐 Gen12 VDENC 地址命令，仅注入自有编码探针。
#include <mach-o/dyld.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include "xe-vdenc-command-layout.h"

static uintptr_t va_base;
static unsigned char *context;
static unsigned frames;

// 确认候选驱动及两个调用点，避免在其他版本上使用固定对象偏移。
static void image_added(const struct mach_header *header, intptr_t slide) {
    (void)slide;
    const char target[] = "AppleIntelTGLGraphicsVADriver";
    for (uint32_t i = 0; i < _dyld_image_count(); ++i) {
        if (_dyld_get_image_header(i) != header) continue;
        const char *name = _dyld_get_image_name(i);
        size_t length = strlen(name);
        if (length < sizeof target - 1 || strcmp(name + length - sizeof target + 1, target)) continue;
        const unsigned char *bytes = (const unsigned char *)header;
        const unsigned char copy_call[] = {0xe8, 0x9d, 0x8e, 0x21, 0x00};
        const unsigned char flush_call[] = {0xe8, 0xef, 0x6b, 0x21, 0x00};
        if (memcmp(bytes + 0x2557a, copy_call, 5) || memcmp(bytes + 0x277aa, flush_call, 5)) _exit(80);
        va_base = (uintptr_t)header;
    }
}

// 记录 builder 上下文并保留 memcpy/memmove 共用地址的重叠复制语义。
__attribute__((noinline)) void *xe_address_copy(void *destination, const void *source,
                                               size_t size, unsigned char *command) {
    if (va_base && size == 0x8c && (uintptr_t)__builtin_return_address(0) - va_base == 0x2557f)
        context = command;
    volatile unsigned char *out = destination;
    const volatile unsigned char *in = source;
    if ((uintptr_t)out > (uintptr_t)in && (uintptr_t)out - (uintptr_t)in < size) {
        for (size_t i = size; i; --i) out[i - 1] = in[i - 1];
    } else {
        for (size_t i = 0; i < size; ++i) out[i] = in[i];
    }
    return destination;
}

// 从保存寄存器传递 VA 命令对象，原三参数 ABI 保持不变。
__attribute__((naked)) static void *copy_adapter(void *, const void *, size_t) {
    __asm__ volatile("movq %rbx, %rcx\n jmp _xe_address_copy");
}

// 在提交前扩展一条地址命令，实际转换复用经过原始采集数据测试的实现。
__attribute__((noinline)) int xe_address_flush(FILE *stream, unsigned char *encoder) {
    if (va_base && (uintptr_t)__builtin_return_address(0) - va_base == 0x277af) {
        if (!context) _exit(81);
        unsigned char *token = *(unsigned char **)(encoder + 0x6bc8);
        unsigned char *begin = *(unsigned char **)(context + 0x10);
        unsigned char *limit = *(unsigned char **)(context + 0x18);
        unsigned char *reserved = *(unsigned char **)(context + 0x20);
        unsigned char *end = *(unsigned char **)(context + 0x70);
        if (!token || !begin || end < begin || reserved < end || limit < reserved ||
            (size_t)(limit - reserved) < 136) _exit(89);
        size_t bytes = (size_t)(end - begin);
        int status = xe_vdenc_expand_address(begin, (size_t)(limit - begin), &bytes, token, 0xce8);
        if (status) {
            fprintf(stderr, "GEN12-ADDR rejected frame=%u status=%d bytes=%zu token_words=%u source_words=%u slice_words=%u\n",
                frames, status, bytes, *(uint32_t *)(token + 0xce0),
                *(uint32_t *)(token + 0xc1c), *(uint32_t *)(token + 0xc20));
            _exit(90 + status);
        }
        *(unsigned char **)(context + 0x20) = reserved + 136;
        *(unsigned char **)(context + 0x70) = begin + bytes;
        fprintf(stderr, "GEN12-ADDR frame=%u bytes=%zu\n", frames++, bytes);
    }
    return fflush(stream);
}

// 提取编码器对象并转交普通 C 函数，避免修改驱动本身。
__attribute__((naked)) static int flush_adapter(FILE *) {
    __asm__ volatile("movq %rbx, %rsi\n jmp _xe_address_flush");
}

#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *new_fn, *old_fn; } hook_##original \
    __attribute__((section("__DATA,__interpose"))) = {(const void *)replacement, (const void *)original}
INTERPOSE(copy_adapter, memcpy);
INTERPOSE(flush_adapter, fflush);

// 必须显式选择实验模式；本库不会写入系统驱动或发布系统编码能力。
__attribute__((constructor)) static void initialize(void) {
    const char *enabled = getenv("XE_VDENC_GEN12_ADDR");
    if (!enabled || strcmp(enabled, "1")) _exit(78);
    _dyld_register_func_for_add_image(image_added);
}
