// 检查当前安装驱动能否创建指定 GPU 的硬件编码会话；不把会话创建当成编码画面通过。
// 编译：clang -fobjc-arc -Wall -Wextra -Werror xe-encode-capability.m \
//   -framework Foundation -framework Metal -framework VideoToolbox -o xe-encode-capability
// 用法：xe-encode-capability intel|amd h264|hevc
#import <VideoToolbox/VideoToolbox.h>
#import <Metal/Metal.h>
#include <stdio.h>
#include <string.h>

// 显式选择目标 GPU，避免默认编码器回退被误当成 Intel 编码通过。
static id<MTLDevice> selectDevice(NSString *vendor) {
    for (id<MTLDevice> candidate in MTLCopyAllDevices()) {
        if ([candidate.name localizedCaseInsensitiveContainsString:vendor]) return candidate;
    }
    return nil;
}

// 读取硬件使用状态、实际公布的 GPU ID 和编码器 ID，保留 API 错误码。
static void describeSession(VTCompressionSessionRef session) {
    for (id key in @[(__bridge id)kVTCompressionPropertyKey_UsingHardwareAcceleratedVideoEncoder,
                     (__bridge id)kVTCompressionPropertyKey_UsingGPURegistryID,
                     (__bridge id)kVTCompressionPropertyKey_EncoderID]) {
        CFTypeRef value = NULL;
        OSStatus status = VTSessionCopyProperty(session, (__bridge CFStringRef)key, NULL, &value);
        printf("%s status=%d value=%s\n", [key description].UTF8String, (int)status,
               value ? [(__bridge id)value description].UTF8String : "null");
        if (value) CFRelease(value);
    }
}

// 仅创建和销毁当前支持的会话，未提交图像，不能据此验收编码质量或连续帧稳定性。
int main(int argc, const char **argv) { @autoreleasepool {
    if (argc != 3 || (strcmp(argv[2], "h264") && strcmp(argv[2], "hevc"))) {
        fprintf(stderr, "usage: %s intel|amd h264|hevc\n", argv[0]); return 2;
    }
    id<MTLDevice> device = selectDevice(@(argv[1]));
    if (!device) return 2;
    CMVideoCodecType codec = !strcmp(argv[2], "h264") ? kCMVideoCodecType_H264 : kCMVideoCodecType_HEVC;
    NSDictionary *spec = @{
        (__bridge id)kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder:@YES,
        (__bridge id)kVTVideoEncoderSpecification_RequiredEncoderGPURegistryID:@(device.registryID)
    };
    VTCompressionSessionRef session = NULL;
    OSStatus status = VTCompressionSessionCreate(NULL, 640, 480, codec,
        (__bridge CFDictionaryRef)spec, NULL, NULL, NULL, NULL, &session);
    printf("GPU=%s registryID=%llu codec=%s create=%d\n", device.name.UTF8String,
           device.registryID, argv[2], (int)status);
    if (status) return 1;
    describeSession(session);
    puts("session-created-only; encoded-frame validation still required");
    VTCompressionSessionInvalidate(session); CFRelease(session);
    return 0;
} }
