// 对系统 KEXT 图标和指定 kext 的 NSWorkspace 图标，复用 layer contents 比较 Intel 与 AMD。
// 用法：xe-kext-icons 输出目录 kext路径；不启动应用、不写系统或驱动。
// 编译：clang -fobjc-arc -Wall -Wextra -Werror xe-kext-icons.m \
//   -framework AppKit -framework QuartzCore -framework Metal -o xe-kext-icons
#import <AppKit/AppKit.h>
#import <QuartzCore/QuartzCore.h>
#import <Metal/Metal.h>
#include <stdio.h>
#include <string.h>

static NSUInteger kSide=512;
enum { kColumns=6,kRows=4 };

// 显式按厂商选择 Metal 设备，保证两组结果来自不同 GPU。
static id<MTLDevice> deviceFor(NSString *vendor) {
    for(id<MTLDevice> d in MTLCopyAllDevices()) if([d.name localizedCaseInsensitiveContainsString:vendor])return d;
    return nil;
}

// 在与批量探针同样的页面尺寸和图块位置渲染同一个 layer contents 对象。
static NSData *render(id<MTLDevice> d,id contents,NSUInteger slot) {
    NSUInteger width=kSide*kColumns,height=kSide*kRows;
    MTLTextureDescriptor *td=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:width height:height mipmapped:NO];
    td.usage=MTLTextureUsageRenderTarget|MTLTextureUsageShaderRead;td.storageMode=MTLStorageModeManaged;
    id<MTLTexture> texture=[d newTextureWithDescriptor:td];
    if(!texture)return nil;
    NSMutableData *page=[NSMutableData dataWithLength:width*height*4];
    [texture replaceRegion:MTLRegionMake2D(0,0,width,height) mipmapLevel:0 withBytes:page.bytes bytesPerRow:width*4];
    CGColorSpaceRef space=CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CARenderer *renderer=[CARenderer rendererWithMTLTexture:texture options:@{kCARendererColorSpace:(__bridge id)space,@"kCARendererMetalDevice":d}];
    CGColorSpaceRelease(space);
    CALayer *root=[CALayer layer];root.frame=CGRectMake(0,0,width,height);root.backgroundColor=NSColor.clearColor.CGColor;
    NSUInteger x=(slot%kColumns)*kSide,y=height-(slot/kColumns+1)*kSide;
    CALayer *icon=[CALayer layer];icon.frame=CGRectMake(x,y,kSide,kSide);icon.contents=contents;icon.contentsGravity=kCAGravityResizeAspect;[root addSublayer:icon];
    renderer.layer=root;renderer.bounds=root.frame;[CATransaction flush];
    [renderer beginFrameAtTime:CACurrentMediaTime() timeStamp:NULL];[renderer addUpdateRect:renderer.bounds];[renderer render];[renderer endFrame];
    id<MTLCommandBuffer> cb=[[d newCommandQueue] commandBuffer];id<MTLBlitCommandEncoder> blit=[cb blitCommandEncoder];[blit synchronizeResource:texture];[blit endEncoding];[cb commit];[cb waitUntilCompleted];
    if(cb.status!=MTLCommandBufferStatusCompleted||cb.error)return nil;
    [texture getBytes:page.mutableBytes bytesPerRow:width*4 fromRegion:MTLRegionMake2D(0,0,width,height) mipmapLevel:0];
    NSMutableData *pixels=[NSMutableData dataWithLength:kSide*kSide*4];
    for(NSUInteger row=0;row<kSide;row++)memcpy((uint8_t *)pixels.mutableBytes+row*kSide*4,(const uint8_t *)page.bytes+((y+row)*width+x)*4,kSide*4);
    return pixels;
}

// 比较全部 BGRA 通道，输出差异数、最大差、非透明覆盖率，避免只看是否非空。
static NSDictionary *compare(NSData *intel,NSData *amd) {
    const uint8_t *a=intel.bytes,*b=amd.bytes;NSUInteger max=0,diffs=0,ia=0,aa=0;
    for(NSUInteger i=0;i<intel.length;i++){NSUInteger delta=(NSUInteger)abs((int)a[i]-(int)b[i]);max=MAX(max,delta);diffs+=delta>1;}
    for(NSUInteger i=3;i<intel.length;i+=4){ia+=a[i]>16;aa+=b[i]>16;}
    return @{@"max_channel_delta":@(max),@"channels_above_one":@(diffs),@"intel_alpha":@(ia),@"amd_alpha":@(aa),@"pass":@(max<=1&&ia&&aa)};
}


// 对比系统 KEXT.icns 与实际 AMFIPass.kext 类型图标在两颗 GPU 的像素输出。
int main(int argc,const char **argv){@autoreleasepool{
    if(argc!=3){fprintf(stderr,"usage: %s output-directory kext-path\n",argv[0]);return 2;}
    NSString *output=@(argv[1]);if(![NSFileManager.defaultManager createDirectoryAtPath:output withIntermediateDirectories:YES attributes:nil error:NULL])return 2;
    id<MTLDevice> intel=deviceFor(@"intel"),amd=deviceFor(@"amd");if(!intel||!amd)return 2;
    NSString *resource=@"/System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/KEXT.icns";
    NSString *actual=@(argv[2]);
    if(![NSFileManager.defaultManager fileExistsAtPath:actual])return 2;
    NSMutableArray *records=[NSMutableArray array];NSUInteger failures=0;
    for(NSString *source in @[@"system-resource",@"workspace-kext"]){
      for(NSNumber *size in @[@16,@32,@128,@512]){@autoreleasepool{
        kSide=size.unsignedIntegerValue;
        NSImage *image=[source isEqualToString:@"system-resource"]?[[NSImage alloc] initWithContentsOfFile:resource]:[NSWorkspace.sharedWorkspace iconForFile:actual];
        image.size=NSMakeSize(kSide,kSide);id contents=[image layerContentsForContentsScale:1.0];if(!contents)return 2;
        NSData *i=render(intel,contents,0),*a=render(amd,contents,0);if(!i||!a)return 2;
        NSMutableDictionary *record=[compare(i,a) mutableCopy];record[@"source"]=source;record[@"size"]=size;record[@"file"]=[source isEqualToString:@"system-resource"]?resource:actual;
        NSString *stem=[NSString stringWithFormat:@"%@-%lu",source,(unsigned long)kSide];
        [i writeToFile:[output stringByAppendingPathComponent:[stem stringByAppendingString:@"-intel.bgra"]] atomically:YES];[a writeToFile:[output stringByAppendingPathComponent:[stem stringByAppendingString:@"-amd.bgra"]] atomically:YES];
        [records addObject:record];failures+=![record[@"pass"] boolValue];
        printf("%s %lu max=%lu %s\n",source.UTF8String,(unsigned long)kSide,[record[@"max_channel_delta"] unsignedLongValue],[record[@"pass"] boolValue]?"PASS":"FAIL");
      }}
    }
    NSData *json=[NSJSONSerialization dataWithJSONObject:records options:NSJSONWritingPrettyPrinted|NSJSONWritingSortedKeys error:NULL];[json writeToFile:[output stringByAppendingPathComponent:@"results.json"] atomically:YES];
    printf("tests=%lu failures=%lu\n",(unsigned long)records.count,(unsigned long)failures);return failures?1:0;
}}
