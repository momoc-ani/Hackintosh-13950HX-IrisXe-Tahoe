/* VA 提交边界诊断：仅捕获进程内 manager 与缓冲区，不修改驱动文件。 */
#include "xe-vme-submit-capture.h"

#include <limits.h>
#include <stdio.h>
#include <string.h>

#ifndef XE_VME_CAPTURE_TEST
#include <errno.h>
#include <fcntl.h>
#include <mach-o/dyld.h>
#include <mach-o/loader.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <dlfcn.h>
#include <stdlib.h>
#include <unistd.h>
#endif

/* 写入短错误文本，避免诊断失败时继续使用不完整快照。 */
static void xe_vme_error(char *error, size_t error_size, const char *message) {
    if (!error || !error_size) return;
    (void)snprintf(error, error_size, "%s", message);
}

/* 只允许已验证的 VA 提交点在显式截停模式下进入捕获逻辑。 */
int xe_vme_submit_action(int capture_enabled, int caller_resolved,
                         int caller_is_va, int callsite_verified,
                         int stop_enabled) {
    if (!capture_enabled) return XE_VME_ACTION_FORWARD;
    if (!caller_resolved) return XE_VME_ACTION_REJECT;
    if (!caller_is_va) return XE_VME_ACTION_FORWARD;
    if (!callsite_verified || !stop_enabled) return XE_VME_ACTION_REJECT;
    return XE_VME_ACTION_CAPTURE;
}

/* 只接受当前 VA 版本的直接 call 指令，防止错配提交边界。 */
int xe_vme_callsite_matches(const unsigned char *image, size_t image_size) {
    static const unsigned char expected[] = {0xe8, 0x15, 0x38, 0x23, 0x00};
    if (!image || image_size < XE_VME_SUBMIT_CALLSITE_OFFSET + sizeof(expected))
        return 0;
    return memcmp(image + XE_VME_SUBMIT_CALLSITE_OFFSET, expected,
                  sizeof(expected)) == 0;
}

/* 只读复制 a893 helper 实际访问的固定前缀，并保留偏移 +0x58 的缓存计数。 */
int xe_vme_capture_manager_snapshot(const void *manager, uint32_t request,
                                    uint32_t flags, uint32_t fault,
                                    XeVMECopyBytes copy_bytes,
                                    XeVMEManagerSnapshot *snapshot,
                                    unsigned char *storage, size_t storage_size,
                                    char *error, size_t error_size) {
    if (!manager || !copy_bytes || !snapshot || !storage ||
        storage_size < XE_VME_MANAGER_PREFIX_BYTES ||
        (uintptr_t)manager > UINTPTR_MAX - XE_VME_MANAGER_PREFIX_BYTES) {
        xe_vme_error(error, error_size, "manager prefix span is invalid");
        return -1;
    }
    memset(snapshot, 0, sizeof(*snapshot));
    size_t copied = copy_bytes(storage, manager, XE_VME_MANAGER_PREFIX_BYTES);
    if (copied != XE_VME_MANAGER_PREFIX_BYTES) {
        xe_vme_error(error, error_size, "manager prefix copy was incomplete");
        return -1;
    }
    memcpy(&snapshot->cached_class_count, storage + 0x58,
           sizeof(snapshot->cached_class_count));
    snapshot->manager = (uintptr_t)manager;
    snapshot->request = request;
    snapshot->flags = flags;
    snapshot->fault = fault;
    snapshot->manager_prefix_bytes = XE_VME_MANAGER_PREFIX_BYTES;
    snapshot->captured_bytes = XE_VME_MANAGER_PREFIX_BYTES;
    return 0;
}

/* 解析 VA 写游标与边界，仅测量当前提交涉及的已用区间。 */
static int xe_vme_measure_buffer_range(const unsigned char *prefix,
                                       size_t base_offset,
                                       size_t current_offset,
                                       size_t end_offset,
                                       XeVMEBufferSnapshot *buffer,
                                       uint64_t *total,
                                       char *error, size_t error_size) {
    uintptr_t base = 0;
    uintptr_t current = 0;
    uintptr_t end = 0;
    memcpy(&base, prefix + base_offset, sizeof(base));
    memcpy(&current, prefix + current_offset, sizeof(current));
    memcpy(&end, prefix + end_offset, sizeof(end));
    buffer->address = base;
    buffer->size = 0;
    buffer->storage_offset = XE_VME_MANAGER_PREFIX_BYTES + *total;

    if (current == 0 && end == 0) return 0;
    if (!base || !current || !end || base > current || current > end) {
        xe_vme_error(error, error_size, "submit buffer pointer range is invalid");
        return -1;
    }
    uint64_t used = (uint64_t)(current - base);
    if (used > XE_VME_CAPTURE_MAX_TOTAL_BYTES ||
        *total > XE_VME_CAPTURE_MAX_TOTAL_BYTES - used) {
        xe_vme_error(error, error_size, "submit buffers exceed capture budget");
        return -1;
    }
    buffer->size = used;
    *total += used;
    return 0;
}

