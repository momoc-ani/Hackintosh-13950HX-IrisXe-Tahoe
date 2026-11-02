// 进程内独立实验：在每个 AVC walker 前写入 Gen12 默认参考权重。
#ifndef XE_VDENC_REFERENCE_STATE_H
#define XE_VDENC_REFERENCE_STATE_H
#include <stdint.h>
#include <stddef.h>
#include <string.h>

// 按真实命令边界寻找 walker，保持所有原有命令、资源及关键帧选择不变。
static inline int xe_vdenc_add_default_weights(unsigned char *batch, size_t capacity,
                                               size_t *bytes, unsigned char *token,
                                               size_t token_bytes) {
    if (token_bytes < 0xce8 || *bytes > capacity || *bytes % 4) return 1;
    uint32_t slice_start, slice_words, stream_words;
    memcpy(&slice_start, token + 0xc20, 4);
    memcpy(&slice_words, token + 0xc24, 4);
    memcpy(&stream_words, token + 0xce0, 4);
    size_t positions[128], count = 0, offset = 0;
    while (offset < *bytes) {
        uint32_t header;
        memcpy(&header, batch + offset, 4);
        size_t words;
        if ((header >> 29) == 0) {
            unsigned opcode = (header >> 23) & 63;
            words = (opcode == 0 || opcode == 5 || opcode == 10) ? 1 : (header & 63) + 2;
        } else {
            words = (header & 4095) + 2;
        }
        if (words > (*bytes - offset) / 4) return 2;
        if ((header >> 16) == 0x7088) return 3;
        if ((header >> 16) == 0x7087) {
            if (count == 128 || offset < (size_t)slice_start * 4 ||
                offset >= ((size_t)slice_start + slice_words) * 4) return 4;
            positions[count++] = offset;
        }
        offset += words * 4;
    }
    if (!count || count * 12 > capacity - *bytes) return 5;
    const uint32_t defaults[] = {0x70880001, 0x00010001, 0x00000001};
    // 从后向前插入，后续命令和各 slice 的权重位置保持可追踪。
    for (size_t i = count; i; --i) {
        offset = positions[i - 1];
        memmove(batch + offset + 12, batch + offset, *bytes - offset);
        memcpy(batch + offset, defaults, sizeof defaults);
        *bytes += 12;
    }
    // 内核以 slice 区起点加区长度定位帧末状态写回，故区长度也必须增加。
    slice_words += (uint32_t)count * 3;
    stream_words += (uint32_t)count * 3;
    memcpy(token + 0xc24, &slice_words, 4);
    memcpy(token + 0xce0, &stream_words, 4);
    return 0;
}
// 按 Gen12 构造器补齐 walker，AVC 宽度取同批次的 source surface；不改参考和等待位。
static inline int xe_vdenc_expand_walker(unsigned char *batch, size_t capacity,
                                         size_t *bytes, unsigned char *token,
                                         size_t token_bytes) {
    if (token_bytes < 0xce8 || *bytes > capacity || *bytes % 4) return 1;
    uint32_t slice_start, slice_words, stream_words;
    memcpy(&slice_start, token + 0xc20, 4);
    memcpy(&slice_words, token + 0xc24, 4);
    memcpy(&stream_words, token + 0xce0, 4);
    size_t positions[128], count = 0, offset = 0;
    uint32_t width_minus_one = 0;
    int found_source = 0;
    while (offset < *bytes) {
        uint32_t header;
        memcpy(&header, batch + offset, 4);
        size_t words;
        if ((header >> 29) == 0) {
            unsigned opcode = (header >> 23) & 63;
            words = (opcode == 0 || opcode == 5 || opcode == 10) ? 1 : (header & 63) + 2;
        } else words = (header & 4095) + 2;
        if (words > (*bytes - offset) / 4) return 2;
        if ((header >> 16) == 0x7081) {
            if (words != 6) return 3;
            memcpy(&width_minus_one, batch + offset + 8, 4);
            width_minus_one = (width_minus_one >> 4) & 0x3fff;
            found_source = 1;
        }
        if ((header >> 16) == 0x7087) {
            if (words != 4 || count == 128 || offset < (size_t)slice_start * 4 ||
                offset >= ((size_t)slice_start + slice_words) * 4) return 4;
            positions[count++] = offset;
        }
        offset += words * 4;
    }
    if (!found_source || !count || count * 92 > capacity - *bytes) return 5;
    // 先验证全部命令再移动，确保失败时不会留下部分改写。
    for (size_t i = count; i; --i) {
        offset = positions[i - 1];
        uint32_t walker[27] = {0};
        memcpy(walker, batch + offset, 16);
        walker[0] = 0x70870019;
        walker[5] = width_minus_one;
        // 这些值来自 Intel Gen12 官方构造器，不能把整个新增尾部全部清零。
        walker[14] = 0x3f400000;
        walker[16] = 0x003f3f3f;
        memmove(batch + offset + 108, batch + offset + 16, *bytes - offset - 16);
        memcpy(batch + offset, walker, sizeof walker);
        *bytes += 92;
    }
    slice_words += (uint32_t)count * 23;
    stream_words += (uint32_t)count * 23;
    memcpy(token + 0xc24, &slice_words, 4);
    memcpy(token + 0xce0, &stream_words, 4);
    return 0;
}

// 单活动参考按 Gen12 oneOnOneMapping 放入槽零；I 帧及多活动参考保持原路径。
static inline int xe_vdenc_map_single_reference(unsigned char *batch, size_t bytes,
                                               unsigned char *token, size_t token_bytes) {
    if (token_bytes < 0xce8 || bytes % 4) return 1;
    size_t positions[128], count = 0, offset = 0;
    unsigned reference_slot = 0;
    int found_slot = 0;
    while (offset < bytes) {
        uint32_t header;
        memcpy(&header, batch + offset, 4);
        size_t words;
        if ((header >> 29) == 0) {
            unsigned opcode = (header >> 23) & 63;
            words = (opcode == 0 || opcode == 5 || opcode == 10) ? 1 : (header & 63) + 2;
        } else words = (header & 4095) + 2;
        if (words > (bytes - offset) / 4) return 2;
        if ((header >> 16) == 0x7103) {
            uint32_t counts;
            if (words < 4) return 3;
            memcpy(&counts, batch + offset + 8, 4);
            if (((counts >> 16) & 63) != 1 || ((counts >> 24) & 63)) return 0;
        }
        if ((header >> 16) == 0x7104) {
            uint32_t list;
            if (words != 10 || count == 128) return 3;
            memcpy(&list, batch + offset + 4, 4);
            if (list != 0 || (batch[offset + 8] & 0x80)) return 0;
            for (size_t index = 9; index < 40; ++index)
                if (batch[offset + index] != 0x80) return 0;
            unsigned slot = (batch[offset + 8] >> 1) & 15;
            if (found_slot && slot != reference_slot) return 4;
            found_slot = 1;
            reference_slot = slot;
            positions[count++] = offset;
        }
        offset += words * 4;
    }
    if (!count) return 0;
    uint32_t selected, active;
    memcpy(&selected, token + 0xc78 + reference_slot * 4, 4);
    memcpy(&active, token + 0xcb8, 4);
    if (!active || (selected & 0xfff) != active) return 5;
    // 先清空旧 DPB 槽，再保留活动引用的资源标志；内核按新槽位回填最终地址。
    memset(token + 0xc78, 0, 64);
    memcpy(token + 0xc78, &selected, 4);
    for (size_t index = 0; index < count; ++index)
        batch[positions[index] + 8] &= (unsigned char)~0x1e;
    return 0;
}
#endif
