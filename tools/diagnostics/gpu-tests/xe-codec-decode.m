// 解码固定 MP4，输出完整 Y/UV 行与硬件状态；需再用 compare-codec-frames.py 验收像素。
// 编译：clang -fobjc-arc -Wall -Wextra -Werror -Wno-deprecated-declarations xe-codec-decode.m \
//   -framework Foundation -framework AVFoundation -framework CoreMedia -framework CoreVideo -framework VideoToolbox -framework Metal -o xe-codec-decode
// 用法：xe-codec-decode file.mp4 hardware|software output-prefix 8|10 [--local-gva]
// --local-gva 仅用于25E253的候选VA回归，避免XPC服务加载系统旧包。
#import <AVFoundation/AVFoundation.h>
#import <VideoToolbox/VideoToolbox.h>
#include <stdio.h>
#include <dlfcn.h>
#include <sys/sysctl.h>
#import <Metal/Metal.h>

// 从实际系统导出表读取注册键，原有 XPC / 软件解码用法不调用此入口。
static NSString *decoderKey(void *handle,const char *symbol){
    CFStringRef *key=dlsym(handle,symbol);
    return key?(__bridge NSString*)*key:nil;
}

// 将系统 GVA decoder 仅注册到测试进程，确保候选 VA 的真实硬件解码可以独立验收。
static NSString *registerLocalDecoder(CMVideoCodecType codec){
    char build[64];size_t size=sizeof(build);
    if(sysctlbyname("kern.osversion",build,&size,NULL,0)||strcmp(build,"25E253"))return nil;
    void *vt=dlopen("/System/Library/Frameworks/VideoToolbox.framework/VideoToolbox",RTLD_NOW);Dl_info info;
    void *reg=dlsym(vt,"VTRegisterVideoDecoderWithInfo");if(!reg||!dladdr(reg,&info))return nil;
    void *factory=NULL;
    if(codec==kCMVideoCodecType_H264){
        factory=(char*)info.dli_fbase+0x4187e8;
        const unsigned char expected[]={0x55,0x48,0x89,0xe5,0x41,0x57,0x41,0x56,0x41,0x54,0x53};
        if(memcmp(factory,expected,sizeof expected))return nil;
    }else if(codec==kCMVideoCodecType_HEVC){
        // HEVC 使用系统 plug-in 导出的 factory，不能借用 H.264 decoder。
        void *hevc=dlopen("/System/Library/Video/Plug-Ins/AppleGVAHEVCDecoder.bundle/Contents/MacOS/AppleGVAHEVCDecoder",RTLD_NOW);
        if(hevc)factory=dlsym(hevc,"GVACODEC_VTDecoder_CreateInstance");
    }
    if(!factory)return nil;
    id<MTLDevice> intel=nil;
    for(id<MTLDevice> device in MTLCopyAllDevices())if([device.name localizedCaseInsensitiveContainsString:@"Intel"])intel=device;
    if(!intel)return nil;
    NSMutableDictionary *attributes=[@{@"CMClassImplementationID":@"local.test.intel.decoder"} mutableCopy];
    const char *symbols[]={"kVTVideoDecoder_IsHardwareAccelerated","kVTVideoDecoder_DecoderMetalRegistryID","kVTVideoDecoder_AllowSandboxedDecode"};
    NSArray *values=@[@YES,@(intel.registryID),@NO];
    for(NSUInteger i=0;i<values.count;i++){
        NSString *key=decoderKey(vt,symbols[i]);if(!key)return nil;attributes[key]=values[i];
    }
    typedef OSStatus(*Register)(CMVideoCodecType,CFDictionaryRef,void*);
    OSStatus status=((Register)reg)(codec,(__bridge CFDictionaryRef)attributes,factory);
    printf("local_decoder_register=%d\n",(int)status);if(status)return nil;
    // VTRegisterVideoDecoderWithInfo 会把 fourcc 后缀加入注册 ID。
    return [NSString stringWithFormat:@"local.test.intel.decoder.%c%c%c%c",(int)(codec>>24)&255,(int)(codec>>16)&255,(int)(codec>>8)&255,(int)codec&255];
}

// 保存每帧输出和 PTS，解码顺序与显示顺序分别处理。
@interface DecodeCapture : NSObject
@property NSMutableArray<NSDictionary *> *frames;
@property unsigned errors;
@property OSStatus lastError;
@end
@implementation DecodeCapture
@end

