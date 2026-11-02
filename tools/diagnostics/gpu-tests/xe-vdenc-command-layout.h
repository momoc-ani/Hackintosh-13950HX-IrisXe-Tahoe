// 仅供进程内实验：将旧 37 DWORD 地址命令补齐到 Intel Gen12 的 71 DWORD。
#ifndef XE_VDENC_COMMAND_LAYOUT_H
#define XE_VDENC_COMMAND_LAYOUT_H
#include <stdint.h>
#include <stddef.h>
#include <string.h>

// 核对短格式头、容量及 token 长度后移动命令；不改资源索引和引用关系。
static int xe_vdenc_expand_address(unsigned char *batch, size_t capacity, size_t *bytes,
                                   unsigned char *token, size_t token_bytes) {
    const size_t extra = 34 * 4;
    if (token_bytes < 0xce8 || *bytes > capacity || capacity - *bytes < extra) return 1;
    uint32_t source_words, slice_words, stream_words;
    memcpy(&source_words, token + 0xc1c, 4);
    memcpy(&slice_words, token + 0xc20, 4);
    memcpy(&stream_words, token + 0xce0, 4);
    size_t address = (size_t)source_words * 4 + 0x48;
    if (*bytes % 4 ||
        address > *bytes || *bytes - address < 37 * 4 ||
        (size_t)slice_words * 4 < address + 37 * 4 ||
        (size_t)slice_words * 4 >= *bytes) return 2;
    uint32_t header;
    memcpy(&header, batch + address, 4);
    if (header != 0x70840023) return 3;
    // Gen12 前 37 DWORD 的地址位置相同；新增字段保持未使用状态。
    size_t insertion = address + 37 * 4;
    memmove(batch + insertion + extra, batch + insertion, *bytes - insertion);
    memset(batch + insertion, 0, extra);
    header = 0x70840045;
    memcpy(batch + address, &header, 4);
    slice_words += 34;
    stream_words += 34;
    memcpy(token + 0xc20, &slice_words, 4);
    memcpy(token + 0xce0, &stream_words, 4);
    *bytes += extra;
    return 0;
}
#endif