/* 根据 manager 前缀校验 command/sideband 游标，并计算快照布局。 */
int xe_vme_measure_submit_buffers(const void *manager,
                                  const unsigned char *manager_prefix,
                                  uint32_t request, uint32_t flags,
                                  uint32_t fault,
                                  XeVMEManagerSnapshot *snapshot,
                                  char *error, size_t error_size) {
    if (!manager || !manager_prefix || !snapshot) {
        xe_vme_error(error, error_size, "submit buffer measurement inputs are invalid");
        return -1;
    }
    memset(snapshot, 0, sizeof(*snapshot));
    memcpy(&snapshot->cached_class_count, manager_prefix + 0x58,
           sizeof(snapshot->cached_class_count));
    snapshot->manager = (uintptr_t)manager;
    snapshot->request = request;
    snapshot->flags = flags;
    snapshot->fault = fault;
    snapshot->manager_prefix_bytes = XE_VME_MANAGER_PREFIX_BYTES;

    uint64_t total = 0;
    if (xe_vme_measure_buffer_range(manager_prefix, 0x10, 0x20, 0x18,
            &snapshot->command_buffer, &total, error, error_size) != 0 ||
        xe_vme_measure_buffer_range(manager_prefix, 0x80, 0x60, 0x68,
            &snapshot->sideband_buffer, &total, error, error_size) != 0)
        return -1;
    snapshot->captured_bytes = XE_VME_MANAGER_PREFIX_BYTES + total;
    return 0;
}

/* 识别空的初始化提交，供显式首个非空截停模式安全放行。 */
int xe_vme_submit_has_data(const XeVMEManagerSnapshot *snapshot) {
    if (!snapshot) return 0;
    return snapshot->cached_class_count != 0 ||
        snapshot->command_buffer.size != 0;
}

/* 复制已测量的 VA command/sideband 字节，确认输出布局仍在分配范围内。 */
int xe_vme_copy_submit_buffers(const unsigned char *manager_prefix,
                               const XeVMEManagerSnapshot *snapshot,
                               XeVMECopyBytes copy_bytes,
                               unsigned char *storage, size_t storage_size,
                               char *error, size_t error_size) {
    if (!manager_prefix || !snapshot || !copy_bytes || !storage ||
        snapshot->manager_prefix_bytes != XE_VME_MANAGER_PREFIX_BYTES ||
        snapshot->captured_bytes > storage_size ||
        snapshot->captured_bytes < XE_VME_MANAGER_PREFIX_BYTES) {
        xe_vme_error(error, error_size, "submit buffer storage is invalid");
        return -1;
    }
    memcpy(storage, manager_prefix, XE_VME_MANAGER_PREFIX_BYTES);
    const XeVMEBufferSnapshot *buffers[] = {
        &snapshot->command_buffer, &snapshot->sideband_buffer
    };
    for (size_t index = 0; index < sizeof(buffers) / sizeof(buffers[0]); ++index) {
        const XeVMEBufferSnapshot *buffer = buffers[index];
        if (buffer->storage_offset > snapshot->captured_bytes ||
            buffer->size > snapshot->captured_bytes - buffer->storage_offset ||
            buffer->size > SIZE_MAX ||
            (buffer->size && (!buffer->address ||
             buffer->address > UINTPTR_MAX - buffer->size))) {
            xe_vme_error(error, error_size, "submit buffer copy range is invalid");
            return -1;
        }
        if (buffer->size && copy_bytes(storage + buffer->storage_offset,
                (const void *)buffer->address, (size_t)buffer->size) !=
                (size_t)buffer->size) {
            xe_vme_error(error, error_size, "submit buffer copy was incomplete");
            return -1;
        }
    }
    return 0;
}

