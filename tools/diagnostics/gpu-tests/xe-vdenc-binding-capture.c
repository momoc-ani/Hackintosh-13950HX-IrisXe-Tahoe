// 进程内 VDENC 绑定采集：只提交第一帧 I，保存第二帧后在提交前退出。
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <unistd.h>
#include <string.h>
#include <errno.h>
#include "xe-vdenc-command-layout.h"
#ifndef XE_BINDING_TEST
#include <mach-o/dyld.h>
#endif

static const unsigned char *context, *saved_token, *saved_batch;
static size_t saved_bytes;
static unsigned ready_count;
static int submitted, finished, bind_only_second, expand_address, stop_first, events = -1;
static char output_root[4096];

// 完整写出一个采集块，文件错误时停止自有进程，避免丢证据后继续测试。
static void write_all(int fd, const void *bytes, size_t size) {
    const unsigned char *cursor = bytes;
    while (size) {
        ssize_t written = write(fd, cursor, size);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) _exit(83);
        cursor += written;
        size -= (size_t)written;
    }
}

// 输出独立的阶段快照，保留用户态构造与内核回填之间的差异。
static void save_pair(unsigned frame, const char *stage, const unsigned char *token,
                      const unsigned char *batch, size_t bytes) {
    const char *kinds[] = {"token", "batch"};
    const unsigned char *data[] = {token, batch};
    const size_t sizes[] = {0xce8, bytes};
    for (unsigned i = 0; i < 2; ++i) {
        char path[4096];
        int length = snprintf(path, sizeof path, "%s/frame-%u-%s-%s.bin",
                              output_root, frame, stage, kinds[i]);
        if (length < 0 || (size_t)length >= sizeof path) _exit(82);
        int fd = open(path, O_CREAT | O_TRUNC | O_WRONLY, 0600);
        if (fd < 0) _exit(82);
        write_all(fd, data[i], sizes[i]);
        close(fd);
    }
}

// 在 fflush 的已核对调用点读取最终命令；第二帧在这里退出，尚未进入提交函数。
static void capture_ready(const unsigned char *encoder) {
    const unsigned char *token = *(const unsigned char **)(encoder + 0x6bc8);
    const unsigned char *begin = *(const unsigned char **)(context + 0x10);
    const unsigned char *end = *(const unsigned char **)(context + 0x70);
    if (!token || !begin || end < begin || (size_t)(end - begin) > 1024 * 1024) _exit(84);
    if (expand_address) {
        const unsigned char *limit = *(const unsigned char **)(context + 0x18);
        const unsigned char *reserved = *(const unsigned char **)(context + 0x20);
        size_t bytes = (size_t)(end - begin);
        if (limit < begin || reserved < end || limit < reserved || (size_t)(limit - reserved) < 136)
            _exit(89);
        int status = xe_vdenc_expand_address((unsigned char *)(uintptr_t)begin,
            (size_t)(limit - begin), &bytes, (unsigned char *)(uintptr_t)token, 0xce8);
        if (status) _exit(90 + status);
        end = begin + bytes;
        *(const unsigned char **)(uintptr_t)(context + 0x20) = reserved + 136;
        *(const unsigned char **)(uintptr_t)(context + 0x70) = end;
        dprintf(events, "expanded address command frame=%u bytes=%zu reserved_extra=136\n",
                ready_count, bytes);
    }
    save_pair(ready_count, "before", token, begin, (size_t)(end - begin));
    dprintf(events, "ready frame=%u batch_bytes=%zu\n", ready_count, (size_t)(end - begin));
    if (++ready_count == 1 && stop_first) _exit(77);
    if (ready_count == 2) {
        if (!bind_only_second) {
            dprintf(events, "stop before second frame submission\n");
            _exit(77);
        }
        if (!submitted || !finished) _exit(87);
        // 内核仍按 token 回填资源；GPU 从第一条指令即结束，不执行 P 帧编码。
        *(uint32_t *)(uintptr_t)begin = 0x05000000;
        save_pair(1, "guarded", token, begin, (size_t)(end - begin));
        dprintf(events, "guard second batch with MI_BATCH_BUFFER_END\n");
    }
    saved_token = token;
    saved_batch = begin;
    saved_bytes = (size_t)(end - begin);
}

// 提交返回后立即采集，早于 VA 换用下一缓冲区及清零操作。
static void capture_submitted(int status, uint32_t flags, uint32_t fault) {
    if (ready_count == 2 && bind_only_second) {
        if (*(const uint32_t *)saved_batch != 0x05000000) _exit(88);
        dprintf(events, "bind-only submit status=%d flags=%u fault=%u\n", status, flags, fault);
        if (status || fault || (flags & 0x20000000)) _exit(85);
        save_pair(1, "submitted", saved_token, saved_batch, saved_bytes);
        dprintf(events, "stop after second frame binding\n");
        _exit(77);
    }
    if (ready_count != 1 || submitted) return;
    dprintf(events, "submit status=%d flags=%u fault=%u\n", status, flags, fault);
    if (status || fault || (flags & 0x20000000)) _exit(85);
    save_pair(0, "submitted", saved_token, saved_batch, saved_bytes);
    submitted = 1;
}

