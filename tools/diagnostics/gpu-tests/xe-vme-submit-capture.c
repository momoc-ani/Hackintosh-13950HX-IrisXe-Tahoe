/* 普通 VME/PAK 提交前捕获器：默认只在进程内工作，绝不修改系统 VA。 */
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

/* 检查 size 累加是否会溢出并且仍处于调用者提供的存储范围内。 */
static int xe_vme_span_fits(size_t offset, uint64_t size, size_t storage_size) {
    if (size > SIZE_MAX) return 0;
    return offset <= storage_size && (size_t)size <= storage_size - offset;
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

/* 只接受当前 VA 版本的直接 call 指令，防止错把其它导入调用当作提交边界。 */
int xe_vme_callsite_matches(const unsigned char *image, size_t image_size) {
    static const unsigned char expected[] = {0xe8, 0x15, 0x38, 0x23, 0x00};
    if (!image || image_size < XE_VME_SUBMIT_CALLSITE_OFFSET + sizeof(expected))
        return 0;
    return memcmp(image + XE_VME_SUBMIT_CALLSITE_OFFSET, expected,
                  sizeof(expected)) == 0;
}

/* 在资源元数据满足边界后复制一个 class，并记录其原始地址与存储偏移。 */
static int xe_vme_capture_class(void *context, uint32_t index,
                                const XeVMEProvider *provider,
                                XeVMESubmitSnapshot *snapshot,
                                unsigned char *storage, size_t storage_size,
                                size_t *storage_offset, char *error,
                                size_t error_size) {
    void *resource = provider->get_resource(context, index);
    if (!resource) {
        xe_vme_error(error, error_size, "data-buffer resource is null");
        return -1;
    }

    const void *data_bytes = provider->get_data_bytes(resource);
    uint64_t size = provider->get_data_size(resource);
    if (size == 0) {
        xe_vme_error(error, error_size, "data-buffer class has no bytes");
        return -1;
    }
    if (size > XE_VME_MAX_CLASS_BYTES) {
        xe_vme_error(error, error_size, "data-buffer class is too large");
        return -1;
    }
    if (snapshot->total_bytes > XE_VME_MAX_TOTAL_BYTES - size) {
        xe_vme_error(error, error_size, "capture total exceeds the safety limit");
        return -1;
    }
    if (size && (uintptr_t)data_bytes > UINTPTR_MAX - (uintptr_t)size) {
        xe_vme_error(error, error_size, "data-buffer address span overflows");
        return -1;
    }
    if (size && !data_bytes) {
        xe_vme_error(error, error_size, "data-buffer bytes are null");
        return -1;
    }
    if (!xe_vme_span_fits(*storage_offset, size, storage_size)) {
        xe_vme_error(error, error_size, "capture storage span is out of bounds");
        return -1;
    }
    if (size) {
        size_t copied = provider->copy_bytes(storage + *storage_offset, data_bytes,
                                             (size_t)size);
        if (copied != (size_t)size) {
            xe_vme_error(error, error_size, "data-buffer copy was incomplete");
            return -1;
        }
    }

    XeVMEClassSnapshot *record = &snapshot->classes[index];
    record->index = index;
    record->resource = (uintptr_t)resource;
    record->data_bytes = (uintptr_t)data_bytes;
    record->size = size;
    record->copied = size;
    record->storage_offset = *storage_offset;
    *storage_offset += (size_t)size;
    snapshot->captured_classes++;
    snapshot->total_bytes += size;
    return 0;
}

/* 读取提交前所有 data-buffer class，并在任何未知或越界状态下 fail-closed。 */
int xe_vme_capture_snapshot(void *context, uint32_t request, uint32_t flags,
                            uint32_t fault, const XeVMEProvider *provider,
                            XeVMESubmitSnapshot *snapshot, unsigned char *storage,
                            size_t storage_size, char *error, size_t error_size) {
    if (!provider || !provider->get_class_count || !provider->get_resource ||
        !provider->get_data_bytes || !provider->get_data_size ||
        !provider->copy_bytes || !snapshot || (!storage && storage_size)) {
        xe_vme_error(error, error_size, "capture provider is incomplete");
        return -1;
    }
    memset(snapshot, 0, sizeof(*snapshot));
    snapshot->request = request;
    snapshot->flags = flags;
    snapshot->fault = fault;

    int class_count = provider->get_class_count(context);
    if (class_count <= 0 || (uint32_t)class_count > XE_VME_MAX_CLASSES) {
        xe_vme_error(error, error_size, "data-buffer class count is invalid");
        return -1;
    }
    snapshot->class_count = (uint32_t)class_count;
    size_t storage_offset = 0;
    for (uint32_t index = 0; index < snapshot->class_count; ++index) {
        if (xe_vme_capture_class(context, index, provider, snapshot, storage,
                                 storage_size, &storage_offset, error,
                                 error_size) != 0) {
            return -1;
        }
    }
    if (snapshot->total_bytes > XE_VME_MAX_TOTAL_BYTES) {
        xe_vme_error(error, error_size, "capture total exceeds the safety limit");
        return -1;
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

/* 输出一个完整 class 二进制快照，失败时结束本次诊断进程。 */
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

/* 将快照元数据写成逐行 JSON，便于与普通 VDENC 采集做离线比较。 */
static void xe_vme_write_metadata(const char *root,
                                  const XeVMESubmitSnapshot *snapshot) {
    char path[PATH_MAX];
    int length = snprintf(path, sizeof(path), "%s/submit-events-%ld.jsonl",
                          root, (long)getpid());
    if (length < 0 || (size_t)length >= sizeof(path)) _exit(86);
    int fd = open(path, O_CREAT | O_EXCL | O_WRONLY, 0600);
    if (fd < 0) _exit(86);
    if (dprintf(fd, "{\"event\":\"submit-before-original\",\"request\":%u,\"flags\":%u,\"fault\":%u,\"class_count\":%u,\"total_bytes\":%llu,\"used_lengths\":\"unknown\",\"sideband\":\"not-captured\"}\n",
                snapshot->request, snapshot->flags, snapshot->fault,
                snapshot->class_count,
                (unsigned long long)snapshot->total_bytes) < 0) _exit(86);
    for (uint32_t index = 0; index < snapshot->captured_classes; ++index) {
        const XeVMEClassSnapshot *record = &snapshot->classes[index];
        if (dprintf(fd, "{\"event\":\"class\",\"index\":%u,\"resource\":\"0x%llx\",\"data_bytes\":\"0x%llx\",\"capacity\":%llu,\"used_length\":\"unknown\",\"storage_offset\":%llu}\n",
                    record->index, (unsigned long long)record->resource,
                    (unsigned long long)record->data_bytes,
                    (unsigned long long)record->size,
                    (unsigned long long)record->storage_offset) < 0) _exit(86);
    }
    if (fsync(fd) != 0 || close(fd) != 0) _exit(86);
}

/* 写出所有 class 后按环境变量决定截停还是继续原提交。 */
static void xe_vme_write_snapshot(const char *root,
                                  const XeVMESubmitSnapshot *snapshot,
                                  const unsigned char *storage) {
    for (uint32_t index = 0; index < snapshot->captured_classes; ++index) {
        const XeVMEClassSnapshot *record = &snapshot->classes[index];
        char path[PATH_MAX];
        int length = snprintf(path, sizeof(path), "%s/submit-%ld-class-%u.bin",
                              root, (long)getpid(), index);
        if (length < 0 || (size_t)length >= sizeof(path)) _exit(86);
        int fd = open(path, O_CREAT | O_EXCL | O_WRONLY, 0600);
        if (fd < 0) _exit(86);
        xe_vme_write_all(fd, storage + record->storage_offset, (size_t)record->size);
        if (fsync(fd) != 0 || close(fd) != 0) _exit(86);
    }
    xe_vme_write_metadata(root, snapshot);
}

extern int IOAccelVideoContextSubmitDataBuffers(void *, uint32_t, uint32_t *, uint32_t *);
extern int IOAccelVideoContextGetDataBufferClassCount(void *);
extern void *IOAccelVideoContextGetDataBufferResource(void *, uint32_t);
extern void *IOAccelResourceGetDataBytes(void *);
extern uint64_t IOAccelResourceGetDataSize(void *);

/* 通过系统 IOAccelerator getter 提供待提交 class 的只读数据。 */
static int xe_vme_get_class_count(void *context) {
    return IOAccelVideoContextGetDataBufferClassCount(context);
}

/* 返回一个 data-buffer resource，不修改驱动状态。 */
static void *xe_vme_get_resource(void *context, uint32_t index) {
    return IOAccelVideoContextGetDataBufferResource(context, index);
}

/* 返回 resource 映射到当前进程的起始地址。 */
static const void *xe_vme_get_data_bytes(void *resource) {
    return IOAccelResourceGetDataBytes(resource);
}

/* 返回 resource 当前完整容量，实际使用长度保持为未知。 */
static uint64_t xe_vme_get_data_size(void *resource) {
    return IOAccelResourceGetDataSize(resource);
}

static char xe_vme_capture_root[PATH_MAX];
static XeVMEProvider xe_vme_provider;
static uintptr_t xe_vme_driver_base;
static int xe_vme_capture_enabled;

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

/* 在提交入口捕获所有 class；截停开关开启时绝不调用原始提交。 */
__attribute__((noinline)) static int xe_vme_submit_adapter(
    void *context, uint32_t request, uint32_t *flags, uint32_t *fault) {
    const char *root = getenv("XE_VME_CAPTURE_DIR");
    const char *stop = getenv("XE_VME_STOP_BEFORE_SUBMIT");
    uintptr_t caller = (uintptr_t)__builtin_return_address(0);
    Dl_info caller_info;
    int caller_resolved = dladdr((const void *)caller, &caller_info) != 0;
    int caller_is_va = caller_resolved &&
        caller_info.dli_fname && strstr(caller_info.dli_fname,
                                        "AppleIntelTGLGraphicsVADriver");
    int callsite_verified = xe_vme_driver_base && caller >= xe_vme_driver_base &&
        caller - xe_vme_driver_base == 0xa8fb;
    int stop_enabled = root && *root && stop && strcmp(stop, "1") == 0;
    int action = xe_vme_submit_action(xe_vme_capture_enabled, caller_resolved,
                                      caller_is_va, callsite_verified,
                                      stop_enabled);
    if (action == XE_VME_ACTION_REJECT) {
        dprintf(STDERR_FILENO,
                caller_is_va && !stop_enabled ?
                    "XE-VME stop-before-submit switch is required\n" :
                    "XE-VME submit caller or callsite is not verified\n");
        _exit(caller_is_va && !stop_enabled ? 78 : 79);
    }
    if (action == XE_VME_ACTION_FORWARD) {
        // dyld 不对 interposer 自身的原导出引用重复应用同一替换。
        return IOAccelVideoContextSubmitDataBuffers(context, request, flags, fault);
    }

    unsigned char *storage = malloc(XE_VME_MAX_TOTAL_BYTES);
    if (!storage) _exit(86);
    XeVMESubmitSnapshot snapshot;
    char error[128] = {0};
    uint32_t flag_value = flags ? *flags : 0;
    uint32_t fault_value = fault ? *fault : 0;
    int status = xe_vme_capture_snapshot(context, request, flag_value, fault_value,
                                         &xe_vme_provider, &snapshot, storage,
                                         XE_VME_MAX_TOTAL_BYTES, error,
                                         sizeof(error));
    if (status != 0) {
        dprintf(STDERR_FILENO, "XE-VME capture rejected: %s\n", error);
        free(storage);
        _exit(87);
    }
    xe_vme_write_snapshot(xe_vme_capture_root, &snapshot, storage);
    free(storage);
    _exit(77);
}

/* 初始化进程内提交捕获；没有显式输出目录时不改变任何运行行为。 */
__attribute__((constructor)) static void xe_vme_capture_init(void) {
    const char *root = getenv("XE_VME_CAPTURE_DIR");
    const char *stop = getenv("XE_VME_STOP_BEFORE_SUBMIT");
    if ((!root || !*root) && stop && strcmp(stop, "1") == 0) {
        dprintf(STDERR_FILENO,
                "XE-VME capture directory is required with stop-before-submit\n");
        _exit(78);
    }
    if (!root || !*root) return;
    if (strlen(root) >= sizeof(xe_vme_capture_root)) {
        dprintf(STDERR_FILENO, "XE-VME capture directory path is too long\n");
        _exit(78);
    }
    strcpy(xe_vme_capture_root, root);
    xe_vme_provider.get_class_count = xe_vme_get_class_count;
    xe_vme_provider.get_resource = xe_vme_get_resource;
    xe_vme_provider.get_data_bytes = xe_vme_get_data_bytes;
    xe_vme_provider.get_data_size = xe_vme_get_data_size;
    xe_vme_provider.copy_bytes = xe_vme_copy_current_task;
    xe_vme_capture_enabled = 1;
    dprintf(STDERR_FILENO,
            "XE-VME capture armed; stop-before-submit is mandatory; used lengths and sideband remain unknown\n");
    _dyld_register_func_for_add_image(xe_vme_image_added);
}

/* dyld interpose 表仅替换当前进程的导入指针。 */
#define XE_VME_INTERPOSE(replacement, original) \
    __attribute__((used)) static struct { const void *new_fn, *old_fn; } \
        xe_vme_hook_##original __attribute__((section("__DATA,__interpose"))) = \
            {(const void *)(replacement), (const void *)(original)}

XE_VME_INTERPOSE(xe_vme_submit_adapter, IOAccelVideoContextSubmitDataBuffers);

#endif
