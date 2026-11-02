#!/usr/bin/env python3
"""以 CPU 内存验证绑定采集的三个阶段，以及第二帧提交前截停。"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).with_name('xe-vdenc-binding-capture.c')


class BindingCaptureTests(unittest.TestCase):
    """直接编译并运行采集器核心，避免只检查源码中的开关。"""

    def test_post_submit_snapshot_and_second_frame_stop(self):
        """首帧三个阶段保留不同数据，第二帧写出后必须以 77 退出。"""
        self.assertTrue(SOURCE.exists(), '尚未实现提交返回后的绑定采集器')
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            harness = root / 'capture-test.c'
            harness.write_text('#define XE_BINDING_TEST 1\n#include "' + str(SOURCE) + '"\n' + r'''
// 构造用户态内存与模拟内核回填，只验证采集和截停，不连接 GPU。
int main(int argc, char **argv) {
    (void)argc;
    unsigned char token[0xce8] = {0}, batch[128] = {0};
    unsigned char encoder[0x6bd0] = {0}, command[0x78] = {0};
    initialize_files(argv[1]);
    *(const unsigned char **)(encoder + 0x6bc8) = token;
    *(const unsigned char **)(command + 0x10) = batch;
    *(const unsigned char **)(command + 0x70) = batch + sizeof batch;
    context = command;
    token[0] = 1;
    capture_ready(encoder);
    token[0x10] = 0x42; batch[0x10] = 0x31;
    capture_submitted(0, 0, 0);
    token[0x10] = 0x43; batch[0x10] = 0x32;
    capture_finished(0, 17);
    token[0] = 2;
    capture_ready(encoder);
    // 只有截停失效才会执行；测试须拒绝此退出码。
    return 99;
}
''')
            program = root / 'capture-test'
            built = subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                                    str(harness), '-o', str(program)], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run([str(program), str(root)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 77, result.stderr)
            before = (root / 'frame-0-before-token.bin').read_bytes()
            submitted = (root / 'frame-0-submitted-token.bin').read_bytes()
            finished = (root / 'frame-0-finished-token.bin').read_bytes()
            second = (root / 'frame-1-before-token.bin').read_bytes()
            self.assertEqual((before[0x10], submitted[0x10], finished[0x10]), (0, 0x42, 0x43))
            self.assertEqual((before[0], second[0]), (1, 2))
            self.assertEqual((root / 'frame-0-submitted-batch.bin').read_bytes()[0x10], 0x31)
            self.assertEqual((root / 'frame-0-finished-batch.bin').read_bytes()[0x10], 0x32)
            self.assertIn('finish status=0 fence=17', (root / 'binding-events.txt').read_text())
            self.assertFalse((root / 'frame-1-submitted-token.bin').exists())


    def test_second_frame_can_bind_only_after_batch_end_guard(self):
        """第二帧只做资源绑定时，先结束 GPU 批次，回填采集后直接退出。"""
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            harness = root / 'bind-only-test.c'
            harness.write_text('#define XE_BINDING_TEST 1\n#include "' + str(SOURCE) + '"\n' + r'''
// 模拟两次内核提交，验证第二批次只含批次结束指令。
int main(int argc, char **argv) {
    (void)argc;
    unsigned char token[0xce8] = {0}, batch[128] = {0};
    unsigned char encoder[0x6bd0] = {0}, command[0x78] = {0};
    initialize_files(argv[1]);
    bind_only_second = 1;
    *(const unsigned char **)(encoder + 0x6bc8) = token;
    *(const unsigned char **)(command + 0x10) = batch;
    *(const unsigned char **)(command + 0x70) = batch + sizeof batch;
    context = command;
    capture_ready(encoder);
    capture_submitted(0, 0, 0);
    capture_finished(0, 17);
    *(uint32_t *)batch = 0x13000003;
    capture_ready(encoder);
    if (*(uint32_t *)batch != 0x05000000) return 98;
    token[0x10] = 0x55;
    capture_submitted(0, 0, 0);
    return 99;
}
''')
            program = root / 'bind-only-test'
            built = subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                                    str(harness), '-o', str(program)], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run([str(program), str(root)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 77, result.stderr)
            self.assertEqual((root / 'frame-1-submitted-token.bin').read_bytes()[0x10], 0x55)
            guarded = (root / 'frame-1-guarded-batch.bin').read_bytes()
            self.assertEqual(guarded[:4], bytes.fromhex('00000005'))
            before = (root / 'frame-1-before-batch.bin').read_bytes()
            self.assertEqual(before[:4], bytes.fromhex('03000013'))
            self.assertIn('stop after second frame binding',
                          (root / 'binding-events.txt').read_text())


    def test_interposed_memmove_preserves_overlapping_bytes(self):
        """实际加载 dyld 拦截器，验证系统 memmove 别名仍保持向后复制。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'overlap.c'
            source.write_text(r'''
#include <string.h>
#include <stddef.h>
// 实际调用系统 memmove，使其经过 dyld 的 memcpy 别名拦截。
int main(void) {
    unsigned char data[1024];
    for (size_t i = 0; i < sizeof data; ++i) data[i] = i % 251;
    memmove(data + 136, data, 888);
    for (size_t i = 0; i < 888; ++i)
        if (data[i + 136] != i % 251) return 99;
    return 0;
}
''')
            program = root / 'overlap'
            subprocess.run(['clang', '-fno-builtin', str(source), '-o', str(program)],
                           capture_output=True, check=True)
            for capture_source in [SOURCE, SOURCE.with_name('xe-vdenc-address-lab.c')]:
                with self.subTest(source=capture_source.name):
                    library = root / (capture_source.stem + '.dylib')
                    built = subprocess.run(['clang', '-dynamiclib', '-std=c11', '-Wall', '-Wextra',
                        '-Werror', '-Wno-c2x-extensions', '-undefined', 'dynamic_lookup',
                        str(capture_source), '-o', str(library)], capture_output=True, text=True)
                    self.assertEqual(built.returncode, 0, built.stderr)
                    environment = os.environ.copy()
                    environment.update(DYLD_INSERT_LIBRARIES=str(library), XE_CAPTURE_DIR=str(root),
                                       XE_VDENC_GEN12_ADDR='1')
                    result = subprocess.run([str(program)], env=environment, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0,
                                     '重叠拷贝被 memcpy 拦截器破坏: ' + result.stderr)

    def test_capture_multiple_completed_frames_in_separate_files(self):
        """显式多帧模式逐次保存回填和完成快照，达到上限后退出。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = root / 'multiple.c'
            harness.write_text('#define XE_BINDING_TEST 1\n#include "' + str(SOURCE) + '"\n' + r'''
// 用 CPU 内存模拟三帧完成，验证不会覆盖前一帧的证据。
int main(int argc, char **argv) {
    (void)argc;
    unsigned char token[0xce8] = {0}, batch[128] = {0};
    unsigned char encoder[0x6bd0] = {0}, command[0x78] = {0};
    initialize_files(argv[1]);
    capture_frames = 3;
    *(const unsigned char **)(encoder + 0x6bc8) = token;
    *(const unsigned char **)(command + 0x10) = batch;
    *(const unsigned char **)(command + 0x70) = batch + sizeof batch;
    context = command;
    for (unsigned index = 0; index < 3; ++index) {
        token[0x10] = index;
        capture_ready(encoder);
        token[0x10] += 10;
        capture_submitted(0, 0, 0);
        capture_finished(0, index + 1);
    }
    return 99;
}
''')
            program = root / 'multiple'
            built = subprocess.run(['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                                    str(harness), '-o', str(program)], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run([str(program), str(root)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 77)
            for index in range(3):
                self.assertEqual((root / f'frame-{index}-before-token.bin').read_bytes()[0x10], index)
                self.assertEqual((root / f'frame-{index}-finished-token.bin').read_bytes()[0x10], index + 10)



if __name__ == '__main__':
    unittest.main()
