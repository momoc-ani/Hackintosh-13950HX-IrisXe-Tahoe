// VA 帧命令诊断：只读取请求字段，在原调度入口前结束进程，不提交 GPU 命令。
#include "xe-va-command-trace.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// 从 VA 的已核对字段读取 32 位数值，避免未对齐访问。
static uint32_t command_read32(const unsigned char *source) {
    uint32_t value;
    memcpy(&value, source, sizeof(value));
    return value;
}

// 读取 64 位字段本身，不解引用字段中保存的指针。
static uint64_t command_read64(const unsigned char *source) {
    uint64_t value;
    memcpy(&value, source, sizeof(value));
    return value;
}

static unsigned xe_va_statistics_schedules_forwarded;

// 仅匹配已经核对过的单节点 1280x720 FrameStatisticsContext 调度。
static int xe_va_is_single_statistics_request(const unsigned char *header) {
    if (command_read32(header) != 1) return 0;
    const unsigned char *const *roots =
        (const unsigned char *const *)(uintptr_t)command_read64(header + 8);
    if (!roots || !roots[0]) return 0;

    const unsigned char *root = roots[0];
    if (command_read32(root) != 2 || command_read64(root + 8) != 0x1000 ||
        !command_read64(root + 0x18) || !command_read64(root + 0x20)) return 0;
    const unsigned char *context =
        (const unsigned char *)(uintptr_t)command_read64(root + 0x18);
    const unsigned char *output =
        (const unsigned char *)(uintptr_t)command_read64(root + 0x30);
    if (command_read32(context + 0x138) != 1280 ||
        command_read32(context + 0x13c) != 720 ||
        command_read32(context + 0x150) != 16 ||
        command_read32(context + 0x158) != 16 ||
        command_read32(context + 0x168) != 1 ||
        command_read32(context + 0x4590) != 0x2050 || !output) return 0;
    return command_read32(output) == 16 &&
        command_read64(output + 8) == 0x20 &&
        command_read64(output + 0x18) == 0 &&
        command_read64(output + 0x20) != 0 &&
        command_read64(output + 0x30) == 0;
}

// 只记录 VDENC 已使用的帧参数槽，用于比较普通编码与低延迟编码的实际数据布局。
static void trace_frame_fields(const unsigned char *frame, uint32_t root, unsigned depth) {
    static const uint32_t offsets[] = {
        0x8, 0x410, 0x418, 0x420, 0x428, 0x430, 0x438, 0x440, 0x448,
        0x450, 0x458, 0x460, 0x468, 0x470, 0x478, 0x480, 0x488,
        0x490, 0x498, 0x4a0
    };
    for (size_t i = 0; i < sizeof(offsets) / sizeof(offsets[0]); i++) {
        uint32_t offset = offsets[i];
        fprintf(stderr, "VA-COMMAND {\"event\":\"frame-field\",\"root\":%u,\"depth\":%u,\"offset\":\"0x%x\",\"value\":\"0x%llx\"}\n",
                root, depth, offset, (unsigned long long)command_read64(frame + offset));
    }
}

// 按 0x22236 的实际读取宽度采集统计任务；表面字段只记录数值，不解引用。
static void trace_statistics_fields(const unsigned char *frame, const unsigned char *context,
                                     uint32_t root, unsigned depth) {
    fprintf(stderr, "VA-COMMAND {\"event\":\"frame-statistics\",\"root\":%u,\"depth\":%u,\"current_surface\":\"0x%llx\",\"previous_surface\":\"0x%llx\",\"statistics_slot\":%u,\"blocks_before_schedule\":%u}\n",
            root, depth, (unsigned long long)command_read64(frame),
            (unsigned long long)command_read64(frame + 8),
            command_read32(frame + 0x10), command_read32(frame + 0x14));
    if (context) {
        // 构造与初始化分别证实这些字段属于 0x4730 字节的 FrameStatisticsContext。
        fprintf(stderr, "VA-COMMAND {\"event\":\"frame-statistics-context\",\"root\":%u,\"depth\":%u,\"width\":%u,\"height\":%u,\"block_width\":%u,\"block_height\":%u,\"curbe_bytes\":%u,\"kernel_count\":%u,\"kernel_bytes\":%u}\n",
                root, depth, command_read32(context + 0x138), command_read32(context + 0x13c),
                command_read32(context + 0x150), command_read32(context + 0x158),
                command_read32(context + 0x160), command_read32(context + 0x168),
                command_read32(context + 0x4590));
        // 只记录容量、占用和地址数值，不解引用 kernel buffer 指针。
        fprintf(stderr, "VA-COMMAND {\"event\":\"frame-statistics-storage\",\"root\":%u,\"depth\":%u,\"capacity\":%u,\"used_bytes\":%u,\"storage_base\":\"0x%llx\",\"kernel_copy\":\"0x%llx\",\"kernel_bytes\":%u}\n",
                root, depth, command_read32(context + 0x190), command_read32(context + 0x16c),
                (unsigned long long)command_read64(context + 0x198),
                (unsigned long long)command_read64(context + 0x4390),
                command_read32(context + 0x4590));
        // 0x2c84a 从 context+8 指向的对象读取该 32 位线程字段；不使用调度入口参数。
        const unsigned char *statistics_accelerator =
            (const unsigned char *)(uintptr_t)command_read64(context + 8);
        fprintf(stderr, "VA-COMMAND {\"event\":\"frame-statistics-threads\",\"root\":%u,\"depth\":%u,\"accelerator\":\"0x%llx\",\"field_offset\":\"0xa868\",\"hardware_threads\":",
                root, depth, (unsigned long long)(uintptr_t)statistics_accelerator);
        if (statistics_accelerator) {
            fprintf(stderr, "%u", command_read32(statistics_accelerator + 0xa868));
        } else {
            fputs("null", stderr);
        }
        fputs("}\n", stderr);

        // 这些是原调度前的存储槽；全零表示此处尚无命令头，不能当作最终 GPU 命令。
        fprintf(stderr, "VA-COMMAND {\"event\":\"frame-statistics-command-state\",\"root\":%u,\"depth\":%u,\"capture_phase\":\"before-original-schedule\",\"vfe_dwords\":[",
                root, depth);
        for (size_t i = 0; i < 9; i++) {
            fprintf(stderr, "%s%u", i ? "," : "", command_read32(context + 0x427c + i * 4));
        }
        fputs("],\"curbe_load_dwords\":[", stderr);
        for (size_t i = 0; i < 4; i++) {
            fprintf(stderr, "%s%u", i ? "," : "", command_read32(context + 0x42c4 + i * 4));
        }
        fputs("],\"descriptor_load_dwords\":[", stderr);
        for (size_t i = 0; i < 4; i++) {
            fprintf(stderr, "%s%u", i ? "," : "", command_read32(context + 0x42e4 + i * 4));
        }
        fputs("]}\n", stderr);
    }
}

