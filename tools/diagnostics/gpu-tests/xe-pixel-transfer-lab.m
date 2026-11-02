// 对照启用硬件转换与纯软件转换，检查普通 VPHAL 被拒绝后是否能正确回退。
// 编译：clang -fobjc-arc -Wall -Wextra -Werror xe-pixel-transfer-lab.m \
//   -framework Foundation -framework VideoToolbox -framework CoreMedia -framework CoreVideo -o xe-pixel-transfer-lab
// 用法：xe-pixel-transfer-lab 输出目录；只影响当前进程。
#import <Foundation/Foundation.h>
#import <VideoToolbox/VideoToolbox.h>
#include <dlfcn.h>
#include <stdio.h>

// 创建具有已知非零图案和 IOSurface 的 NV12 / BGRA 输入。
static CVPixelBufferRef pixel(size_t width,size_t height,OSType format,BOOL fill){
 CVPixelBufferRef p=NULL;NSDictionary *a=@{(__bridge id)kCVPixelBufferIOSurfacePropertiesKey:@{}};
 if(CVPixelBufferCreate(NULL,width,height,format,(__bridge CFDictionaryRef)a,&p))return NULL;
 if(!fill)return p;
 CVPixelBufferLockBaseAddress(p,0);
 if(format==kCVPixelFormatType_32BGRA){
  uint8_t *base=CVPixelBufferGetBaseAddress(p);size_t pitch=CVPixelBufferGetBytesPerRow(p);
  for(size_t y=0;y<height;y++)for(size_t x=0;x<width;x++){uint8_t *q=base+y*pitch+x*4;q[0]=(x/20)%200+20;q[1]=(y/20)%200+20;q[2]=(x/40+y/40)%200+20;q[3]=255;}
 }else{
  for(size_t i=0;i<2;i++){uint8_t *base=CVPixelBufferGetBaseAddressOfPlane(p,i);size_t pitch=CVPixelBufferGetBytesPerRowOfPlane(p,i),rows=i?height/2:height;
   for(size_t y=0;y<rows;y++)for(size_t x=0;x<width;x++)base[y*pitch+x]=i?(x%2?110+(y/20)%25:125+(x/40)%25):32+(x/20+y/20)%12*15;
  }
 }
 CVPixelBufferUnlockBaseAddress(p,0);return p;
}

// 保存有效图像行，排除分配对齐产生的 padding 字节。
static NSData *bytes(CVPixelBufferRef p){
 if(CVPixelBufferLockBaseAddress(p,kCVPixelBufferLock_ReadOnly))return nil;
 NSMutableData *data=[NSMutableData data];size_t planes=CVPixelBufferGetPlaneCount(p),width=CVPixelBufferGetWidth(p);
 if(!planes){const uint8_t *base=CVPixelBufferGetBaseAddress(p);size_t pitch=CVPixelBufferGetBytesPerRow(p);
  for(size_t y=0;y<CVPixelBufferGetHeight(p);y++)[data appendBytes:base+y*pitch length:width*4];
 }else for(size_t i=0;i<planes;i++){const uint8_t *base=CVPixelBufferGetBaseAddressOfPlane(p,i);size_t pitch=CVPixelBufferGetBytesPerRowOfPlane(p,i);
  for(size_t y=0;y<CVPixelBufferGetHeightOfPlane(p,i);y++)[data appendBytes:base+y*pitch length:width];
 }
 CVPixelBufferUnlockBaseAddress(p,kCVPixelBufferLock_ReadOnly);return data;
}

// 仅改变硬件转换开关，同一输入在两个独立转换会话中执行。
static NSData *transfer(CVPixelBufferRef input,size_t w,size_t h,OSType format,BOOL hardware,OSStatus *result){
 VTPixelTransferSessionRef session=NULL;*result=VTPixelTransferSessionCreate(NULL,&session);if(*result)return nil;
 void *vt=dlopen("/System/Library/Frameworks/VideoToolbox.framework/VideoToolbox",RTLD_NOW);
 const char *symbols[]={"kVTPixelTransferPropertyKey_EnableHardwareAcceleratedTransfer","kVTPixelTransferPropertyKey_EnableGPUAcceleratedTransfer"};
 for(unsigned i=0;i<2;i++){CFStringRef *key=dlsym(vt,symbols[i]);if(!key){*result=-1;break;}
  *result=VTSessionSetProperty(session,*key,i==0&&hardware?kCFBooleanTrue:kCFBooleanFalse);if(*result)break;
 }
 CVPixelBufferRef output=pixel(w,h,format,NO);NSData *data=nil;
 if(!*result&&output){*result=VTPixelTransferSessionTransferImage(session,input,output);if(!*result)data=bytes(output);}
 if(output)CFRelease(output);VTPixelTransferSessionInvalidate(session);CFRelease(session);return data;
}

// 覆盖同尺寸拷贝、颜色转换和缩放，任一有效像素差异都返回失败。
int main(int argc,const char **argv){@autoreleasepool{
 if(argc!=2)return 2;NSString *directory=@(argv[1]);[NSFileManager.defaultManager createDirectoryAtPath:directory withIntermediateDirectories:YES attributes:nil error:NULL];
 NSArray *cases=@[@[@"nv12-copy",@0,@0,@1920,@1080],@[@"nv12-bgra",@0,@1,@1920,@1080],@[@"bgra-nv12",@1,@0,@1920,@1080],@[@"bgra-scale",@1,@1,@960,@540],@[@"nv12-scale",@0,@0,@1280,@720]];
 NSMutableArray *reports=[NSMutableArray array];unsigned failures=0;
 for(NSArray *row in cases){@autoreleasepool{
  CVPixelBufferRef input=pixel(1920,1080,[row[1] boolValue]?kCVPixelFormatType_32BGRA:kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,YES);if(!input)return 2;
  OSType format=[row[2] boolValue]?kCVPixelFormatType_32BGRA:kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange;OSStatus hw=0,sw=0;
  NSData *reference=transfer(input,[row[3] unsignedLongValue],[row[4] unsignedLongValue],format,NO,&sw);
  NSData *actual=transfer(input,[row[3] unsignedLongValue],[row[4] unsignedLongValue],format,YES,&hw);
  BOOL same=actual&&reference&&[actual isEqualToData:reference];const uint8_t *p=actual.bytes;NSUInteger nonzero=0;for(NSUInteger i=0;i<actual.length;i++)nonzero+=p[i]!=0;
  BOOL pass=!hw&&!sw&&same&&nonzero;failures+=!pass;
  NSDictionary *report=@{@"case":row[0],@"software_status":@(sw),@"hardware_enabled_status":@(hw),@"bytes":@(actual.length),@"nonzero_bytes":@(nonzero),@"pixel_exact":@(same),@"pass":@(pass)};[reports addObject:report];printf("%s software=%d hardware-enabled=%d exact=%d %s\n",[row[0] UTF8String],(int)sw,(int)hw,same,pass?"PASS":"FAIL");
  [reference writeToFile:[directory stringByAppendingPathComponent:[row[0] stringByAppendingString:@"-software.raw"]] atomically:YES];[actual writeToFile:[directory stringByAppendingPathComponent:[row[0] stringByAppendingString:@"-hardware-enabled.raw"]] atomically:YES];CFRelease(input);
 }}
 [[NSJSONSerialization dataWithJSONObject:reports options:NSJSONWritingPrettyPrinted|NSJSONWritingSortedKeys error:NULL] writeToFile:[directory stringByAppendingPathComponent:@"results.json"] atomically:YES];return failures?1:0;
}}
