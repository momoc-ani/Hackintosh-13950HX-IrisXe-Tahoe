#!/usr/bin/env python3
"""验证参考与同步审计能发现实际捕获中的错误，而不触发 GPU。"""
from pathlib import Path
import importlib.util
import struct
import unittest

ROOT = Path(__file__).resolve().parents[3]
CAPTURE = ROOT / 'docs/evidence/20261102-kext-icons-and-encode/vdenc-binding'
SOURCE = Path(__file__).with_name('inspect-vdenc-reference.py')


class ReferenceAuditTests(unittest.TestCase):
    """审计输出区分当前一致性、格式差异和未完成的硬件验收。"""

    def load_audit(self):
        """加载实际审计器及首帧/第二帧已绑定捕获。"""
        self.assertTrue(SOURCE.exists(), '尚未实现参考和同步的可复现审计')
        spec = importlib.util.spec_from_file_location('vdenc_reference_audit', SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        frames = [(CAPTURE / f'frame-{i}-submitted-batch.bin').read_bytes() for i in [0, 1]]
        tokens = [(CAPTURE / f'frame-{i}-submitted-token.bin').read_bytes() for i in [0, 1]]
        return module, frames, tokens

    def test_actual_reference_and_sync_fields(self):
        """本次实际资源和同步字段一致，但不能据此声称 P 帧通过。"""
        module, frames, tokens = self.load_audit()
        report = module.audit(frames, tokens)
        self.assertTrue(all(report['checks'].values()), report['checks'])
        self.assertEqual(report['slices'][0]['start_row'], 0)
        self.assertEqual(report['slices'][-1]['next_row'], 45)
        self.assertTrue(all(s['active_l0_references'] == 1 for s in report['slices']))
        self.assertTrue(all(s['flush_word'] == '0x0002001a' for s in report['slices']))
        self.assertEqual(report['observed_layout']['walker_dwords'], [4, 4, 4, 4])
        self.assertEqual(report['gen12_reference']['walker_dwords'], 27)
        self.assertFalse(report['observed_layout']['walker_tile_width_present'])
        self.assertFalse(report['observed_layout']['default_weights_present'])
        self.assertIsNone(report['p_frame_hardware_pass'])

    def test_wrong_reference_address_is_rejected(self):
        """有效句柄对应错误 VDENC GPU 地址时必须报告不一致。"""
        module, frames, tokens = self.load_audit()
        frames[1] = bytearray(frames[1])
        struct.pack_into('<I', frames[1], 0x274 + 22 * 4, 0x404d4000)
        report = module.audit(frames, tokens)
        self.assertFalse(report['checks']['vdenc_and_mfx_reference_match_previous_recon'])

    def test_missing_vdenc_wait_is_rejected(self):
        """命令存在但缺少 VDENC 完成等待时，审计不能通过。"""
        module, frames, tokens = self.load_audit()
        frames[1] = bytearray(frames[1])
        struct.pack_into('<I', frames[1], 0x8b8, 0x20018)
        report = module.audit(frames, tokens)
        self.assertFalse(report['checks']['all_slices_wait_vdenc_mfx_parser_and_flush_vdenc'])


if __name__ == '__main__':
    unittest.main()
