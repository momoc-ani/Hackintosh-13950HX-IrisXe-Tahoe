// 复现 GT1 的 URB 越界导致系统 ID 和顶点位置丢失；不以命令完成代替像素验收。
// 编译：clang -fobjc-arc -Wall -Wextra xe-vertex-urb.m -framework Foundation -framework Metal -o xe-vertex-urb
// 基线：./xe-vertex-urb --gpu intel（或 amd）
// 进程内对照：./xe-vertex-urb --gpu intel --l3-banks 4
// 对照只修改本进程的 TGL 能力结构，退出即消失；不写驱动、EFI 或 NVRAM。
#import <Metal/Metal.h>
#import <objc/runtime.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { kSide = 64, kSlots = 8 };

static NSString *const kShader =
    @"#include <metal_stdlib>\nusing namespace metal;\n"
    "struct Out { float4 position [[position]]; };\n"
    "struct PointOut { float4 position [[position]]; float size [[point_size]]; };\n"
    "struct In { float2 position [[attribute(0)]]; };\n"
    "// 以原子计数分配独立槽位，避免错误 ID 互相覆盖记录。\n"
    "void record(device atomic_uint *r, uint v, uint i, float2 p) {\n"
    " uint slot = atomic_fetch_add_explicit(r, 1u, memory_order_relaxed);\n"
    " if (slot < 8) {\n"
    "  atomic_store_explicit(&r[1+slot*3], ((v<<16)|i)+1, memory_order_relaxed);\n"
    "  atomic_store_explicit(&r[2+slot*3], as_type<uint>(p.x), memory_order_relaxed);\n"
    "  atomic_store_explicit(&r[3+slot*3], as_type<uint>(p.y), memory_order_relaxed);\n"
    " }\n}\n"
    "// 用系统 ID 生成覆盖整个目标的三角形。\n"
    "vertex Out systemValues(uint v [[vertex_id]], uint i [[instance_id]],\n"
    " device atomic_uint *r [[buffer(0)]]) {\n"
    " const float2 p[3] = {float2(-1,-1),float2(3,-1),float2(-1,3)};\n"
    " float2 q = p[v < 3 ? v : 0]; record(r,v,i,q); return {float4(q,0,1)};\n}\n"
    "// 独立验证 VF 从顶点缓冲读取的数据能传给着色器。\n"
    "vertex Out attributes(In in [[stage_in]], uint v [[vertex_id]],\n"
    " uint i [[instance_id]], device atomic_uint *r [[buffer(0)]]) {\n"
    " record(r,v,i,in.position); return {float4(in.position,0,1)};\n}\n"
    "// 固定位置的点不依赖 vertex_id 来生成几何。\n"
    "vertex PointOut point(uint v [[vertex_id]], uint i [[instance_id]],\n"
    " device atomic_uint *r [[buffer(0)]]) {\n"
    " record(r,v,i,float2(0)); return {float4(0,0,0,1),16};\n}\n"
    "// 写入精确可比的 BGRA8 蓝色。\n"
    "fragment half4 blue() { return half4(0,0,1,1); }\n"
    "// 对照计算通道，区分全部执行失败与顶点数据通路失败。\n"
    "kernel void compute(device uint *r [[buffer(0)]], uint i [[thread_position_in_grid]]) {\n"
    " r[i] = i*3u+7u;\n}\n";

// 选择指定厂商设备，保证 AMD 对照与 Intel 用例各自显式执行。
static id<MTLDevice> selectDevice(NSString *name) {
    for (id<MTLDevice> candidate in MTLCopyAllDevices()) {
        if ([candidate.name localizedCaseInsensitiveContainsString:name]) return candidate;
    }
    return nil;
}

// 在创建任何 pipeline 前更改能力字段，让驱动自行重新计算完整 URB 状态。
static BOOL configureProcessL3(id<MTLDevice> device, BOOL change) {
    Ivar ivar = class_getInstanceVariable(object_getClass(device), "hwInfo");
    if (![device.name localizedCaseInsensitiveContainsString:@"Intel"] || !ivar) {
        if (change) fprintf(stderr, "L3 对照要求 TGL 的 hwInfo 布局\n");
        return !change;
    }
    unsigned char *caps = (unsigned char *)(__bridge void *)device + ivar_getOffset(ivar);
    uint32_t eu, slices, banks;
    memcpy(&eu, caps + 0x14, sizeof eu);
    memcpy(&slices, caps + 0x4c, sizeof slices);
    memcpy(&banks, caps + 0x54, sizeof banks);
    printf("caps EU=%u slices=%u L3=%u", eu, slices, banks);
    if (change) {
        if (eu != 32 || slices != 1) {
            fprintf(stderr, "\n此对照只适用于已核对的 32 EU / 1 slice GT1\n");
            return NO;
        }
        uint32_t corrected = 4;
        memcpy(caps + 0x54, &corrected, sizeof corrected);
        printf(" -> %u (仅本进程)", corrected);
    }
    puts("");
    return YES;
}

