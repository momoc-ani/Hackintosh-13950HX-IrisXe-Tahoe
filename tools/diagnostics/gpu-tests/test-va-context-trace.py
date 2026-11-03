#!/usr/bin/env python3
"""执行 VA 创建诊断核心，验证日志能定位不匹配且不改变返回值或请求。"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).with_name("xe-va-context-trace.c")


class ContextTraceTests(unittest.TestCase):
    """仅使用真实 C 调用和内存对象，不创建 GPU 上下文。"""

    def test_opt_in_remap_changes_only_encode_capability_and_handler_status(self):
        """默认请求保持原状，显式启用后仅将编码能力与参数项映射为已测 VDENC 值。"""
        self.assertTrue(SOURCE.exists(), "尚未实现可保留返回值的 VA 创建诊断")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "test.c"
            source.write_text('#include <stdlib.h>\n#define XE_CONTEXT_TRACE_TEST\n#include "' + str(SOURCE) + '"\n' + r'''
// 原函数仅记录本次调用并返回指定状态，不触发 GPU。
static int calls;
static uint64_t observed_capability;
static uint64_t observed_parameter_capability;
// 记录原始 VA 函数实际收到的 capability，并保留既定返回码。
static int original(void *accelerator, void **request) {
    (void)accelerator;
    const unsigned char *header=*request;
    const unsigned char **nodes=(const unsigned char **)(uintptr_t)read64(header+8);
    observed_capability=read64(nodes[0]+8);calls++;return 7;
}
// 模拟参数入口报告缺少 context，诊断必须保留错误码。
static int parameter_original(void *accelerator, uint32_t *request) {
    (void)accelerator;
    const unsigned char *header=(const unsigned char *)request;
    const unsigned char *entries=(const unsigned char *)(uintptr_t)read64(header+8);
    observed_parameter_capability=read64(entries+8);
    return observed_parameter_capability==0x400?0:4;
}
// 构造一条可用能力和不匹配请求，检查诊断保留所有输入字节。
// 以默认和显式启用环境分别运行，确认映射只影响 opt-in 请求。
int main(void) {
    unsigned char node[0x438]={0}, header[0x20]={0}, request_node[0x40]={0};
    unsigned char *nodes[]={node}, *requested[]={request_node};
    uint32_t count=1,kind=0x20;uint64_t available=0x400,wanted=0x4000;
    memcpy(header+0x10,&count,4);
    void *array=nodes;memcpy(header+0x18,&array,8);
    memcpy(node,&kind,4);memcpy(node+8,&available,8);
    memcpy(request_node,&kind,4);memcpy(request_node+8,&wanted,8);
    unsigned char request_header[16]={0};memcpy(request_header,&count,4);
    array=requested;memcpy(request_header+8,&array,8);
    void *request=request_header;unsigned char before[0x40];memcpy(before,request_node,sizeof before);
    trace_capabilities(header);original_contexts=original;
    if(trace_contexts(NULL,&request)!=7||calls!=1)return 99;
    const char *enabled=getenv("XE_USAGE0_REMAP");
    if(enabled&&strcmp(enabled,"1")==0){
        if(observed_capability!=0x400||read64(request_node+8)!=0x400)return 97;
        if(memcmp(before,request_node,8)||memcmp(before+16,request_node+16,sizeof before-16))return 96;
    }else{
        if(observed_capability!=0x4000||memcmp(before,request_node,sizeof before))return 95;
    }
    unsigned char parameter[0x30]={0},parameter_header[0x10]={0};
    memcpy(parameter,&kind,4);memcpy(parameter+8,&wanted,8);
    array=parameter;memcpy(parameter_header,&count,4);memcpy(parameter_header+8,&array,8);
    original_parameters=parameter_original;
    int expected_parameter_status=enabled&&strcmp(enabled,"1")==0?0:4;
    uint64_t expected_parameter_capability=expected_parameter_status==0?0x400:0x4000;
    if(trace_parameters(NULL,(uint32_t *)parameter_header)!=expected_parameter_status)return 98;
    if(observed_parameter_capability!=expected_parameter_capability)return 94;
    return 0;
}
''')
            executable = root / "test"
            result = subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                                     str(source), str(SOURCE.with_name("xe-usage0-remap.c")),
                                     "-o", str(executable)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            default_environment = os.environ.copy()
            default_environment.pop("XE_USAGE0_REMAP", None)
            for environment in (default_environment,
                                {**default_environment, "XE_USAGE0_REMAP": "1"}):
                result = subprocess.run([str(executable)], env=environment,
                                         capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('"capability":"0x400"', result.stderr)
                self.assertIn('"status":7', result.stderr)
                self.assertIn('"event":"parameter"', result.stderr)
                if environment.get("XE_USAGE0_REMAP") == "1":
                    self.assertIn('"event":"usage0-remap-node"', result.stderr)
                    self.assertIn('"old_capability":"0x4000"', result.stderr)
                    self.assertIn('"new_capability":"0x400"', result.stderr)
                    self.assertIn('"event":"usage0-remap-result"', result.stderr)
                    self.assertIn('"count":1', result.stderr)
                    self.assertIn('"event":"usage0-remap-parameter"', result.stderr)
                    self.assertIn('"event":"usage0-remap-parameters-result"', result.stderr)
                    self.assertIn('"event":"parameter-result"', result.stderr)
                    self.assertIn('"status":0', result.stderr)
                else:
                    self.assertNotIn('"event":"usage0-remap-node"', result.stderr)
                    self.assertIn('"capability":"0x4000"', result.stderr)
                    self.assertIn('"status":4', result.stderr)


if __name__ == "__main__":
    unittest.main()
