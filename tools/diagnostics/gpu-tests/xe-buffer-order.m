// 验证 fill -> buffer 上传 -> GPU 消费的顺序；逐字节比对，不依赖 blit 读回补丁。
// 编译：clang -fobjc-arc -Wall -Wextra xe-buffer-order.m -framework Foundation -framework Metal -o xe-buffer-order
// 用法：./xe-buffer-order --gpu intel --repeats 2（或 amd）
// 顺序诊断：XE_SPLIT_BLIT=1 分开 encoder；XE_COMMIT_EACH=1 逐操作提交并等待。
// 对照：XE_COPIA_NATIVA=1 ./xe-buffer-order --gpu intel；只改变该测试进程。
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    const char *name;
    NSUInteger sourceOffset;
    NSUInteger destinationOffset;
    BOOL fill;
    BOOL chain;
    BOOL patch;
} UploadCase;

static NSString *const kReadbackShader =
    @"#include <metal_stdlib>\nusing namespace metal;\n"
    "// 通过计算通道读取整个缓冲区，避免读回再次进入被测 blit 路径。\n"
    "kernel void snapshot(device const uchar *src [[buffer(0)]],\n"
    " device uchar *dst [[buffer(1)]], constant uint &length [[buffer(2)]],\n"
    " uint i [[thread_position_in_grid]]) { if (i < length) dst[i] = src[i]; }\n";

// 显式选择厂商，保证 Intel 故障与 AMD 对照使用同样的操作序列。
static id<MTLDevice> selectDevice(NSString *name) {
    for (id<MTLDevice> device in MTLCopyAllDevices()) {
        if ([device.name localizedCaseInsensitiveContainsString:name]) return device;
    }
    return nil;
}

// 生成全部非零且随偏移变化的输入，便于识别清零、错位和部分上传。
static uint8_t payload(NSUInteger position, NSUInteger seed) {
    return (uint8_t)((position * 37 + (position >> 7) + seed * 53) % 251 + 1);
}

// 诊断时可在每次操作后结束 encoder，以隔离同一 encoder 内的缓存与依赖问题。
static id<MTLBlitCommandEncoder> nextBlit(id<MTLCommandBuffer> __strong *command, id<MTLBlitCommandEncoder> blit) {
    if (!getenv("XE_SPLIT_BLIT") && !getenv("XE_COMMIT_EACH")) return blit;
    [blit endEncoding];
    if (getenv("XE_COMMIT_EACH")) {
        [*command commit];
        [*command waitUntilCompleted];
        if ((*command).status != MTLCommandBufferStatusCompleted || (*command).error) {
            fprintf(stderr, "中间命令失败：%s\n", (*command).error.description.UTF8String ?: "unknown");
            exit(2);
        }
        *command = [(*command).commandQueue commandBuffer];
    }
    return [*command blitCommandEncoder];
}

// 在同一命令缓冲中执行指定上传序列，并通过独立计算通道验证有效数据及边界。
static BOOL runCase(id<MTLDevice> device, id<MTLCommandQueue> queue,
                    id<MTLComputePipelineState> pipeline, UploadCase test,
                    NSUInteger size, NSUInteger iteration) {
    NSUInteger capacity = (size + 255) & ~(NSUInteger)63;
    id<MTLBuffer> staging = [device newBufferWithLength:capacity options:MTLResourceStorageModeShared];
    id<MTLBuffer> target = [device newBufferWithLength:capacity options:MTLResourceStorageModePrivate];
    id<MTLBuffer> chained = test.chain ? [device newBufferWithLength:capacity options:MTLResourceStorageModePrivate] : nil;
    id<MTLBuffer> snapshot = [device newBufferWithLength:capacity options:MTLResourceStorageModeShared];
    if (!staging || !target || !snapshot || (test.chain && !chained)) return NO;
    uint8_t *input = staging.contents;
    for (NSUInteger i = 0; i < capacity; ++i) input[i] = payload(i, iteration);
    memset(snapshot.contents, 0xcc, capacity);
    NSMutableData *expectedData = [NSMutableData dataWithLength:capacity];
    uint8_t *expected = expectedData.mutableBytes;
    id<MTLCommandBuffer> command = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = [command blitCommandEncoder];
    if (test.fill) {
        [blit fillBuffer:target range:NSMakeRange(0, capacity) value:0];
        blit = nextBlit(&command, blit);
    }
    if (test.chain) {
        [blit fillBuffer:chained range:NSMakeRange(0, capacity) value:0];
        blit = nextBlit(&command, blit);
    }
    [blit copyFromBuffer:staging sourceOffset:test.sourceOffset toBuffer:target
      destinationOffset:test.destinationOffset size:size];
    blit = nextBlit(&command, blit);
    memcpy(expected + test.destinationOffset, input + test.sourceOffset, size);
    if (test.patch) {
        // 对齐与非对齐写入同一资源，覆盖 Dawn 初始化后追加数据的顺序。
        NSUInteger patchSize = (size / 2) & ~(NSUInteger)3;
        [blit copyFromBuffer:staging sourceOffset:4 toBuffer:target
          destinationOffset:test.destinationOffset size:patchSize];
        blit = nextBlit(&command, blit);
        memcpy(expected + test.destinationOffset, input + 4, patchSize);
    }
    if (test.chain) {
        [blit copyFromBuffer:target sourceOffset:test.destinationOffset toBuffer:chained
          destinationOffset:0 size:size];
        memmove(expected, expected + test.destinationOffset, size);
        memset(expected + size, 0, capacity - size);
    }
    [blit endEncoding];
    id<MTLComputeCommandEncoder> compute = [command computeCommandEncoder];
    [compute setComputePipelineState:pipeline];
    [compute setBuffer:test.chain ? chained : target offset:0 atIndex:0];
    [compute setBuffer:snapshot offset:0 atIndex:1];
    uint32_t length = (uint32_t)capacity;
    [compute setBytes:&length length:sizeof length atIndex:2];
    NSUInteger width = MIN((NSUInteger)64, pipeline.maxTotalThreadsPerThreadgroup);
    [compute dispatchThreadgroups:MTLSizeMake((capacity + width - 1) / width, 1, 1)
           threadsPerThreadgroup:MTLSizeMake(width, 1, 1)];
    [compute endEncoding];
    [command commit];
    [command waitUntilCompleted];
    if (command.status != MTLCommandBufferStatusCompleted || command.error) {
        printf("FAIL case=%s size=%lu iteration=%lu status=%lu error=%s\n", test.name,
               (unsigned long)size, (unsigned long)iteration, (unsigned long)command.status,
               command.error.description.UTF8String ?: "none");
        return NO;
    }
    const uint8_t *actual = snapshot.contents;
    NSUInteger mismatches = 0, first = NSNotFound;
    // 未清零用例只验收已上传区域；清零用例同时验收前后未写区域。
    NSUInteger begin = test.fill || test.chain ? 0 : test.destinationOffset;
    NSUInteger end = test.fill || test.chain ? capacity : test.destinationOffset + size;
    for (NSUInteger i = begin; i < end; ++i) {
        if (actual[i] != expected[i]) { if (first == NSNotFound) first = i; ++mismatches; }
    }
    if (mismatches) {
        printf("FAIL case=%s size=%lu so=%lu dst=%lu iteration=%lu mismatches=%lu/%lu first=%lu got=%u expected=%u\n",
               test.name, (unsigned long)size, (unsigned long)test.sourceOffset,
               (unsigned long)test.destinationOffset, (unsigned long)iteration,
               (unsigned long)mismatches, (unsigned long)(end - begin),
               (unsigned long)first, actual[first], expected[first]);
    }
    return mismatches == 0;
}