// 比较数值而不是字符串顺序，GPU 的顶点调用顺序并不固定。
static int compareIDs(const void *left, const void *right) {
    uint32_t a = *(const uint32_t *)left, b = *(const uint32_t *)right;
    return (a > b) - (a < b);
}

// 检查真实 GPU 的系统 ID 组合和顶点坐标；原子槽位捕获重复 ID。
static BOOL checkRecords(const uint32_t *records, BOOL point, BOOL attributes) {
    const uint32_t expectedIDs[] = {1, 2, 65537, 65538, 131073, 131074};
    const uint32_t expectedPositions[][2] = {
        {0xbf800000, 0xbf800000}, {0x40400000, 0xbf800000}, {0xbf800000, 0x40400000}
    };
    NSUInteger count = point ? 1 : 6;
    uint32_t ids[kSlots];
    BOOL valid = records[0] == count;
    printf(" invocations=%u IDs+1=", records[0]);
    for (NSUInteger slot = 0; slot < count; ++slot) {
        ids[slot] = records[1 + slot * 3];
        printf("%s%u", slot ? "," : "", ids[slot]);
        if (attributes) {
            uint32_t vertex = (ids[slot] - 1) >> 16;
            if (vertex > 2 || records[2 + slot * 3] != expectedPositions[vertex][0] ||
                records[3 + slot * 3] != expectedPositions[vertex][1]) valid = NO;
        }
    }
    qsort(ids, count, sizeof ids[0], compareIDs);
    for (NSUInteger i = 0; i < count; ++i) valid &= ids[i] == expectedIDs[i];
    return valid;
}

// 同时验收顶点输入记录和实际目标像素，捕获 Completed 但没有渲染的情况。
static BOOL runRender(id<MTLDevice> device, id<MTLLibrary> library, NSString *name) {
    BOOL point = [name isEqualToString:@"point"];
    BOOL attributes = [name isEqualToString:@"attributes"];
    MTLRenderPipelineDescriptor *descriptor = [MTLRenderPipelineDescriptor new];
    descriptor.vertexFunction = [library newFunctionWithName:name];
    descriptor.fragmentFunction = [library newFunctionWithName:@"blue"];
    descriptor.colorAttachments[0].pixelFormat = MTLPixelFormatBGRA8Unorm;
    if (attributes) {
        descriptor.vertexDescriptor = [MTLVertexDescriptor vertexDescriptor];
        descriptor.vertexDescriptor.attributes[0].format = MTLVertexFormatFloat2;
        descriptor.vertexDescriptor.attributes[0].bufferIndex = 1;
        descriptor.vertexDescriptor.layouts[1].stride = sizeof(float) * 2;
    }
    NSError *error = nil;
    id<MTLRenderPipelineState> pipeline = [device newRenderPipelineStateWithDescriptor:descriptor error:&error];
    if (!pipeline) {
        fprintf(stderr, "%s pipeline: %s\n", name.UTF8String, error.description.UTF8String);
        return NO;
    }
    MTLTextureDescriptor *td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                                                               width:kSide height:kSide mipmapped:NO];
    td.usage = MTLTextureUsageRenderTarget;
    td.storageMode = MTLStorageModeManaged;
    id<MTLTexture> texture = [device newTextureWithDescriptor:td];
    id<MTLBuffer> seen = [device newBufferWithLength:(1 + kSlots * 3) * sizeof(uint32_t)
                                          options:MTLResourceStorageModeShared];
    const float positions[] = {-1,-1, 3,-1, -1,3};
    id<MTLBuffer> vertices = [device newBufferWithBytes:positions length:sizeof positions options:MTLResourceStorageModeShared];
    if (!texture || !seen || !vertices) return NO;
    memset(seen.contents, 0, seen.length);
    MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = texture;
    pass.colorAttachments[0].loadAction = MTLLoadActionClear;
    pass.colorAttachments[0].clearColor = MTLClearColorMake(1,1,1,1);
    pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    id<MTLCommandBuffer> command = [[device newCommandQueue] commandBuffer];
    id<MTLRenderCommandEncoder> encoder = [command renderCommandEncoderWithDescriptor:pass];
    [encoder setRenderPipelineState:pipeline];
    [encoder setViewport:(MTLViewport){0,0,kSide,kSide,0,1}];
    [encoder setScissorRect:(MTLScissorRect){0,0,kSide,kSide}];
    [encoder setVertexBuffer:seen offset:0 atIndex:0];
    if (attributes) [encoder setVertexBuffer:vertices offset:0 atIndex:1];
    [encoder drawPrimitives:point ? MTLPrimitiveTypePoint : MTLPrimitiveTypeTriangle
               vertexStart:0 vertexCount:point ? 1 : 3 instanceCount:point ? 1 : 2];
    [encoder endEncoding];
    id<MTLBlitCommandEncoder> blit = [command blitCommandEncoder];
    [blit synchronizeResource:texture];
    [blit endEncoding];
    [command commit];
    [command waitUntilCompleted];
    printf("%s status=%lu", name.UTF8String, (unsigned long)command.status);
    if (command.status != MTLCommandBufferStatusCompleted || command.error) {
        printf(" FAIL %s\n", command.error.description.UTF8String);
        return NO;
    }
    BOOL recordsOK = checkRecords(seen.contents, point, attributes);
    uint8_t pixels[kSide * kSide * 4];
    [texture getBytes:pixels bytesPerRow:kSide * 4 fromRegion:MTLRegionMake2D(0,0,kSide,kSide) mipmapLevel:0];
    NSUInteger blue = 0, white = 0;
    for (NSUInteger i = 0; i < kSide * kSide; ++i) {
        const uint8_t *p = pixels + i * 4;
        blue += p[0] == 255 && p[1] == 0 && p[2] == 0 && p[3] == 255;
        white += p[0] == 255 && p[1] == 255 && p[2] == 255 && p[3] == 255;
    }
    BOOL pixelsOK = point ? blue == 256 && white == 3840 : blue == 4096;
    printf(" blue=%lu white=%lu %s\n", (unsigned long)blue, (unsigned long)white,
           recordsOK && pixelsOK ? "PASS" : "FAIL");
    return recordsOK && pixelsOK;
}

