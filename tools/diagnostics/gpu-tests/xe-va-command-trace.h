#ifndef XE_VA_COMMAND_TRACE_H
#define XE_VA_COMMAND_TRACE_H

#include <stdint.h>

typedef int (*XeVAScheduleFunction)(void *, uint32_t *, void *);

// 默认转发原调度；开启采集或不兼容映射时记录字段，并在调用原入口前退出探针。
int xe_va_trace_schedule(void *accelerator, uint32_t *request, void *output,
                         XeVAScheduleFunction original);

#endif
