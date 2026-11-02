#!/usr/bin/env python3
"""执行 VA 创建诊断核心，验证日志能定位不匹配且不改变返回值或请求。"""
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).with_name("xe-va-context-trace.c")


class ContextTraceTests(unittest.TestCase):
    """仅使用真实 C 调用和内存对象，不创建 GPU 上下文。"""

    def test_context_failure_status_and_request_are_preserved(self):
        """请求 0x4000 而可用节点为 0x400，日志须保留两者与原状态 7。"""
        self.assertTrue(SOURCE.exists(), "尚未实现可保留返回值的 VA 创建诊断")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "test.c"
            source.write_text('#define XE_CONTEXT_TRACE_TEST\n#include "' + str(SOURCE) + '"\n' + r'''
// 原函数仅记录本次调用并返回指定状态，不触发 GPU。
static int calls;
static int original(void *accelerator, void **request) {
    (void)accelerator;(void)request;calls++;return 7;
}
// 模拟参数入口报告缺少 context，诊断必须保留错误码。
static int parameter_original(void *accelerator, uint32_t *request) {
    (void)accelerator;(void)request;return 4;
}
// 构造一条可用能力和不匹配请求，检查诊断保留所有输入字节。
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
    if(trace_contexts(NULL,&request)!=7||calls!=1||memcmp(before,request_node,sizeof before))return 99;
    unsigned char parameter[0x30]={0},parameter_header[0x10]={0};
    memcpy(parameter,&kind,4);memcpy(parameter+8,&wanted,8);
    array=parameter;memcpy(parameter_header,&count,4);memcpy(parameter_header+8,&array,8);
    original_parameters=parameter_original;
    if(trace_parameters(NULL,(uint32_t *)parameter_header)!=4)return 98;
    return 0;
}
''')
            executable = root / "test"
            result = subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                                     str(source), "-o", str(executable)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"capability":"0x400"', result.stderr)
            self.assertIn('"capability":"0x4000"', result.stderr)
            self.assertIn('"status":7', result.stderr)
            self.assertIn('"event":"parameter"', result.stderr)
            self.assertIn('"status":4', result.stderr)


if __name__ == "__main__":
    unittest.main()
