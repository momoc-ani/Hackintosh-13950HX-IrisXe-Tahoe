#ifndef XE_VME_SUBMIT_CAPTURE_H
#define XE_VME_SUBMIT_CAPTURE_H

#include <stddef.h>
#include <stdint.h>

/* 当前 Tahoe VA 中已核对的 SubmitDataBuffers call 指令偏移。 */
#define XE_VME_SUBMIT_CALLSITE_OFFSET 0xa8f6U

/* 包含 a893 helper 固定字段与 command/sideband 游标的 manager 前缀长度。 */
#define XE_VME_MANAGER_PREFIX_BYTES 0x88U

/* 限制单次诊断复制量；这是采集预算，不代表 VA 的 class 上限。 */
#define XE_VME_CAPTURE_MAX_TOTAL_BYTES (64ULL * 1024ULL * 1024ULL)

typedef size_t (*XeVMECopyBytes)(void *destination, const void *source,
                                 size_t size);

typedef struct {
    uintptr_t address;
    uint64_t size;
    uint64_t storage_offset;
} XeVMEBufferSnapshot;

typedef struct {
    uintptr_t manager;
    uint32_t request;
    uint32_t flags;
    uint32_t fault;
    uint32_t cached_class_count;
    uint32_t manager_prefix_bytes;
    uint64_t captured_bytes;
    XeVMEBufferSnapshot command_buffer;
    XeVMEBufferSnapshot sideband_buffer;
} XeVMEManagerSnapshot;

enum {
    XE_VME_ACTION_FORWARD = 0,
    XE_VME_ACTION_CAPTURE = 1,
    XE_VME_ACTION_REJECT = 2,
    XE_VME_ACTION_CAPTURE_FORWARD = 3,
    XE_VME_ACTION_CAPTURE_STOP = 4,
};

/* 根据调用者身份和显式截停开关选择转发、捕获或拒绝。 */
int xe_vme_submit_action(int capture_enabled, int caller_resolved,
                         int caller_is_va, int callsite_verified,
                         int stop_enabled);

/* 捕获并单次放行非空 submit；后续非空 submit 必须在原调用前停止。 */
int xe_vme_forward_nonempty_action(int forward_enabled, int has_data,
                                   uint32_t forwarded_nonempty_count);

/* 读取 VA 已使用的 ClientShared 字段；这些字段不是 GPU 地址。 */
int xe_vme_read_client_shared_values(const void *client_shared,
                                     uint32_t *value_0x100,
                                     uint8_t *value_0x1c);

/* 验证目标 VA image 的提交调用点指令字节。 */
int xe_vme_callsite_matches(const unsigned char *image, size_t image_size);

/* 捕获 VA helper 的固定前缀，不解引用其中保存的地址。 */
int xe_vme_capture_manager_snapshot(const void *manager, uint32_t request,
                                    uint32_t flags, uint32_t fault,
                                    XeVMECopyBytes copy_bytes,
                                    XeVMEManagerSnapshot *snapshot,
                                    unsigned char *storage, size_t storage_size,
                                    char *error, size_t error_size);

/* 从已复制的 manager 前缀校验 command 与 sideband 的已用范围。 */
int xe_vme_measure_submit_buffers(const void *manager,
                                  const unsigned char *manager_prefix,
                                  uint32_t request, uint32_t flags,
                                  uint32_t fault,
                                  XeVMEManagerSnapshot *snapshot,
                                  char *error, size_t error_size);

/* 判断 submit 控制字段、缓存 class 或任一 command/sideband 字节是否非空。 */
int xe_vme_submit_has_data(const XeVMEManagerSnapshot *snapshot);

/* 识别 A9DC 单独追加的零 command 尾标与一条 type-2 sideband 控制记录。 */
int xe_vme_is_empty_control_flush(const XeVMEManagerSnapshot *snapshot,
                                  const unsigned char *storage,
                                  size_t storage_size);

/* 将 manager 前缀及已校验的 command/sideband 范围复制到一个文件缓冲区。 */
int xe_vme_copy_submit_buffers(const unsigned char *manager_prefix,
                               const XeVMEManagerSnapshot *snapshot,
                               XeVMECopyBytes copy_bytes,
                               unsigned char *storage, size_t storage_size,
                               char *error, size_t error_size);

#endif
