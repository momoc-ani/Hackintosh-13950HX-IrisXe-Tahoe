// 自有进程诊断：记录 VA 能力、请求链和创建返回值，不改变请求与原处理结果。
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#ifndef XE_CONTEXT_TRACE_TEST
#include <stdlib.h>
#include <dlfcn.h>
#endif

static int (*original_contexts)(void *,void **);
static int (*original_parameters)(void *,uint32_t *);
static unsigned sequence;

// 读取已核对的整数/指针字段，避免从可能未对齐节点直接强转解引用。
static uint64_t read64(const unsigned char *source){uint64_t value;memcpy(&value,source,8);return value;}
// 读取节点类型、数量或用途字段，仅服务诊断输出。
static uint32_t read32(const unsigned char *source){uint32_t value;memcpy(&value,source,4);return value;}

// 输出 Tahoe 转换后能力链；0x430 是原适配已确认的 next 指针位置。
static void trace_capabilities(const unsigned char *header){
    if(!header)return;
    uint32_t count=read32(header+0x10);
    const unsigned char **roots=(const unsigned char **)(uintptr_t)read64(header+0x18);
    for(uint32_t i=0;i<count;i++){
        unsigned depth=0;
        for(const unsigned char *node=roots[i];node;node=(const unsigned char *)(uintptr_t)read64(node+0x430)){
            fprintf(stderr,"VA-CONTEXT {\"event\":\"available\",\"root\":%u,\"depth\":%u,\"kind\":%u,\"capability\":\"0x%llx\",\"id\":%u,\"usage\":%u}\n",
                i,depth++,read32(node),(unsigned long long)read64(node+8),read32(node+0x10),read32(node+0x14));
        }
    }
}

// 在原函数前后保存同一序号，失败时也能判断是哪个完整请求链被拒绝。
static int trace_contexts(void *accelerator,void **request){
    unsigned current=sequence++;
    const unsigned char *header=*request;uint32_t count=read32(header);
    const unsigned char **nodes=(const unsigned char **)(uintptr_t)read64(header+8);
    for(uint32_t i=0;i<count;i++){
        unsigned depth=0;
        for(const unsigned char *node=nodes[i];node;node=(const unsigned char *)(uintptr_t)read64(node+0x38)){
            fprintf(stderr,"VA-CONTEXT {\"event\":\"request\",\"sequence\":%u,\"root\":%u,\"depth\":%u,\"kind\":%u,\"capability\":\"0x%llx\",\"id\":%u,\"usage\":%u}\n",
                current,i,depth++,read32(node),(unsigned long long)read64(node+8),read32(node+0x10),read32(node+0x14));
        }
    }
    int status=original_contexts(accelerator,request);
    fprintf(stderr,"VA-CONTEXT {\"event\":\"result\",\"sequence\":%u,\"status\":%d}\n",current,status);
    return status;
}

// 记录 SetContextParameters 的 0x30 字节条目，定位 streamout 的能力、参数和 handle。
static int trace_parameters(void *accelerator,uint32_t *request){
    unsigned current=sequence++;const unsigned char *header=(const unsigned char *)request;
    const unsigned char *entries=(const unsigned char *)(uintptr_t)read64(header+8);
    for(uint32_t i=0;i<read32(header);i++){
        const unsigned char *entry=entries+i*0x30;
        fprintf(stderr,"VA-CONTEXT {\"event\":\"parameter\",\"sequence\":%u,\"entry\":%u,\"kind\":%u,\"capability\":\"0x%llx\",\"id\":%u,\"context_present\":%s,\"parameter\":%u}\n",
            current,i,read32(entry),(unsigned long long)read64(entry+8),read32(entry+0x10),read64(entry+0x18)?"true":"false",read32(entry+0x20));
    }
    int status=original_parameters(accelerator,request);
    fprintf(stderr,"VA-CONTEXT {\"event\":\"parameter-result\",\"sequence\":%u,\"status\":%d}\n",current,status);
    return status;
}

#ifndef XE_CONTEXT_TRACE_TEST
static int (*original_create)(void **,const void *,void *);

// VA 创建完成后仅替换本进程函数表入口，能力数据及初始化结果保持原样。
static int trace_create(void **accelerator,const void *configuration,void *info){
    int status=original_create(accelerator,configuration,info);
    fprintf(stderr,"VA-CONTEXT {\"event\":\"accelerator\",\"status\":%d}\n",status);
    if(!status&&accelerator&&*accelerator){
        void **table=*accelerator;original_contexts=(void *)table[3];table[3]=(void *)trace_contexts;
        original_parameters=(void *)table[13];table[13]=(void *)trace_parameters;
        trace_capabilities((const unsigned char *)(uintptr_t)read64((const unsigned char *)info+8));
    }
    return status;
}

// 只拦截指定 Intel VA 的导出解析，其他驱动与符号使用系统 dlsym。
static void *trace_lookup(void *handle,const char *name){
    void *result=dlsym(handle,name);
    if(result&&!strcmp(name,"AVD_CreateAVDAccelerator")){
        Dl_info info;
        if(dladdr(result,&info)&&strstr(info.dli_fname,"AppleIntelTGLGraphicsVADriver")){
            original_create=result;return(void *)trace_create;
        }
    }
    return result;
}
__attribute__((used)) static struct{const void *replacement,*original;} interpose
    __attribute__((section("__DATA,__interpose")))={(const void *)trace_lookup,(const void *)dlsym};
#endif