// 复制有效图像行，排除 pitch padding；保留完整 Y/UV 像素供软件解码对照。
static NSData *copyFrame(CVPixelBufferRef image) {
    OSType format=CVPixelBufferGetPixelFormatType(image);
    size_t bytes=(format==kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange)?2:1;
    if(format!=kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange && format!=kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange)return nil;
    if(CVPixelBufferGetPlaneCount(image)!=2)return nil;
    if(CVPixelBufferLockBaseAddress(image,kCVPixelBufferLock_ReadOnly))return nil;
    NSMutableData *data=[NSMutableData data];
    for(size_t plane=0;plane<2;++plane){
        uint8_t *base=CVPixelBufferGetBaseAddressOfPlane(image,plane);
        size_t pitch=CVPixelBufferGetBytesPerRowOfPlane(image,plane);
        size_t width=CVPixelBufferGetWidth(image)*bytes,height=CVPixelBufferGetHeightOfPlane(image,plane);
        if(!base || pitch<width){CVPixelBufferUnlockBaseAddress(image,kCVPixelBufferLock_ReadOnly);return nil;}
        for(size_t y=0;y<height;++y)[data appendBytes:base+y*pitch length:width];
    }
    CVPixelBufferUnlockBaseAddress(image,kCVPixelBufferLock_ReadOnly);
    return data;
}

// 接收真实 VideoToolbox 回调，错误帧与像素缺失都计为失败。
static void decoded(void *context, void *frameContext, OSStatus status, VTDecodeInfoFlags flags,
                    CVImageBufferRef image, CMTime pts, CMTime duration) {
    (void)frameContext;(void)flags;(void)duration;
    DecodeCapture *capture=(__bridge DecodeCapture *)context;
    @synchronized(capture){
        if(status || !image){capture.errors++;capture.lastError=status;return;}
        NSData *pixels=copyFrame(image);
        if(!pixels){capture.errors++;capture.lastError=-1;return;}
        [capture.frames addObject:@{@"pts":@(CMTimeGetSeconds(pts)),@"pixels":pixels,
            @"width":@(CVPixelBufferGetWidth(image)),@"height":@(CVPixelBufferGetHeight(image)),
            @"format":@(CVPixelBufferGetPixelFormatType(image))}];
    }
}

// AVAssetReader 会返回没有压缩数据的边界标记；只提交真实视频包。
static CMSampleBufferRef nextPacket(AVAssetReaderTrackOutput *output, unsigned *markers) {
    CMSampleBufferRef sample;
    while((sample=[output copyNextSampleBuffer])) {
        if(CMSampleBufferGetNumSamples(sample)>0 && CMSampleBufferGetDataBuffer(sample)) return sample;
        ++*markers;CFRelease(sample);
    }
    return NULL;
}

