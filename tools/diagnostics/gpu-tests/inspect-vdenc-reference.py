#!/usr/bin/env python3
"""静态核对两帧已绑定 VDENC 捕获的参考资源、slice 和完成等待。"""
import argparse
import json
from pathlib import Path
import struct


def commands(batch):
    """按 MI/媒体各自的长度字段解码，不把命令 payload 当作新命令。"""
    offset = 0
    while offset < len(batch):
        header = struct.unpack_from('<I', batch, offset)[0]
        if header >> 29 == 0:
            opcode = (header >> 23) & 63
            length = 1 if opcode in (0, 5, 10) else (header & 63) + 2
        else:
            length = (header & 4095) + 2
        if offset + length * 4 > len(batch):
            raise ValueError(f'命令 {offset:#x} 超过捕获范围')
        yield {'offset': offset, 'header': header,
               'words': list(struct.unpack_from(f'<{length}I', batch, offset))}
        offset += length * 4


def resource(token, kernel_offset):
    """从内核字段减去用户态 token 的 8 字节差值，读取已回填资源。"""
    index = struct.unpack_from('<I', token, kernel_offset - 8)[0]
    handle, _, low, high, pitch, flags = struct.unpack_from('<6I', token, 8 + index * 24)
    return {'index': index, 'handle': handle, 'address': low | high << 32,
            'pitch': pitch, 'flags': flags}


def address(words, index):
    """读取媒体命令中的低/高地址对，不使用 MOCS 字作为地址。"""
    return words[index] | words[index + 1] << 32


def surface(command):
    """解码 Gen12 VDENC 六 DWORD 表面状态的尺寸、平面和格式字段。"""
    words = command['words']
    return {'width': ((words[2] >> 4) & 0x3fff) + 1,
            'height': (words[2] >> 18) + 1,
            'pitch': ((words[3] >> 3) & 0x1ffff) + 1,
            'format': words[3] >> 27, 'tile_mode': words[3] & 3,
            'chroma_u_row': words[4] & 0xffff, 'chroma_v_row': words[5] & 0xffff}