// 验证 compute 的全部结果，以保留渲染与计算通道的对照。
static BOOL runCompute(id<MTLDevice> device, id<MTLLibrary> library) {
    NSError *error = nil;
    id<MTLComputePipelineState> pipeline = [device newComputePipelineStateWithFunction:[library newFunctionWithName:@"compute"] error:&error];
    if (!pipeline) { fprintf(stderr,"compute pipeline: %s\n", error.description.UTF8String); return NO; }
    id<MTLBuffer> result = [device newBufferWithLength:4096 * sizeof(uint32_t) options:MTLResourceStorageModeShared];
    if (!result) return NO;
    memset(result.contents, 0, result.length);
    id<MTLCommandBuffer> command = [[device newCommandQueue] commandBuffer];
    id<MTLComputeCommandEncoder> encoder = [command computeCommandEncoder];
    [encoder setComputePipelineState:pipeline];
    [encoder setBuffer:result offset:0 atIndex:0];
    [encoder dispatchThreadgroups:MTLSizeMake(64,1,1) threadsPerThreadgroup:MTLSizeMake(64,1,1)];
    [encoder endEncoding];
    [command commit];
    [command waitUntilCompleted];
    NSUInteger correct = 0;
    for (NSUInteger i = 0; i < 4096; ++i) correct += ((uint32_t *)result.contents)[i] == i * 3 + 7;
    BOOL valid = command.status == MTLCommandBufferStatusCompleted && !command.error && correct == 4096;
    printf("compute status=%lu correct=%lu/4096 %s\n", (unsigned long)command.status,
           (unsigned long)correct, valid ? "PASS" : "FAIL");
    return valid;
}

// 运行所选 GPU 的真实输入与像素回归；失败退出 1，环境或用法问题退出 2。
int main(int argc, const char **argv) { @autoreleasepool {
    NSString *gpu = @"Intel";
    BOOL changeL3 = NO;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--gpu") && i + 1 < argc) gpu = @(argv[++i]);
        else if (!strcmp(argv[i], "--l3-banks") && i + 1 < argc && !strcmp(argv[i+1], "4")) { changeL3 = YES; ++i; }
        else { fprintf(stderr,"usage: %s [--gpu intel|amd] [--l3-banks 4]\n",argv[0]); return 2; }
    }
    id<MTLDevice> device = selectDevice(gpu);
    if (!device) { fprintf(stderr,"没有找到 GPU: %s\n",gpu.UTF8String); return 2; }
    printf("GPU=%s registryID=%llu headless=%d\n", device.name.UTF8String, device.registryID, device.headless);
    if (!configureProcessL3(device, changeL3)) return 2;
    NSError *error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:kShader options:nil error:&error];
    if (!library) { fprintf(stderr,"library: %s\n",error.description.UTF8String); return 2; }
    unsigned failures = 0;
    failures += !runCompute(device, library);
    for (NSString *name in @[@"systemValues", @"attributes", @"point"]) failures += !runRender(device, library, name);
    printf("tests=4 failures=%u\n", failures);
    return failures ? 1 : 0;
} }