// 运行尺寸、偏移、混合路径和私有缓冲链的交叉矩阵；退出码直接反映像素输入是否正确。
int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSString *gpu = @"intel";
        NSUInteger repeats = 2;
        for (int i = 1; i < argc; ++i) {
            if (!strcmp(argv[i], "--gpu") && i + 1 < argc) gpu = @(argv[++i]);
            else if (!strcmp(argv[i], "--repeats") && i + 1 < argc) repeats = (NSUInteger)strtoul(argv[++i], NULL, 10);
            else { fprintf(stderr, "用法：%s --gpu intel|amd --repeats 2\n", argv[0]); return 2; }
        }
        if (!repeats || repeats > 100) return 2;
        id<MTLDevice> device = selectDevice(gpu);
        if (!device) { fprintf(stderr, "没有匹配的 GPU\n"); return 2; }
        NSError *error = nil;
        id<MTLLibrary> library = [device newLibraryWithSource:kReadbackShader options:nil error:&error];
        id<MTLComputePipelineState> pipeline = library ? [device newComputePipelineStateWithFunction:[library newFunctionWithName:@"snapshot"] error:&error] : nil;
        if (!pipeline) { fprintf(stderr, "snapshot pipeline: %s\n", error.description.UTF8String); return 2; }
        id<MTLCommandQueue> queue = [device newCommandQueue];
        const NSUInteger sizes[] = {8,24,64,256,4096,65536,131072,1048576,4194372};
        const UploadCase cases[] = {
            {"no-fill-aligned", 0,0,NO,NO,NO},
            {"no-fill-unaligned-src", 4,0,NO,NO,NO},
            {"no-fill-unaligned-dst", 0,4,NO,NO,NO},
            {"fill-aligned-0", 0,0,YES,NO,NO},
            {"fill-aligned-4", 4,4,YES,NO,NO},
            {"fill-aligned-60", 60,60,YES,NO,NO},
            {"fill-aligned-64", 64,64,YES,NO,NO},
            {"fill-unaligned-src", 4,0,YES,NO,NO},
            {"fill-unaligned-dst", 0,4,YES,NO,NO},
            {"fill-aligned-then-unaligned", 0,0,YES,NO,YES},
            {"fill-aligned-private-chain", 0,0,YES,YES,NO},
            {"fill-unaligned-private-chain", 4,0,YES,YES,NO},
        };
        printf("GPU=%s registryID=%llu headless=%d repeats=%lu native-copy=%d native-all=%d\n",
               device.name.UTF8String, (unsigned long long)device.registryID, device.headless,
               (unsigned long)repeats, getenv("XE_COPIA_NATIVA") != NULL, getenv("XE_FILL_NATIVO") != NULL);
        NSUInteger tests = 0, failures = 0;
        for (NSUInteger repeat = 0; repeat < repeats; ++repeat) {
            for (NSUInteger i = 0; i < sizeof sizes / sizeof *sizes; ++i) {
                for (NSUInteger j = 0; j < sizeof cases / sizeof *cases; ++j) {
                    @autoreleasepool {
                        ++tests;
                        if (!runCase(device, queue, pipeline, cases[j], sizes[i], repeat)) ++failures;
                    }
                }
            }
            printf("iteration=%lu tests=%lu failures=%lu\n", (unsigned long)repeat,
                   (unsigned long)tests, (unsigned long)failures);
            fflush(stdout);
        }
        printf("tests=%lu failures=%lu\n", (unsigned long)tests, (unsigned long)failures);
        return failures ? 1 : 0;
    }
}