// 保留默认调用行为；诊断开关启用时仅读取命令与帧字段，然后在原 VA 调度前退出。
int xe_va_trace_schedule(void *accelerator, uint32_t *request, void *output,
                         XeVAScheduleFunction original) {
    const char *enabled = getenv("XE_VA_STOP_BEFORE_SCHEDULE");
    const char *remapped = getenv("XE_USAGE0_REMAP");
    const char *allow_statistics = getenv("XE_VA_ALLOW_ONE_FRAME_STATISTICS");
    int stop_enabled = enabled && strcmp(enabled, "1") == 0;
    int remap_enabled = remapped && strcmp(remapped, "1") == 0;
    int allow_statistics_enabled = allow_statistics &&
        strcmp(allow_statistics, "1") == 0;
    // 该映射已实测导致 RCS 重置和段错误，启用映射即强制截停，避免重走失败路径。
    if (!stop_enabled && !remap_enabled && !allow_statistics_enabled) {
        return original(accelerator, request, output);
    }

    const unsigned char *header = (const unsigned char *)request;
    uint32_t count = command_read32(header);
    const unsigned char *const *roots =
        (const unsigned char *const *)(uintptr_t)command_read64(header + 8);
    fprintf(stderr, "VA-COMMAND {\"event\":\"schedule\",\"roots\":%u}\n", count);
    for (uint32_t root = 0; root < count; root++) {
        unsigned depth = 0;
        int statistics_output = 0;
        for (const unsigned char *node = roots[root]; node;
             node = (const unsigned char *)(uintptr_t)command_read64(node + 0x30)) {
            uint32_t kind = command_read32(node);
            uint64_t capability = command_read64(node + 8);
            int context_present = command_read64(node + 0x18) != 0;
            const unsigned char *frame =
                (const unsigned char *)(uintptr_t)command_read64(node + 0x20);
            fprintf(stderr, "VA-COMMAND {\"event\":\"node\",\"root\":%u,\"depth\":%u,\"kind\":%u,\"capability\":\"0x%llx\",\"context_present\":%s,\"frame_present\":%s}\n",
                    root, depth, kind, (unsigned long long)capability,
                    context_present ? "true" : "false", frame ? "true" : "false");
            if (frame && kind == 0x20 && (capability == 0x400 || capability == 0x4000)) {
                trace_frame_fields(frame, root, depth);
            }
            if (frame && kind == 2 && capability == 0x1000) {
                trace_statistics_fields(frame,
                    (const unsigned char *)(uintptr_t)command_read64(node + 0x18), root, depth);
            } else if (frame && statistics_output && kind == 16 && capability == 0x20) {
                fprintf(stderr, "VA-COMMAND {\"event\":\"frame-statistics-output\",\"root\":%u,\"depth\":%u,\"output_surface\":\"0x%llx\"}\n",
                        root, depth, (unsigned long long)command_read64(frame + 8));
            }
            statistics_output = kind == 2 && capability == 0x1000;
            depth++;
        }
    }

    if (!stop_enabled && remap_enabled && allow_statistics_enabled &&
        xe_va_statistics_schedules_forwarded == 0 &&
        xe_va_is_single_statistics_request(header)) {
        xe_va_statistics_schedules_forwarded = 1;
        fprintf(stderr, "VA-COMMAND {\"event\":\"allow-one-statistics-only\",\"count\":1}\n");
        int status = original(accelerator, request, output);
        fprintf(stderr, "VA-COMMAND {\"event\":\"statistics-only-result\",\"status\":%d}\n",
                status);
        return status;
    }

    // 诊断和已知不兼容映射均在调用原 ScheduleAVDCommands 前退出。
    fprintf(stderr, "VA-COMMAND {\"event\":\"stop-before-original\",\"exit_code\":77}\n");
    fflush(stderr);
    _exit(77);
}
