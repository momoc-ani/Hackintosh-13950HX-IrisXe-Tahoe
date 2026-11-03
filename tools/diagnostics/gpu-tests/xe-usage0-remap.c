// 仅供创建或原 VA 调度前截停诊断；Usage=0 帧参数缺少 VDENC 所需字段，不可用于真实编码。
#include <stdint.h>
#include <string.h>

#define XE_VA_NODE_KIND_OFFSET 0x00
#define XE_VA_NODE_CAPABILITY_OFFSET 0x08
#define XE_VA_NODE_NEXT_OFFSET 0x38
#define XE_VA_PICTURE_ENCODE_KIND 0x20
#define XE_VA_CAPABILITY_ORDINARY 0x4000
#define XE_VA_CAPABILITY_VDENC 0x400

// 遍历一个 VA 请求链，只改写 kind=0x20 且 capability=0x4000 的节点。
int xe_usage0_remap_chain(unsigned char *node) {
    int remapped = 0;

    while (node != NULL) {
        uint32_t kind;
        uint64_t capability;
        uintptr_t next;

        memcpy(&kind, node + XE_VA_NODE_KIND_OFFSET, sizeof(kind));
        memcpy(&capability, node + XE_VA_NODE_CAPABILITY_OFFSET, sizeof(capability));
        if (kind == XE_VA_PICTURE_ENCODE_KIND && capability == XE_VA_CAPABILITY_ORDINARY) {
            capability = XE_VA_CAPABILITY_VDENC;
            memcpy(node + XE_VA_NODE_CAPABILITY_OFFSET, &capability, sizeof(capability));
            remapped++;
        }

        memcpy(&next, node + XE_VA_NODE_NEXT_OFFSET, sizeof(next));
        node = (unsigned char *)next;
    }

    return remapped;
}

// 遍历 0x30 字节的参数项数组，只改写匹配的 picture-encode capability。
int xe_usage0_remap_parameter_entries(unsigned char *entries, uint32_t count) {
    int remapped = 0;

    for (uint32_t i = 0; entries != NULL && i < count; i++) {
        unsigned char *entry = entries + i * 0x30;
        uint32_t kind;
        uint64_t capability;

        memcpy(&kind, entry + XE_VA_NODE_KIND_OFFSET, sizeof(kind));
        memcpy(&capability, entry + XE_VA_NODE_CAPABILITY_OFFSET, sizeof(capability));
        if (kind == XE_VA_PICTURE_ENCODE_KIND && capability == XE_VA_CAPABILITY_ORDINARY) {
            capability = XE_VA_CAPABILITY_VDENC;
            memcpy(entry + XE_VA_NODE_CAPABILITY_OFFSET, &capability, sizeof(capability));
            remapped++;
        }
    }

    return remapped;
}