def audit(batches, tokens):
    """核对已观察字段并列出格式差异，绝不从结构一致推断硬件编码成功。"""
    parsed = [list(commands(batch)) for batch in batches]
    entries = parsed[1]
    by_type = {}
    for entry in entries:
        by_type.setdefault(entry['header'] >> 16, []).append(entry)
    mfx = by_type[0x7002][0]['words']
    vdenc = by_type[0x7084][0]['words']
    img = by_type[0x7085][0]['words']
    ref_state = surface(by_type[0x7082][0])
    ds_state = surface(by_type[0x7083][0])
    previous_recon = resource(tokens[0], 0xc60)
    previous_ds = resource(tokens[0], 0xc68)
    previous_uv = resource(tokens[0], 0xc64)
    previous_ds_uv = resource(tokens[0], 0xc6c)
    ref = resource(tokens[1], 0xcc0)
    ds_ref = resource(tokens[1], 0xccc)
    refs = by_type[0x7104]
    ref_entries = [struct.pack('<8I', *entry['words'][2:10]) for entry in refs]
    rows = (img[5] & 0xffff) + 1
    slices, active = [], None
    for entry in entries:
        kind, words = entry['header'] >> 16, entry['words']
        if kind == 0x7103:
            active = {'offset': f"0x{entry['offset']:x}", 'slice_type': words[1] & 15,
                      'active_l0_references': (words[2] >> 16) & 63,
                      'active_l1_references': (words[2] >> 24) & 63,
                      'weighted_prediction': (words[3] >> 30) & 3,
                      'start_row': words[4] >> 24,
                      'next_row': words[5] >> 16,
                      'last_slice': bool(words[6] & (1 << 19))}
            slices.append(active)
        elif kind == 0x7087 and active is not None:
            active['walker_start_row'] = words[1] & 511
            active['walker_next_row'] = words[2] & 1023
            active['walker_dwords'] = len(words)
        elif kind == 0x7780 and active is not None:
            active['flush_word'] = f'0x{words[1]:08x}'
            active['wait_vdenc_mfx_parser_and_flush_vdenc'] = words[1] & 0x2001a == 0x2001a
    checks = {
        'full_reference_handle_and_address_match_previous_recon':
            (ref['handle'], ref['address']) == (previous_recon['handle'], previous_recon['address']),
        'ds_reference_handle_and_address_match_previous_ds':
            (ds_ref['handle'], ds_ref['address']) == (previous_ds['handle'], previous_ds['address']),
        'vdenc_and_mfx_reference_match_previous_recon':
            address(vdenc, 22) == address(mfx, 19) == previous_recon['address'],
        'vdenc_ds_reference_matches_previous_ds': address(vdenc, 1) == previous_ds['address'],
        'mfx_ref0_selects_valid_store_zero': bool(ref_entries) and all(entry[0] == 0
            and all(value == 0x80 for value in entry[1:]) for entry in ref_entries),
        'p_slice_reference_counts_match_single_reference': bool(slices) and
            all(s['slice_type'] == 0 and s['active_l0_references'] == 1 and
                s['active_l1_references'] == 0 for s in slices),
        'slice_and_walker_row_ranges_match': bool(slices) and all(
            (s['start_row'], s['next_row']) ==
            (s.get('walker_start_row'), s.get('walker_next_row')) for s in slices),
        'slices_cover_frame_rows_without_gaps': bool(slices) and slices[0]['start_row'] == 0
            and slices[-1]['next_row'] == rows and all(
                a['next_row'] == b['start_row'] for a, b in zip(slices, slices[1:])),
        'all_slices_wait_vdenc_mfx_parser_and_flush_vdenc': bool(slices) and all(
            s.get('wait_vdenc_mfx_parser_and_flush_vdenc', False) for s in slices),
        'reference_surface_pitch_matches_bound_resource': ref_state['pitch'] == ref['pitch'],
        'ds_surface_pitch_matches_bound_resource': ds_state['pitch'] == ds_ref['pitch'],
        'full_chroma_offset_matches_previous_bound_planes':
            previous_uv['address'] - previous_recon['address'] == ref_state['chroma_u_row'] * ref['pitch'],
        'ds_chroma_offset_matches_previous_bound_planes':
            previous_ds_uv['address'] - previous_ds['address'] == ds_state['chroma_u_row'] * ds_ref['pitch'],
        'reference_formats_are_planar_420_8': ref_state['format'] == ds_state['format'] == 4,
    }
    return {'checks': checks, 'previous_recon': previous_recon, 'previous_ds': previous_ds,
            'reference_surface': ref_state, 'ds_reference_surface': ds_state, 'slices': slices,
            'observed_layout': {'mode_dwords': len(by_type[0x7080][0]['words']),
                'address_dwords': len(vdenc), 'ds_surface_dwords': len(by_type[0x7083][0]['words']),
                'walker_dwords': [len(e['words']) for e in by_type[0x7087]],
                'walker_tile_width_present': all(len(e['words']) > 5 for e in by_type[0x7087]),
                'default_weights_present': 0x7088 in by_type},
            'gen12_reference': {'mode_dwords': 6, 'address_dwords': 71,
                'ds_surface_dwords': 10, 'walker_dwords': 27,
                'walker_tile_width': ref_state['width'] - 1,
                'walker_reserved_dw14': '0x3f400000', 'walker_reserved_dw16': '0x003f3f3f'},
            'p_frame_hardware_pass': None,
            'limitations': ['仅适用于这组 AVC 单参考、逐行帧、Gen12 字段的捕获',
                '结构核对不能证明 GPU 数据读取、缓存一致性或 P 帧编码已成功',
                '格式差异分别记录；不能把短命令差异直接判定为根因']}


def main():
    """从本地已绑定捕获生成可审阅 JSON，不运行驱动或写入系统。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    batches = [(args.capture / f'frame-{i}-submitted-batch.bin').read_bytes() for i in [0, 1]]
    tokens = [(args.capture / f'frame-{i}-submitted-token.bin').read_bytes() for i in [0, 1]]
    report = audit(batches, tokens)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report['checks'], ensure_ascii=False))


if __name__ == '__main__':
    main()
