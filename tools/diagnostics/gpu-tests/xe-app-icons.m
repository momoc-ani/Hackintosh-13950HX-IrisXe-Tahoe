// 批量对比已安装应用图标的输入像素和 CARenderer 输出。
// 编译：clang -fobjc-arc -Wall -Wextra xe-app-icons.m -framework AppKit -framework QuartzCore -framework Metal -o xe-app-icons
// 用法：xe-app-icons --gpu intel|amd --source resources|workspace --output 目录 [--l3-banks 4]
// 4-bank 对照仅改本进程 TGL hwInfo；不修改 IconServices、系统驱动或 EFI。
#import <AppKit/AppKit.h>
#import <QuartzCore/QuartzCore.h>
#import <Metal/Metal.h>
#import <objc/runtime.h>
#include <stdio.h>
#include <string.h>

enum { kColumns = 6, kRows = 4, kPageCount = kColumns * kRows };

// 枚举常用应用目录，遇到 .app 即停止向内部遍历，排除应用自身的 helper。
static NSArray<NSString *> *installedApps(void) {
    NSFileManager *fm = NSFileManager.defaultManager;
    NSMutableSet<NSString *> *paths = [NSMutableSet set];
    for (NSString *root in @[@"/Applications", @"/System/Applications", [NSHomeDirectory() stringByAppendingPathComponent:@"Applications"]]) {
        NSDirectoryEnumerator *entries = [fm enumeratorAtPath:root];
        for (NSString *relative in entries) {
            if ([relative.pathExtension.lowercaseString isEqualToString:@"app"]) {
                [paths addObject:[root stringByAppendingPathComponent:relative]];
                [entries skipDescendants];
            }
        }
    }
    [paths addObject:@"/System/Library/CoreServices/Finder.app"];
    return [paths.allObjects sortedArrayUsingSelector:@selector(compare:)];
}

// 按应用声明定位实际图标资源；没有声明时保留缺失状态，避免拿通用图标冒充。
static NSString *iconResource(NSString *app) {
    NSDictionary *info = [NSDictionary dictionaryWithContentsOfFile:[app stringByAppendingPathComponent:@"Contents/Info.plist"]];
    NSString *name = info[@"CFBundleIconFile"];
    if (![name isKindOfClass:NSString.class] || !name.length) return nil;
    NSString *resource = [[app stringByAppendingPathComponent:@"Contents/Resources"] stringByAppendingPathComponent:name];
    for (NSString *candidate in @[resource, [resource stringByAppendingPathExtension:@"icns"]]) {
        if ([NSFileManager.defaultManager fileExistsAtPath:candidate]) return candidate;
    }
    return nil;
}

// 选择实际运行 CARenderer 的 GPU，并可在所有 pipeline 创建前进行 4-bank 对照。
static id<MTLDevice> selectDevice(NSString *vendor, BOOL changeL3) {
    for (id<MTLDevice> device in MTLCopyAllDevices()) {
        if (![device.name localizedCaseInsensitiveContainsString:vendor]) continue;
        if (changeL3) {
            Ivar ivar = class_getInstanceVariable(object_getClass(device), "hwInfo");
            if (!ivar || ![device.name localizedCaseInsensitiveContainsString:@"Intel"]) return nil;
            unsigned char *caps = (unsigned char *)(__bridge void *)device + ivar_getOffset(ivar);
            uint32_t eu, slices, banks;
            memcpy(&eu, caps + 0x14, 4); memcpy(&slices, caps + 0x4c, 4); memcpy(&banks, caps + 0x54, 4);
            if (eu != 32 || slices != 1) return nil;
            uint32_t corrected = 4; memcpy(caps + 0x54, &corrected, 4);
            printf("process L3=%u -> 4\n", banks);
        }
        printf("GPU=%s registryID=%llu headless=%d\n",device.name.UTF8String,device.registryID,device.headless);
        return device;
    }
    return nil;
}

// 用 CPU 位图检查进入合成器前的 NSImage，区分空白输入与渲染失败。
static NSUInteger inputCoverage(NSImage *image, NSUInteger side) {
    NSBitmapImageRep *rep = [[NSBitmapImageRep alloc] initWithBitmapDataPlanes:NULL pixelsWide:side pixelsHigh:side
        bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES isPlanar:NO colorSpaceName:NSDeviceRGBColorSpace
        bytesPerRow:side * 4 bitsPerPixel:32];
    memset(rep.bitmapData, 0, side * side * 4);
    [NSGraphicsContext saveGraphicsState];
    NSGraphicsContext.currentContext = [NSGraphicsContext graphicsContextWithBitmapImageRep:rep];
    [image drawInRect:NSMakeRect(0,0,side,side) fromRect:NSZeroRect operation:NSCompositingOperationCopy fraction:1];
    [NSGraphicsContext restoreGraphicsState];
    NSUInteger covered = 0;
    for (NSUInteger i = 0; i < side * side; ++i) covered += rep.bitmapData[i * 4 + 3] > 16;
    return covered;
}