#ifndef XE_VME_CAPTURE_TEST

/* 将当前进程可读的资源内存安全复制到诊断缓冲区，失败时返回零。 */
static size_t xe_vme_copy_current_task(void *destination, const void *source,
                                       size_t size) {
    mach_vm_size_t copied = 0;
    kern_return_t status = mach_vm_read_overwrite(
        mach_task_self(), (mach_vm_address_t)(uintptr_t)source,
        (mach_vm_size_t)size, (mach_vm_address_t)(uintptr_t)destination, &copied);
    return status == KERN_SUCCESS ? (size_t)copied : 0;
}

/* 写出完整 manager 前缀快照，失败时结束本次诊断进程。 */
static void xe_vme_write_all(int fd, const void *bytes, size_t size) {
    const unsigned char *cursor = bytes;
    while (size) {
        ssize_t written = write(fd, cursor, size);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) _exit(86);
        cursor += written;
        size -= (size_t)written;
    }
}

/* 写出 manager 前缀及本次提交前的 VA command/sideband 元数据。 */
static void xe_vme_write_manager_snapshot(const char *root,
                                          const XeVMEManagerSnapshot *snapshot,
                                          const unsigned char *storage,
                                          int buffers_captured) {
    char path[PATH_MAX];
    int length = snprintf(path, sizeof(path), "%s/%s-%ld.bin", root,
                          buffers_captured ? "submit" : "manager",
                          (long)getpid());
    if (length < 0 || (size_t)length >= sizeof(path)) _exit(86);
    int fd = open(path, O_CREAT | O_EXCL | O_WRONLY, 0600);
    if (fd < 0) _exit(86);
    xe_vme_write_all(fd, storage, snapshot->captured_bytes);
    if (fsync(fd) != 0 || close(fd) != 0) _exit(86);

    length = snprintf(path, sizeof(path), "%s/submit-events-%ld.jsonl",
                      root, (long)getpid());
    if (length < 0 || (size_t)length >= sizeof(path)) _exit(86);
    fd = open(path, O_CREAT | O_EXCL | O_WRONLY, 0600);
    if (fd < 0) _exit(86);
    const char *event = buffers_captured ? "submit-buffers-before-original" :
                                           "manager-prefix-before-original";
    const char *payload_state = buffers_captured ? "captured" : "not-captured";
    const char *sideband_state = buffers_captured ? "captured" : "not-captured";
    if (dprintf(fd, "{\"event\":\"%s\",\"manager\":\"0x%llx\",\"request\":%u,\"flags\":%u,\"fault\":%u,\"cached_class_count\":%u,\"captured_bytes\":%llu,\"command_buffer_address\":\"0x%llx\",\"command_buffer_bytes\":%llu,\"command_buffer_offset\":%llu,\"command_payload\":\"%s\",\"sideband_buffer_address\":\"0x%llx\",\"sideband_buffer_bytes\":%llu,\"sideband_buffer_offset\":%llu,\"sideband\":\"%s\",\"used_lengths\":\"unknown\"}\n",
                event, (unsigned long long)snapshot->manager,
                snapshot->request, snapshot->flags, snapshot->fault,
                snapshot->cached_class_count,
                (unsigned long long)snapshot->captured_bytes,
                (unsigned long long)snapshot->command_buffer.address,
                (unsigned long long)snapshot->command_buffer.size,
                (unsigned long long)snapshot->command_buffer.storage_offset,
                payload_state,
                (unsigned long long)snapshot->sideband_buffer.address,
                (unsigned long long)snapshot->sideband_buffer.size,
                (unsigned long long)snapshot->sideband_buffer.storage_offset,
                sideband_state) < 0) _exit(86);
    if (fsync(fd) != 0 || close(fd) != 0) _exit(86);
}

extern int IOAccelVideoContextSubmitDataBuffers(void *, uint32_t, uint32_t *, uint32_t *);
static char xe_vme_capture_root[PATH_MAX];
static uintptr_t xe_vme_driver_base;
static int xe_vme_capture_enabled;

/* 未启用捕获时直接调用原导出，避免进入诊断分派路径。 */
__attribute__((used, noinline)) static int xe_vme_submit_forward(
    void *context, uint32_t request, uint32_t *flags, uint32_t *fault) {
    return IOAccelVideoContextSubmitDataBuffers(context, request, flags, fault);
}

