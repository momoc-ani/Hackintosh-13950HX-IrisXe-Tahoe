// 对两个 NSWorkspace 差异样本，只生成一次 layer contents，交给 Intel 和 AMD 分别渲染。
// 用法：xe-icon-shared-input workspace-results.json 全新输出目录；不启动应用、不写系统或驱动。
#import <AppKit/AppKit.h>
#import <QuartzCore/QuartzCore.h>
#import <Metal/Metal.h>
#include <stdio.h>
#include <string.h>

enum { kSide=512,kColumns=6,kRows=4 };

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

// 读取原批量结果以保留槽位；每轮共享一份输入对象，交替 GPU 顺序复查差异。
int main(int argc,const char **argv){@autoreleasepool{
    if(argc!=3){fprintf(stderr,"usage: %s workspace-results.json output-directory\n",argv[0]);return 2;}
    NSString *output=@(argv[2]);if([NSFileManager.defaultManager fileExistsAtPath:output])return 2;
    if(![NSFileManager.defaultManager createDirectoryAtPath:output withIntermediateDirectories:YES attributes:nil error:NULL])return 2;
    NSArray *old=[NSJSONSerialization JSONObjectWithData:[NSData dataWithContentsOfFile:@(argv[1])] options:0 error:NULL];
    id<MTLDevice> intel=deviceFor(@"intel"),amd=deviceFor(@"amd");if(!intel||!amd)return 2;
    printf("Intel registryID=%llu headless=%d; AMD registryID=%llu headless=%d\n",intel.registryID,intel.headless,amd.registryID,amd.headless);
    NSMutableArray *records=[NSMutableArray array];NSUInteger failures=0;
    for(NSDictionary *source in old){
        NSString *app=source[@"app"];
        if([source[@"size"] unsignedIntegerValue]!=512||(![app isEqualToString:@"/Applications/Xcode.app"]&&![app isEqualToString:@"/System/Applications/Journal.app"]))continue;
        for(NSUInteger iteration=0;iteration<3;iteration++){@autoreleasepool{
            NSImage *image=[NSWorkspace.sharedWorkspace iconForFile:app];image.size=NSMakeSize(kSide,kSide);
            id contents=[image layerContentsForContentsScale:1.0];if(!contents)return 2;
            NSUInteger slot=[source[@"index"] unsignedIntegerValue]%24;
            NSData *i=nil,*a=nil;
            if(iteration%2){i=render(intel,contents,slot);a=render(amd,contents,slot);}else{a=render(amd,contents,slot);i=render(intel,contents,slot);}
            if(!i||!a||i.length!=a.length)return 2;
            NSMutableDictionary *record=[compare(i,a) mutableCopy];record[@"app"]=app;record[@"iteration"]=@(iteration);record[@"slot"]=@(slot);record[@"contents_class"]=NSStringFromClass([contents class]);
            NSString *stem=[NSString stringWithFormat:@"%@-%lu",app.lastPathComponent,(unsigned long)iteration];
            if(![i writeToFile:[output stringByAppendingPathComponent:[stem stringByAppendingString:@"-intel.bgra"]] atomically:YES]||![a writeToFile:[output stringByAppendingPathComponent:[stem stringByAppendingString:@"-amd.bgra"]] atomically:YES])return 2;
            [records addObject:record];failures+=![record[@"pass"] boolValue];printf("%s iteration=%lu max=%lu channels-above-one=%lu %s\n",app.UTF8String,(unsigned long)iteration,[record[@"max_channel_delta"] unsignedLongValue],[record[@"channels_above_one"] unsignedLongValue],[record[@"pass"] boolValue]?"PASS":"FAIL");
        }}
    }
    NSData *json=[NSJSONSerialization dataWithJSONObject:records options:NSJSONWritingPrettyPrinted|NSJSONWritingSortedKeys error:NULL];if(![json writeToFile:[output stringByAppendingPathComponent:@"results.json"] atomically:YES])return 2;
    printf("tests=%lu failures=%lu\n",(unsigned long)records.count,(unsigned long)failures);
    return failures?1:0;
}}