// 将原始 BGRA 像素转换为带 alpha 的 PNG，保留透明缺失区域供目视核对。
static BOOL savePNG(const uint8_t *bgra, NSUInteger side, NSString *path) {
    NSBitmapImageRep *rep = [[NSBitmapImageRep alloc] initWithBitmapDataPlanes:NULL pixelsWide:side pixelsHigh:side
        bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES isPlanar:NO colorSpaceName:NSDeviceRGBColorSpace
        bytesPerRow:side * 4 bitsPerPixel:32];
    for (NSUInteger i = 0; i < side * side; ++i) {
        rep.bitmapData[i*4] = bgra[i*4+2]; rep.bitmapData[i*4+1] = bgra[i*4+1];
        rep.bitmapData[i*4+2] = bgra[i*4]; rep.bitmapData[i*4+3] = bgra[i*4+3];
    }
    return [[rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:path atomically:YES];
}

// 每页最多 24 个图标，共享目标纹理；每项保留输入覆盖率、输出覆盖率与像素文件。
static BOOL renderPage(id<MTLDevice> device, NSArray<NSString *> *apps, NSUInteger base, NSUInteger side,
                       NSString *source, NSString *output, NSMutableArray *results) {
    NSUInteger width = kColumns * side, height = kRows * side;
    MTLTextureDescriptor *td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
        width:width height:height mipmapped:NO];
    td.usage = MTLTextureUsageRenderTarget | MTLTextureUsageShaderRead;
    td.storageMode = MTLStorageModeManaged;
    id<MTLTexture> texture = [device newTextureWithDescriptor:td];
    NSMutableData *pixels = [NSMutableData dataWithLength:width * height * 4];
    if (!texture) return NO;
    [texture replaceRegion:MTLRegionMake2D(0,0,width,height) mipmapLevel:0 withBytes:pixels.bytes bytesPerRow:width * 4];
    CGColorSpaceRef cs = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CARenderer *renderer = [CARenderer rendererWithMTLTexture:texture
        options:@{kCARendererColorSpace:(__bridge id)cs, @"kCARendererMetalDevice":device}];
    CGColorSpaceRelease(cs);
    CALayer *root = [CALayer layer]; root.frame = CGRectMake(0,0,width,height);
    root.backgroundColor = NSColor.clearColor.CGColor;
    NSMutableArray *rows = [NSMutableArray array];
    NSMutableArray *images = [NSMutableArray array];
    for (NSUInteger slot = 0; slot < kPageCount && base + slot < apps.count; ++slot) {
        NSString *app = apps[base+slot], *resource = iconResource(app);
        NSImage *image = [source isEqualToString:@"workspace"] ? [NSWorkspace.sharedWorkspace iconForFile:app] :
            (resource ? [[NSImage alloc] initWithContentsOfFile:resource] : nil);
        NSMutableDictionary *row = [@{@"index":@(base+slot), @"app":app, @"size":@(side),
            @"resource":resource ?: NSNull.null, @"input_alpha_pixels":@(0)} mutableCopy];
        if (image) {
            image.size = NSMakeSize(side,side);
            row[@"input_alpha_pixels"] = @(inputCoverage(image, side));
            CALayer *layer = [CALayer layer];
            layer.frame = CGRectMake((slot % kColumns) * side, height - (slot / kColumns + 1) * side, side, side);
            layer.contents = [image layerContentsForContentsScale:1.0];
            layer.contentsGravity = kCAGravityResizeAspect;
            [root addSublayer:layer];
            [images addObject:image];
        } else row[@"input_missing"] = @YES;
        [rows addObject:row];
    }
    renderer.layer = root; renderer.bounds = root.frame;
    [CATransaction flush];
    [renderer beginFrameAtTime:CACurrentMediaTime() timeStamp:NULL];
    [renderer addUpdateRect:renderer.bounds]; [renderer render]; [renderer endFrame];
    id<MTLCommandBuffer> command = [[device newCommandQueue] commandBuffer];
    id<MTLBlitCommandEncoder> blit = [command blitCommandEncoder];
    [blit synchronizeResource:texture]; [blit endEncoding];
    [command commit]; [command waitUntilCompleted];
    if (command.status != MTLCommandBufferStatusCompleted || command.error) return NO;
    [texture getBytes:pixels.mutableBytes bytesPerRow:width * 4 fromRegion:MTLRegionMake2D(0,0,width,height) mipmapLevel:0];
    const uint8_t *data = pixels.bytes;
    for (NSUInteger slot = 0; slot < rows.count; ++slot) {
        NSMutableData *icon = [NSMutableData dataWithLength:side * side * 4];
        uint8_t *bytes = icon.mutableBytes;
        NSUInteger x = (slot % kColumns) * side, y = height - (slot / kColumns + 1) * side, covered = 0;
        // CARenderer 的纹理行坐标与 layer.frame 使用相同的 y 原点。
        for (NSUInteger line = 0; line < side; ++line) memcpy(bytes + line * side * 4, data + ((y+line)*width+x)*4, side * 4);
        for (NSUInteger i = 0; i < side * side; ++i) covered += bytes[i*4+3] > 16;
        NSMutableDictionary *row = rows[slot];
        NSString *stem = [NSString stringWithFormat:@"%03lu-%lu",(unsigned long)(base+slot),(unsigned long)side];
        NSString *rawName = [stem stringByAppendingString:@".bgra"];
        NSString *pngName = [stem stringByAppendingString:@".png"];
        if (![icon writeToFile:[output stringByAppendingPathComponent:rawName] atomically:YES] ||
            !savePNG(bytes,side,[output stringByAppendingPathComponent:pngName])) return NO;
        row[@"output_alpha_pixels"] = @(covered);
        row[@"pixels"] = rawName; row[@"preview"] = pngName;
        [results addObject:row];
    }
    // JSON 每页落盘，发生进程失败也保留已完成样本。
    NSData *json = [NSJSONSerialization dataWithJSONObject:results options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys error:NULL];
    if (![json writeToFile:[output stringByAppendingPathComponent:@"results.json"] atomically:YES]) return NO;
    printf("size=%lu done=%lu/%lu\n",(unsigned long)side,(unsigned long)MIN(base+kPageCount,apps.count),(unsigned long)apps.count);
    fflush(stdout);
    return YES;
}