// 同一视频上下文的 fence 完成后再采集一次，并单独记录完成状态。
static void capture_finished(int status, uint32_t fence) {
    if (ready_count != 1 || !submitted || finished) return;
    dprintf(events, "finish status=%d fence=%u\n", status, fence);
    if (status) _exit(86);
    save_pair(0, "finished", saved_token, saved_batch, saved_bytes);
    finished = 1;
}

// 初始化工作目录与事件日志，不修改系统目录或驱动文件。
static void initialize_files(const char *root) {
    if (!root || strlen(root) >= sizeof output_root) _exit(78);
    strcpy(output_root, root);
    char path[4096];
    int length = snprintf(path, sizeof path, "%s/binding-events.txt", root);
    if (length < 0 || (size_t)length >= sizeof path) _exit(78);
    events = open(path, O_CREAT | O_TRUNC | O_WRONLY, 0600);
    if (events < 0) _exit(79);
}

#ifndef XE_BINDING_TEST
static uintptr_t va_base;
static void *video_context;
extern int IOAccelVideoContextSubmitDataBuffers(void *, uint32_t, uint32_t *, uint32_t *);
extern int IOAccelVideoContextFinishFenceEvent(void *, uint32_t, uint32_t, uint32_t);

// 在 dyld 回调中确认 VA 名称及两个调用点字节，固定偏移只适用于此版本。
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
        if (memcmp(bytes + 0x2557a, copy_call, sizeof copy_call) ||
            memcmp(bytes + 0x277aa, flush_call, sizeof flush_call)) _exit(80);
        va_base = (uintptr_t)header;
        dprintf(events, "image=%s\n", name);
    }
}

// 逐字节复制以避免 interpose 重入，只在 builder 的匹配调用点记录上下文。
__attribute__((noinline)) void *xe_binding_copy(void *destination, const void *source,
                                              size_t size, const unsigned char *command) {
    if (va_base && size == 0x8c && (uintptr_t)__builtin_return_address(0) - va_base == 0x2557f)
        context = command;
    volatile unsigned char *out = destination;
    const volatile unsigned char *in = source;
    // macOS 的 memmove 可与 memcpy 共享导出地址，拦截器也必须保留重叠复制语义。
    if ((uintptr_t)out > (uintptr_t)in && (uintptr_t)out - (uintptr_t)in < size) {
        for (size_t i = size; i; --i) out[i - 1] = in[i - 1];
    } else {
        for (size_t i = 0; i < size; ++i) out[i] = in[i];
    }
    return destination;
}

// 将原 builder 的 rbx 作为第四参数传入，保留原 memcpy ABI。
__attribute__((naked)) static void *copy_adapter(void *, const void *, size_t) {
    __asm__ volatile("movq %rbx, %rcx\n jmp _xe_binding_copy");
}

// 提交前 fflush 提供 encoder 对象；保持 fflush 原本的输出行为。
__attribute__((noinline)) int xe_binding_flush(FILE *stream, const unsigned char *encoder) {
    if (va_base && (uintptr_t)__builtin_return_address(0) - va_base == 0x277af) {
        if (!context) _exit(81);
        capture_ready(encoder);
    }
    return fflush(stream);
}

// 将原 encoder 的 rbx 作为第二参数传入，保留原 fflush ABI。
__attribute__((naked)) static int flush_adapter(FILE *) {
    __asm__ volatile("movq %rbx, %rsi\n jmp _xe_binding_flush");
}

// 已由 0xa893 汇编确认四参数 ABI；第一帧提交后保存内核写回数据。
static int submit_adapter(void *ctx, uint32_t request, uint32_t *flags, uint32_t *fault) {
    int capture = (ready_count == 1 && !submitted) || (ready_count == 2 && bind_only_second);
    int result = IOAccelVideoContextSubmitDataBuffers(ctx, request, flags, fault);
    if (capture) {
        video_context = ctx;
        capture_submitted(result, *flags, *fault);
    }
    return result;
}

// 已由 0x27724 确认 flags/timeout 参数，仅记录首帧所属视频上下文。
static int finish_adapter(void *ctx, uint32_t fence, uint32_t flags, uint32_t timeout) {
    int result = IOAccelVideoContextFinishFenceEvent(ctx, fence, flags, timeout);
    if (ctx == video_context) capture_finished(result, fence);
    return result;
}

#define INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *new_fn, *old_fn; } hook_##original \
    __attribute__((section("__DATA,__interpose"))) = {(const void *)replacement, (const void *)original}
INTERPOSE(copy_adapter, memcpy);
INTERPOSE(flush_adapter, fflush);
INTERPOSE(submit_adapter, IOAccelVideoContextSubmitDataBuffers);
INTERPOSE(finish_adapter, IOAccelVideoContextFinishFenceEvent);

// 显式设置输出目录才启用采集，通过 DYLD_INSERT_LIBRARIES 仅载入自有探针。
__attribute__((constructor)) static void initialize(void) {
    initialize_files(getenv("XE_CAPTURE_DIR"));
    const char *first = getenv("XE_BINDING_BEFORE_ONLY");
    stop_first = first && !strcmp(first, "1");
    const char *address = getenv("XE_VDENC_GEN12_ADDR");
    expand_address = address && !strcmp(address, "1");
    const char *bind_only = getenv("XE_BINDING_SECOND_END");
    bind_only_second = bind_only && !strcmp(bind_only, "1");
    _dyld_register_func_for_add_image(image_added);
}
#endif