// 对一个固定文件执行硬件强制或软件强制解码，不安装插件或改动系统配置。
int main(int argc,const char **argv){@autoreleasepool{
    setvbuf(stdout,NULL,_IONBF,0);
    if(argc<5||argc>6){fprintf(stderr,"usage: %s file.mp4 hardware|software output-prefix 8|10 [--local-gva]\n",argv[0]);return 2;}
    BOOL local=argc==6;if(local&&strcmp(argv[5],"--local-gva"))return 2;
    if((strcmp(argv[2],"hardware") && strcmp(argv[2],"software")) ||
       (strcmp(argv[4],"8") && strcmp(argv[4],"10")))return 2;
    BOOL hardware=!strcmp(argv[2],"hardware"),tenBit=!strcmp(argv[4],"10");
    NSString *prefix=@(argv[3]),*raw=[prefix stringByAppendingString:@".yuv"],*report=[prefix stringByAppendingString:@".json"];
    if([NSFileManager.defaultManager fileExistsAtPath:raw] || [NSFileManager.defaultManager fileExistsAtPath:report])return 2;
    AVURLAsset *asset=[AVURLAsset URLAssetWithURL:[NSURL fileURLWithPath:@(argv[1])] options:nil];
    AVAssetTrack *track=[[asset tracksWithMediaType:AVMediaTypeVideo] firstObject];
    NSError *error=nil;
    AVAssetReader *reader=[[AVAssetReader alloc] initWithAsset:asset error:&error];
    if(!reader || !track){fprintf(stderr,"reader/track: %s\n",error.description.UTF8String);return 2;}
    AVAssetReaderTrackOutput *output=[[AVAssetReaderTrackOutput alloc] initWithTrack:track outputSettings:nil];
    output.alwaysCopiesSampleData=NO;
    [reader addOutput:output];
    if(![reader startReading]){fprintf(stderr,"start reader: %s\n",reader.error.description.UTF8String);return 2;}
    unsigned markers=0;
    CMSampleBufferRef sample=nextPacket(output,&markers);
    if(!sample)return 2;
    CMFormatDescriptionRef inputFormat=CMSampleBufferGetFormatDescription(sample);
    if(!inputFormat) inputFormat=(__bridge CMFormatDescriptionRef)track.formatDescriptions.firstObject;
    printf("input media=%08x subtype=%08x\n",(unsigned)CMFormatDescriptionGetMediaType(inputFormat),(unsigned)CMFormatDescriptionGetMediaSubType(inputFormat));
    DecodeCapture *capture=[DecodeCapture new];capture.frames=[NSMutableArray array];
    VTDecompressionOutputCallbackRecord callback={decoded,(__bridge void *)capture};
    NSDictionary *spec=hardware?@{(__bridge id)kVTVideoDecoderSpecification_RequireHardwareAcceleratedVideoDecoder:@YES}:
        @{(__bridge id)kVTVideoDecoderSpecification_EnableHardwareAcceleratedVideoDecoder:@NO};
    if(local){
        if(!hardware)return 2;
        NSString *id=registerLocalDecoder(CMFormatDescriptionGetMediaSubType(inputFormat));if(!id)return 2;
        void *vt=dlopen("/System/Library/Frameworks/VideoToolbox.framework/VideoToolbox",RTLD_NOW);
        NSString *key=decoderKey(vt,"kVTVideoDecoderSpecification_DecoderID");if(!key)return 2;
        NSMutableDictionary *localSpec=[spec mutableCopy];localSpec[key]=id;spec=localSpec;
        printf("selected_decoder=%s\n",id.UTF8String);
    }
    NSDictionary *attributes=@{(__bridge id)kCVPixelBufferPixelFormatTypeKey:@(tenBit?kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange:kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange),(__bridge id)kCVPixelBufferIOSurfacePropertiesKey:@{}};
    VTDecompressionSessionRef session=NULL;
    OSStatus status=VTDecompressionSessionCreate(NULL,inputFormat,(__bridge CFDictionaryRef)spec,(__bridge CFDictionaryRef)attributes,&callback,&session);
    printf("session=%d requested=%s\n",(int)status,hardware?"hardware":"software");
    if(status){CFRelease(sample);return 1;}
    CFTypeRef hw=NULL,gpu=NULL;
    OSStatus hwStatus=VTSessionCopyProperty(session,kVTDecompressionPropertyKey_UsingHardwareAcceleratedVideoDecoder,NULL,&hw);
    OSStatus gpuStatus=VTSessionCopyProperty(session,kVTDecompressionPropertyKey_UsingGPURegistryID,NULL,&gpu);
    BOOL usingHardware=hw==kCFBooleanTrue;
    printf("hardware=%d propertyStatus=%d GPU=%s gpuStatus=%d\n",usingHardware,(int)hwStatus,gpu?[(__bridge id)gpu description].UTF8String:"built-in-or-unreported",(int)gpuStatus);
    unsigned submitted=0,submitErrors=0;
    CFAbsoluteTime start=CFAbsoluteTimeGetCurrent();
    do{
        status=VTDecompressionSessionDecodeFrame(session,sample,kVTDecodeFrame_EnableAsynchronousDecompression,NULL,NULL);
        if(status){submitErrors++;printf("submit-error=%d\n",(int)status);}
        submitted++;CFRelease(sample);
        sample=nextPacket(output,&markers);
    }while(sample);
    VTDecompressionSessionFinishDelayedFrames(session);
    OSStatus wait=VTDecompressionSessionWaitForAsynchronousFrames(session);
    double elapsed=CFAbsoluteTimeGetCurrent()-start;
    NSArray *frames=[capture.frames sortedArrayUsingComparator:^NSComparisonResult(NSDictionary *a,NSDictionary *b){return [a[@"pts"] compare:b[@"pts"]];}];
    FILE *file=fopen(raw.fileSystemRepresentation,"wb");
    if(!file)return 2;
    NSMutableArray *metadata=[NSMutableArray array];
    BOOL writesOK=YES;
    for(NSDictionary *frame in frames){
        NSData *bytes=frame[@"pixels"];
        writesOK&=fwrite(bytes.bytes,1,bytes.length,file)==bytes.length;
        NSMutableDictionary *row=[frame mutableCopy];[row removeObjectForKey:@"pixels"];row[@"bytes"]=@(bytes.length);
        [metadata addObject:row];
    }
    writesOK&=fclose(file)==0;
    BOOL pass=usingHardware==hardware && frames.count==submitted && submitted>0 && !submitErrors && !capture.errors && !wait && writesOK && reader.status==AVAssetReaderStatusCompleted;
    NSDictionary *result=@{@"source":@(argv[1]),@"local_gva":@(local),@"requested_hardware":@(hardware),@"using_hardware":@(usingHardware),
        @"gpu_registry_id":gpu?(__bridge id)gpu:NSNull.null,@"submitted":@(submitted),@"frames":@(frames.count),@"markers_skipped":@(markers),
        @"submit_errors":@(submitErrors),@"callback_errors":@(capture.errors),@"last_error":@(capture.lastError),
        @"wait_status":@(wait),@"seconds":@(elapsed),@"transfer_ok":@(pass),@"frame_metadata":metadata};
    [[NSJSONSerialization dataWithJSONObject:result options:NSJSONWritingPrettyPrinted|NSJSONWritingSortedKeys error:NULL] writeToFile:report atomically:YES];
    printf("submitted=%u frames=%lu submitErrors=%u callbackErrors=%u wait=%d seconds=%.3f transfer=%s pixels_need_comparison\n",submitted,(unsigned long)frames.count,submitErrors,capture.errors,(int)wait,elapsed,pass?"PASS":"FAIL");
    VTDecompressionSessionInvalidate(session);CFRelease(session);if(hw)CFRelease(hw);if(gpu)CFRelease(gpu);
    return pass?0:1;
}}