// 在独立输出目录中保存全量结果；基线渲染缺失由 JSON 比较统计，不伪装成进程错误。
int main(int argc, const char **argv) { @autoreleasepool {
    NSString *vendor = @"Intel", *source = @"resources", *output = nil;
    BOOL changeL3 = NO;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i],"--gpu") && i+1<argc) vendor = @(argv[++i]);
        else if (!strcmp(argv[i],"--source") && i+1<argc) source = @(argv[++i]);
        else if (!strcmp(argv[i],"--output") && i+1<argc) output = @(argv[++i]);
        else if (!strcmp(argv[i],"--l3-banks") && i+1<argc && !strcmp(argv[i+1],"4")) { changeL3=YES; ++i; }
        else { fprintf(stderr,"参数无效: %s\n",argv[i]); return 2; }
    }
    if (!output || (![source isEqualToString:@"resources"] && ![source isEqualToString:@"workspace"]) ||
        [NSFileManager.defaultManager fileExistsAtPath:output]) {
        fprintf(stderr,"要求 --output 全新目录、--source resources|workspace\n"); return 2;
    }
    NSError *error = nil;
    if (![NSFileManager.defaultManager createDirectoryAtPath:output withIntermediateDirectories:YES attributes:nil error:&error]) return 2;
    id<MTLDevice> device = selectDevice(vendor,changeL3);
    if (!device) { fprintf(stderr,"GPU 或 32 EU TGL 对照布局不匹配\n"); return 2; }
    NSArray<NSString *> *apps = installedApps();
    printf("apps=%lu source=%s\n",(unsigned long)apps.count,source.UTF8String);
    NSMutableArray *results = [NSMutableArray array];
    for (NSNumber *size in @[@32,@128,@512]) {
        for (NSUInteger base = 0; base < apps.count; base += kPageCount) { @autoreleasepool {
            if (!renderPage(device,apps,base,size.unsignedIntegerValue,source,output,results)) return 1;
        } }
    }
    NSUInteger empty = 0, missing = 0;
    for (NSDictionary *row in results) { empty += [row[@"output_alpha_pixels"] unsignedIntegerValue] == 0; missing += [row[@"input_missing"] boolValue]; }
    printf("samples=%lu empty=%lu missing-input=%lu\n",(unsigned long)results.count,(unsigned long)empty,(unsigned long)missing);
    return 0;
} }
