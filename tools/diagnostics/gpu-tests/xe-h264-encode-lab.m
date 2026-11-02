// 诊断专用：仅在本进程注册当前系统 GVA factory，验证 Intel H.264 全 I 帧编码。
// 此探针不是系统编码能力发布开关，也不测试存在挂起风险的帧间预测路径。
// 编译：clang -fobjc-arc -Wall -Wextra -Werror xe-h264-encode-lab.m \
//   -framework Foundation -framework Metal -framework VideoToolbox -framework CoreMedia -framework CoreVideo -o xe-h264-encode-lab
#import <Foundation/Foundation.h>
#import <VideoToolbox/VideoToolbox.h>
#import <Metal/Metal.h>
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <sys/sysctl.h>

// 从系统 VideoToolbox 导出表获取私有键名，避免假定字面字符串。
static NSString *key(void *handle,const char *symbol){CFStringRef *p=dlsym(handle,symbol);return p?(__bridge NSString *)*p:nil;}


// 收集真实压缩回调，保留样本、丢帧和错误，避免只验证会话创建。
@interface EncodeCapture : NSObject
@property NSMutableArray *samples;
@property unsigned errors;
@property unsigned dropped;
@end
@implementation EncodeCapture
@end

// 保存每次编码回调提供的样本；数组强引用保留 CoreMedia 对象供后续写码流。
static void encoded(void *context,void *source,OSStatus status,VTEncodeInfoFlags flags,CMSampleBufferRef sample){
  (void)source;EncodeCapture *capture=(__bridge EncodeCapture *)context;
  @synchronized(capture){
    if(status||!sample){capture.errors++;fprintf(stderr,"callback error=%d flags=%u\n",(int)status,(unsigned)flags);return;}
    if(flags&kVTEncodeInfo_FrameDropped)capture.dropped++;
    [capture.samples addObject:(__bridge id)sample];
  }
}

// 创建带 IOSurface 的已知 NV12 图像，并把有效行保存为无 padding 的软件参考。
static CVPixelBufferRef frame(NSUInteger width,NSUInteger height,NSUInteger index,NSMutableData *reference,CVPixelBufferPoolRef pool){
  CVPixelBufferRef pixel=NULL;
  NSDictionary *attrs=@{(__bridge id)kCVPixelBufferIOSurfacePropertiesKey:@{}};
  if(pool){if(CVPixelBufferPoolCreatePixelBuffer(NULL,pool,&pixel))return NULL;}
  else if(CVPixelBufferCreate(NULL,width,height,kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,(__bridge CFDictionaryRef)attrs,&pixel))return NULL;
  if(CVPixelBufferLockBaseAddress(pixel,0)){CFRelease(pixel);return NULL;}
  for(NSUInteger plane=0;plane<2;plane++){
    uint8_t *base=CVPixelBufferGetBaseAddressOfPlane(pixel,plane);size_t pitch=CVPixelBufferGetBytesPerRowOfPlane(pixel,plane),rows=plane?height/2:height;
    for(NSUInteger y=0;y<rows;y++){
      for(NSUInteger x=0;x<width;x++)base[y*pitch+x]=plane?(uint8_t)(x%2?100+(index*3)%40:125+(y/32)%30):(uint8_t)(32+((x/24+y/24+index)%12)*15);
      [reference appendBytes:base+y*pitch length:width];
    }
  }
  CVPixelBufferUnlockBaseAddress(pixel,0);return pixel;
}