/* 只接受 image base 为零且包含目标调用点的 Mach-O __TEXT 段。 */
static size_t xe_vme_text_size(const struct mach_header *header) {
    if (!header || header->magic != MH_MAGIC_64) return 0;
    const struct mach_header_64 *header64 =
        (const struct mach_header_64 *)header;
    const struct load_command *command =
        (const struct load_command *)(header64 + 1);
    for (uint32_t index = 0; index < header64->ncmds; ++index) {
        if (command->cmd == LC_SEGMENT_64) {
            const struct segment_command_64 *segment =
                (const struct segment_command_64 *)command;
            if (strcmp(segment->segname, "__TEXT") == 0) {
                if (segment->vmaddr != 0 || segment->vmsize > SIZE_MAX) return 0;
                return (size_t)segment->vmsize;
            }
        }
        command = (const struct load_command *)
            ((const unsigned char *)command + command->cmdsize);
    }
    return 0;
}

/* 识别精确的 Apple VA image，并验证提交调用点仍是已分析的指令。 */
static void xe_vme_image_added(const struct mach_header *header, intptr_t slide) {
    (void)slide;
    for (uint32_t index = 0; index < _dyld_image_count(); ++index) {
        if (_dyld_get_image_header(index) != header) continue;
        const char *name = _dyld_get_image_name(index);
        const char suffix[] = "/AppleIntelTGLGraphicsVADriver";
        size_t name_length = name ? strlen(name) : 0;
        if (name_length < sizeof(suffix) - 1 ||
            strcmp(name + name_length - sizeof(suffix) + 1, suffix)) continue;

        size_t text_size = xe_vme_text_size(header);
        const unsigned char *image = (const unsigned char *)header;
        if (!xe_vme_callsite_matches(image, text_size)) {
            dprintf(STDERR_FILENO, "XE-VME target VA submit callsite mismatch\n");
            _exit(79);
        }
        xe_vme_driver_base = (uintptr_t)header;
        dprintf(STDERR_FILENO, "XE-VME target VA submit callsite verified\n");
        return;
    }
}

/* 在目标入口跳过空初始化提交，或截停首个非空提交缓冲区。 */
__attribute__((used, noinline)) static int xe_vme_submit_dispatch(
    void *context, uint32_t request, uint32_t *flags, uint32_t *fault,
    const void *manager) {
    const char *root = getenv("XE_VME_CAPTURE_DIR");
    const char *stop = getenv("XE_VME_STOP_BEFORE_SUBMIT");
    const char *nonempty = getenv("XE_VME_CAPTURE_NONEMPTY_SUBMIT");
    uintptr_t caller = (uintptr_t)__builtin_return_address(0);
    Dl_info caller_info;
    int caller_resolved = dladdr((const void *)caller, &caller_info) != 0;
    int caller_is_va = caller_resolved &&
        caller_info.dli_fname && strstr(caller_info.dli_fname,
                                        "AppleIntelTGLGraphicsVADriver");
    int callsite_verified = xe_vme_driver_base && caller >= xe_vme_driver_base &&
        caller - xe_vme_driver_base == 0xa8fb;
    int stop_enabled = root && *root && stop && strcmp(stop, "1") == 0;
    int nonempty_enabled = root && *root && nonempty && strcmp(nonempty, "1") == 0;
    int capture_mode_enabled = stop_enabled || nonempty_enabled;
    int action = xe_vme_submit_action(xe_vme_capture_enabled, caller_resolved,
                                      caller_is_va, callsite_verified,
                                      capture_mode_enabled);
    if (action == XE_VME_ACTION_REJECT) {
        dprintf(STDERR_FILENO,
                caller_is_va && !capture_mode_enabled ?
                    "XE-VME capture mode switch is required\n" :
                    "XE-VME submit caller or callsite is not verified\n");
        _exit(caller_is_va && !capture_mode_enabled ? 78 : 79);
    }
    if (action == XE_VME_ACTION_FORWARD) {
        // dyld 不对 interposer 自身的原导出引用重复应用同一替换。
        return IOAccelVideoContextSubmitDataBuffers(context, request, flags, fault);
    }

    char error[128] = {0};
    uint32_t flag_value = flags ? *flags : 0;
    uint32_t fault_value = fault ? *fault : 0;
    unsigned char prefix[XE_VME_MANAGER_PREFIX_BYTES];
    if (xe_vme_copy_current_task(prefix, manager,
                                 XE_VME_MANAGER_PREFIX_BYTES) !=
        XE_VME_MANAGER_PREFIX_BYTES) {
        dprintf(STDERR_FILENO, "XE-VME manager prefix read was incomplete\n");
        _exit(87);
    }
    XeVMEManagerSnapshot snapshot;
    if (xe_vme_measure_submit_buffers(manager, prefix, request, flag_value,
            fault_value, &snapshot, error, sizeof(error)) != 0) {
        dprintf(STDERR_FILENO, "XE-VME buffer range rejected: %s\n", error);
        _exit(87);
    }
    int has_submit_data = xe_vme_submit_has_data(&snapshot);
    if (nonempty_enabled && !stop_enabled && !has_submit_data) {
        dprintf(STDERR_FILENO,
                "XE-VME empty submit allowed; no cached classes or used buffer bytes\n");
        return IOAccelVideoContextSubmitDataBuffers(context, request,
                                                    flags, fault);
    }
    if (snapshot.captured_bytes > SIZE_MAX) _exit(86);
    size_t storage_size = (size_t)snapshot.captured_bytes;
    unsigned char *storage = malloc(storage_size);
    if (!storage) _exit(86);
    if (xe_vme_copy_submit_buffers(prefix, &snapshot, xe_vme_copy_current_task,
            storage, storage_size, error, sizeof(error)) != 0) {
        dprintf(STDERR_FILENO, "XE-VME buffer copy rejected: %s\n", error);
        free(storage);
        _exit(87);
    }
    int buffers_captured = snapshot.command_buffer.size != 0 ||
                           snapshot.sideband_buffer.size != 0;
    xe_vme_write_manager_snapshot(xe_vme_capture_root, &snapshot, storage,
                                  buffers_captured);
    free(storage);
    _exit(77);
}

