#ifndef XE_VME_SUBMIT_CAPTURE_H
#define XE_VME_SUBMIT_CAPTURE_H

#include <stddef.h>
#include <stdint.h>

/* 当前 Tahoe VA 中已核对的 SubmitDataBuffers call 指令偏移。 */
#define XE_VME_SUBMIT_CALLSITE_OFFSET 0xa8f6U

/* a893 直接访问到 helper+0x80 指针，故捕获固定 0x88 字节前缀。 */
#define XE_VME_MANAGER_PREFIX_BYTES 0x88U

typedef size_t (*XeVMECopyBytes)(void *destination, const void *source,
                                 size_t size);

typedef struct {
    uintptr_t manager;
    uint32_t request;
    uint32_t flags;
    uint32_t fault;
    uint32_t cached_class_count;
    uint32_t captured_bytes;
} XeVMEManagerSnapshot;

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

/* 捕获 VA helper 的固定前缀，不解引用其中保存的地址。 */
int xe_vme_capture_manager_snapshot(const void *manager, uint32_t request,
                                    uint32_t flags, uint32_t fault,
                                    XeVMECopyBytes copy_bytes,
                                    XeVMEManagerSnapshot *snapshot,
                                    unsigned char *storage, size_t storage_size,
                                    char *error, size_t error_size);

#endif