// 将 AVCC 样本写成 Annex-B，保留 SPS/PPS、帧大小和 PTS，供独立软件解码验收。
static NSArray *writeSamples(NSArray *samples,NSString *path){
  NSMutableData *bytes=[NSMutableData data];NSMutableArray *rows=[NSMutableArray array];const uint8_t marker[]={0,0,0,1};
  for(id object in samples){
    CMSampleBufferRef sample=(__bridge CMSampleBufferRef)object;CMFormatDescriptionRef format=CMSampleBufferGetFormatDescription(sample);
    const uint8_t *parameter=NULL;size_t length=0,count=0;int nalLength=0;
    if(CMVideoFormatDescriptionGetH264ParameterSetAtIndex(format,0,&parameter,&length,&count,&nalLength))return nil;
    if(!rows.count){for(size_t i=0;i<count;i++){if(CMVideoFormatDescriptionGetH264ParameterSetAtIndex(format,i,&parameter,&length,NULL,NULL))return nil;[bytes appendBytes:marker length:4];[bytes appendBytes:parameter length:length];}}
    if(nalLength!=4)return nil;
    CMBlockBufferRef data=CMSampleBufferGetDataBuffer(sample);size_t size=CMBlockBufferGetDataLength(data);NSMutableData *avcc=[NSMutableData dataWithLength:size];
    if(CMBlockBufferCopyDataBytes(data,0,size,avcc.mutableBytes))return nil;
    const uint8_t *input=avcc.bytes;size_t at=0;
    while(at<size){if(at+4>size)return nil;uint32_t n=((uint32_t)input[at]<<24)|((uint32_t)input[at+1]<<16)|((uint32_t)input[at+2]<<8)|input[at+3];at+=4;if(!n||n>size-at)return nil;[bytes appendBytes:marker length:4];[bytes appendBytes:input+at length:n];at+=n;}
    CMTime pts=CMSampleBufferGetPresentationTimeStamp(sample);CFArrayRef attachments=CMSampleBufferGetSampleAttachmentsArray(sample,false);BOOL keyframe=YES;
    if(attachments&&CFArrayGetCount(attachments)){NSDictionary *a=(__bridge NSDictionary *)CFArrayGetValueAtIndex(attachments,0);keyframe=![a[(__bridge id)kCMSampleAttachmentKey_NotSync] boolValue];}
    [rows addObject:@{@"bytes":@(size),@"pts_value":@(pts.value),@"pts_timescale":@(pts.timescale),@"keyframe":@(keyframe)}];
  }
  return [bytes writeToFile:path atomically:YES]?rows:nil;
}
// 用已经核对的当前系统 factory 在独立进程创建会话；退出即撤销注册。
int main(int argc,const char **argv){@autoreleasepool{
  if(argc<5){fprintf(stderr,"usage: %s output-prefix width height frames [--encoder-pool] [--software-transfer]\n",argv[0]);return 2;}
  BOOL usePool=NO,softwareTransfer=NO;
  for(int i=5;i<argc;i++){if(!strcmp(argv[i],"--encoder-pool"))usePool=YES;else if(!strcmp(argv[i],"--software-transfer"))softwareTransfer=YES;else return 2;}
  NSString *prefix=@(argv[1]);NSUInteger width=strtoul(argv[2],NULL,10),height=strtoul(argv[3],NULL,10),count=strtoul(argv[4],NULL,10);
  if(!width||!height||width%2||height%2||!count||count>120)return 2;
  setvbuf(stdout,NULL,_IOLBF,0);
  // factory 偏移已针对当前系统核对；其他系统构建必须重新定位。
  char osBuild[64];size_t buildSize=sizeof(osBuild);
  if(sysctlbyname("kern.osversion",osBuild,&buildSize,NULL,0)||strcmp(osBuild,"25E253")){fprintf(stderr,"该进程内 factory 仅针对 macOS 25E253 验证\n");return 2;}
  void *vt=dlopen("/System/Library/Frameworks/VideoToolbox.framework/VideoToolbox",RTLD_NOW);Dl_info info;
  if(!vt||!dladdr(dlsym(vt,"VTRegisterVideoEncoderWithInfo"),&info))return 2;
  unsigned char *factory=(unsigned char *)info.dli_fbase+0x46a370;
  const unsigned char expected[]={0x55,0x48,0x89,0xe5,0x41,0x57,0x41,0x56,0x41,0x54,0x53};
  if(memcmp(factory,expected,sizeof expected)){fprintf(stderr,"factory 布局未匹配\n");return 2;}
  id<MTLDevice> gpu=nil;for(id<MTLDevice> d in MTLCopyAllDevices())if([d.name localizedCaseInsensitiveContainsString:@"Intel"])gpu=d;
  if(!gpu)return 2;
  NSMutableDictionary *registration=[NSMutableDictionary dictionary];
  NSString *idKey=@"CMClassImplementationID";
  printf("registration_id_key=%s\n",idKey.UTF8String);
  registration[idKey]=@"local.test.intel.h264";
  const char *names[]={"kVTVideoEncoder_EncoderNameKey","kVTVideoEncoder_CodecNameKey","kVTVideoEncoder_IsHardwareAccelerated","kVTVideoEncoder_EncoderMetalRegistryID","kVTVideoEncoder_AllowSandboxedEncode"};
  NSArray *values=@[@"GT1 process-local H264",@"H.264",@YES,@(gpu.registryID),@NO];
  for(NSUInteger i=0;i<5;i++){NSString *k=key(vt,names[i]);if(!k)return 2;registration[k]=values[i];printf("key %s=%s\n",names[i],k.UTF8String);}
  typedef OSStatus (*Register)(CMVideoCodecType,CFDictionaryRef,void *);
  Register reg=(Register)dlsym(vt,"VTRegisterVideoEncoderWithInfo");
  OSStatus st=reg(kCMVideoCodecType_H264,(__bridge CFDictionaryRef)registration,factory);printf("register=%d ID=%llu\n",(int)st,gpu.registryID);if(st)return 1;
  CFArrayRef all=NULL;VTCopyVideoEncoderList(NULL,&all);for(NSDictionary *entry in (__bridge NSArray *)all)if([[entry description] containsString:@"local.test"])printf("registered=%s\n",entry.description.UTF8String);if(all)CFRelease(all);
  NSMutableDictionary *spec=[@{(__bridge id)kVTVideoEncoderSpecification_EncoderID:@"local.test.intel.h264",(__bridge id)kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder:@YES} mutableCopy];
  spec[key(vt,"kVTVideoEncoderSpecification_Usage")]=@1;
  EncodeCapture *capture=[EncodeCapture new];capture.samples=[NSMutableArray array];
  VTCompressionSessionRef s=NULL;st=VTCompressionSessionCreate(NULL,(int32_t)width,(int32_t)height,kCMVideoCodecType_H264,(__bridge CFDictionaryRef)spec,NULL,NULL,encoded,(__bridge void *)capture,&s);printf("create=%d\n",(int)st);if(st)return 1;

  NSMutableDictionary *report=[NSMutableDictionary dictionary];
  for(id property in @[(__bridge id)kVTCompressionPropertyKey_UsingHardwareAcceleratedVideoEncoder,(__bridge id)kVTCompressionPropertyKey_EncoderID,(__bridge id)kVTCompressionPropertyKey_UsingGPURegistryID]){
    CFTypeRef value=NULL;OSStatus result=VTSessionCopyProperty(s,(__bridge CFStringRef)property,NULL,&value);
    printf("%s status=%d value=%s\n",[property description].UTF8String,(int)result,value?[(__bridge id)value description].UTF8String:"null");if(value){report[[property description]]=(__bridge id)value;CFRelease(value);}
  }
  NSMutableDictionary *settings=[@{(__bridge id)kVTCompressionPropertyKey_RealTime:@YES,(__bridge id)kVTCompressionPropertyKey_AllowFrameReordering:@NO,(__bridge id)kVTCompressionPropertyKey_MaxKeyFrameInterval:@(1),(__bridge id)kVTCompressionPropertyKey_AverageBitRate:@8000000,(__bridge id)kVTCompressionPropertyKey_ExpectedFrameRate:@30} mutableCopy];
  if(softwareTransfer){NSString *hardware=key(vt,"kVTPixelTransferPropertyKey_EnableHardwareAcceleratedTransfer");if(!hardware)return 2;settings[(__bridge id)kVTCompressionPropertyKey_PixelTransferProperties]=@{hardware:@NO};}
  st=VTSessionSetProperties(s,(__bridge CFDictionaryRef)settings);printf("settings=%d\n",(int)st);if(st){VTCompressionSessionInvalidate(s);CFRelease(s);return 1;}
  st=VTCompressionSessionPrepareToEncodeFrames(s);printf("prepare=%d\n",(int)st);if(st){VTCompressionSessionInvalidate(s);CFRelease(s);return 1;}
  NSMutableData *input=[NSMutableData data];unsigned submitErrors=0;
  for(NSUInteger i=0;i<count;i++){
    CVPixelBufferRef pixel=frame(width,height,i,input,usePool?VTCompressionSessionGetPixelBufferPool(s):NULL);if(!pixel)return 2;
    NSDictionary *options=@{(__bridge id)kVTEncodeFrameOptionKey_ForceKeyFrame:@(YES)};VTEncodeInfoFlags flags=0;
    st=VTCompressionSessionEncodeFrame(s,pixel,CMTimeMake((int64_t)i,30),CMTimeMake(1,30),(__bridge CFDictionaryRef)options,NULL,&flags);CFRelease(pixel);
    printf("submit=%lu status=%d flags=%u\n",(unsigned long)i,(int)st,(unsigned)flags);if(st){submitErrors++;break;}
    st=VTCompressionSessionCompleteFrames(s,CMTimeMake((int64_t)i,30));if(st){submitErrors++;break;}
  }
  OSStatus complete=VTCompressionSessionCompleteFrames(s,kCMTimeInvalid);printf("complete=%d callbacks=%lu errors=%u dropped=%u\n",(int)complete,(unsigned long)capture.samples.count,capture.errors,capture.dropped);
  NSArray *packets=writeSamples(capture.samples,[prefix stringByAppendingString:@".h264"]);
  if(![input writeToFile:[prefix stringByAppendingString:@".nv12"] atomically:YES])return 2;
  report[@"intra_only"]=@YES;report[@"encoder_pool_input"]=@(usePool);report[@"software_pixel_transfer"]=@(softwareTransfer);report[@"width"]=@(width);report[@"height"]=@(height);report[@"requested_frames"]=@(count);report[@"frames"]=packets?:@[];report[@"callbacks"]=@(capture.samples.count);report[@"callback_errors"]=@(capture.errors);report[@"dropped"]=@(capture.dropped);report[@"submit_errors"]=@(submitErrors);report[@"complete_status"]=@(complete);report[@"target_gpu_registry_id"]=@(gpu.registryID);
  NSData *json=[NSJSONSerialization dataWithJSONObject:report options:NSJSONWritingPrettyPrinted|NSJSONWritingSortedKeys error:NULL];[json writeToFile:[prefix stringByAppendingString:@".json"] atomically:YES];
  VTCompressionSessionInvalidate(s);CFRelease(s);
  return (!packets||capture.samples.count!=count||submitErrors||capture.errors||capture.dropped||complete)?1:0;

}}