/* 保留 a893 的 callee-saved R14，在 C prologue 改写寄存器前传给捕获器。 */
__attribute__((naked)) static int xe_vme_submit_adapter(
    void *context __attribute__((unused)),
    uint32_t request __attribute__((unused)),
    uint32_t *flags __attribute__((unused)),
    uint32_t *fault __attribute__((unused))) {
    __asm__ volatile("cmpb $0, _xe_vme_capture_enabled(%rip)\n"
                     "je _xe_vme_submit_forward\n"
                     "movq %r14, %r8\n"
                     "jmp _xe_vme_submit_dispatch");
}

/* 初始化进程内提交捕获；没有显式输出目录时不改变任何运行行为。 */
__attribute__((constructor)) static void xe_vme_capture_init(void) {
    const char *root = getenv("XE_VME_CAPTURE_DIR");
    const char *stop = getenv("XE_VME_STOP_BEFORE_SUBMIT");
    const char *nonempty = getenv("XE_VME_CAPTURE_NONEMPTY_SUBMIT");
    int stop_enabled = stop && strcmp(stop, "1") == 0;
    int nonempty_enabled = nonempty && strcmp(nonempty, "1") == 0;
    if ((!root || !*root) && (stop_enabled || nonempty_enabled)) {
        dprintf(STDERR_FILENO,
                "XE-VME capture directory is required with an explicit mode\n");
        _exit(78);
    }
    if (!root || !*root) return;
    if (stop_enabled == nonempty_enabled) {
        dprintf(STDERR_FILENO,
                "XE-VME exactly one capture mode must be enabled\n");
        _exit(78);
    }
    if (strlen(root) >= sizeof(xe_vme_capture_root)) {
        dprintf(STDERR_FILENO, "XE-VME capture directory path is too long\n");
        _exit(78);
    }
    strcpy(xe_vme_capture_root, root);
    xe_vme_capture_enabled = 1;
    dprintf(STDERR_FILENO, "XE-VME %s capture armed\n",
            stop_enabled ? "manager-prefix" : "first-nonempty-submit-buffer");
    _dyld_register_func_for_add_image(xe_vme_image_added);
}

/* dyld interpose 表仅替换当前进程的导入指针。 */
#define XE_VME_INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *new_fn, *old_fn; } \
        xe_vme_hook_##original __attribute__((section("__DATA,__interpose"))) = \
            {(const void *)(replacement), (const void *)(original)}

XE_VME_INTERPOSE(xe_vme_submit_adapter, IOAccelVideoContextSubmitDataBuffers);

#endif
