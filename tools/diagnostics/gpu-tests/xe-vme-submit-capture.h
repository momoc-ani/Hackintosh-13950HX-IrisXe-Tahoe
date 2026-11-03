#ifndef XE_VME_SUBMIT_CAPTURE_H
#define XE_VME_SUBMIT_CAPTURE_H

#include <stddef.h>
#include <stdint.h>

/* 普通 VME/PAK 诊断只允许捕获有限数量的 data-buffer class。 */
#define XE_VME_MAX_CLASSES 32U

/* 单个 class 的最大只读复制范围，避免异常元数据消耗过多内存。 */
#define XE_VME_MAX_CLASS_BYTES (16ULL * 1024ULL * 1024ULL)

/* 一次提交的最大总采集范围，避免异常元数据导致大额内存分配。 */
#define XE_VME_MAX_TOTAL_BYTES (64ULL * 1024ULL * 1024ULL)

/* 当前 Tahoe VA 中已核对的 SubmitDataBuffers call 指令偏移。 */
#define XE_VME_SUBMIT_CALLSITE_OFFSET 0xa8f6U

typedef struct {
    uint32_t index;
    uintptr_t resource;
    uintptr_t data_bytes;
    uint64_t size;
    uint64_t copied;
    uint64_t storage_offset;
} XeVMEClassSnapshot;

typedef struct {
    uint32_t request;
    uint32_t flags;
    uint32_t fault;
    uint32_t class_count;
    uint32_t captured_classes;
    uint64_t total_bytes;
    XeVMEClassSnapshot classes[XE_VME_MAX_CLASSES];
} XeVMESubmitSnapshot;

typedef struct {
    int (*get_class_count)(void *context);
    void *(*get_resource)(void *context, uint32_t index);
    const void *(*get_data_bytes)(void *resource);
    uint64_t (*get_data_size)(void *resource);
    size_t (*copy_bytes)(void *destination, const void *source, size_t size);
} XeVMEProvider;

enum {
    XE_VME_ACTION_FORWARD = 0,
    XE_VME_ACTION_CAPTURE = 1,
    XE_VME_ACTION_REJECT = 2,
};

/* 根据调用者身份和截停开关选择转发、捕获或拒绝。 */
int xe_vme_submit_action(int capture_enabled, int caller_resolved,
                         int caller_is_va, int callsite_verified,
                         int stop_enabled);

/* 验证目标 VA image 的提交调用点指令字节。 */
int xe_vme_callsite_matches(const unsigned char *image, size_t image_size);

/* 捕获一份提交前 data-buffer 快照；任何元数据或复制异常都会返回非零。 */
int xe_vme_capture_snapshot(void *context, uint32_t request, uint32_t flags,
                            uint32_t fault, const XeVMEProvider *provider,
                            XeVMESubmitSnapshot *snapshot, unsigned char *storage,
                            size_t storage_size, char *error, size_t error_size);

#endif
