#!/usr/bin/env python3
"""在普通内存中执行已核对的 VA 统计命令构建器，不加载 bundle 或连接 GPU。"""
import argparse
import ctypes
import json
import mmap
from pathlib import Path
import platform
import struct


def put32(buffer, offset, value):
    """向夹具对象的指定字段写入 32 位整数。"""
    struct.pack_into("<I", buffer, offset, value)


def put64(buffer, offset, value):
    """向夹具对象写入普通进程内存地址或 64 位字段。"""
    struct.pack_into("<Q", buffer, offset, value)


def text_image(path):
    """检查已分析的 x86_64 Mach-O 布局，只取构建器使用的 __TEXT 代码与常量。"""
    image = Path(path).read_bytes()
    if struct.unpack_from("<II", image) != (0xfeedfacf, 0x1000007):
        raise ValueError("需要已分析的 x86_64 VA Mach-O")
    cursor = 32
    for _ in range(struct.unpack_from("<I", image, 16)[0]):
        command, size = struct.unpack_from("<II", image, cursor)
        if command == 0x19 and image[cursor + 8:cursor + 24].rstrip(b"\0") == b"__TEXT":
            address, memory_bytes, file_offset, file_bytes = struct.unpack_from("<4Q", image, cursor + 24)
            if address or file_offset or file_bytes != memory_bytes:
                raise ValueError("__TEXT 不是已核对的零基址连续映射")
            code = image[:file_bytes]
            for entry in (0x2c84a, 0x2cc16):
                if code[entry:entry + 8] != bytes.fromhex("554889e541574156"):
                    raise ValueError(f"构建入口 {entry:#x} 不匹配")
            if code[0x31d744:0x31d788] != struct.pack("<17I", 0x7103000f, *([0] * 16)):
                raise ValueError("媒体 walker 原始模板不匹配")
            return code
        cursor += size
    raise ValueError("没有 __TEXT 段")


def audit_builder(path, width, height, hardware_threads):
    """用自有内存运行 0x2c84a/0x2cc16；所有间接调用都指向已分析的 CPU 构建函数。"""
    if platform.machine().lower() not in ("x86_64", "amd64"):
        raise ValueError("原生 CPU 核对仅用于 x86_64 主机")
    if not (0 < width <= 7680 and 0 < height <= 4320 and 0 < hardware_threads <= 65536):
        raise ValueError("分辨率或线程夹具超出已核对的字段范围")
    image = text_image(path)
    memory = mmap.mmap(-1, len(image), prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
    memory.write(image)
    base = ctypes.addressof(ctypes.c_char.from_buffer(memory))
    context = ctypes.create_string_buffer(0x4730)
    accelerator = ctypes.create_string_buffer(0xab00)
    vtable = ctypes.create_string_buffer(0x90)
    batch = ctypes.create_string_buffer(4096)
    token = ctypes.create_string_buffer(0x80)
    record = ctypes.create_string_buffer(0x100)
    kernel_indices = (ctypes.c_uint32 * 1)(0)
    resources = [ctypes.create_string_buffer(0x108) for _ in range(3)]

    put64(context, 0, ctypes.addressof(vtable))
    put64(context, 8, ctypes.addressof(accelerator))
    put64(vtable, 0x78, base + 0x2c7ee)
    put64(vtable, 0x80, base + 0x2c84a)
    put32(accelerator, 0xa868, hardware_threads)
    context[0x124] = 1
    for offset, value in {
        0x128: 1, 0x138: width, 0x13c: height, 0x150: 16, 0x158: 16,
        0x160: 32, 0x164: 0x2080, 0x168: 1, 0x170: 5, 0x174: 1, 0x41c8: 1,
    }.items():
        put32(context, offset, value)
    for index, (offset, resource) in enumerate(zip((0x11b8, 0x1a8, 0x21c8), resources)):
        put32(resource, 0x100, 0x1001 + index)
        put64(context, offset, ctypes.addressof(resource))
    put64(token, 0x10, ctypes.addressof(batch))
    put64(token, 0x70, ctypes.addressof(batch))
    put64(context, 0x41d8, ctypes.addressof(token))

    # 这两条静态调用链仅访问夹具内存及 __TEXT 常量；不调用 VA 调度、dyld 或 IOKit。
    prepare = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(base + 0x2c84a)
    build = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p)(base + 0x2cc16)
    try:
        prepare(ctypes.addressof(context))
        tail = build(ctypes.addressof(context), ctypes.addressof(record), kernel_indices, 1, None)
        emitted = tail - ctypes.addressof(batch)
        if not (0 < emitted <= len(batch) and emitted % 4 == 0):
            raise ValueError("CPU 构建尾指针超出夹具 batch")
        words = list(struct.unpack_from(f"<{emitted // 4}I", batch))
        walker_index = words.index(0x7103000f)
        return {
            "source": str(path), "execution": "native-cpu-memory-fixture",
            "gpu_commands_executed": False,
            "native_va_methods": ["0x2c84a", "0x2cc16", "0x2c7ee", "0x1c1b", "0x1c40"],
            "fixture_width": width, "fixture_height": height,
            "fixture_hardware_threads": hardware_threads,
            "block_dimensions": [(width + 15) // 16, (height + 15) // 16],
            "batch_bytes": emitted, "batch_dwords": words,
            "walker_offset_bytes": walker_index * 4, "walker_end_bytes": (walker_index + 17) * 4,
            "walker_dwords": words[walker_index:walker_index + 17],
            "vfe_dwords": list(struct.unpack_from("<9I", context, 0x427c)),
            "curbe_load_dwords": list(struct.unpack_from("<4I", context, 0x42c4)),
            "descriptor_load_dwords": list(struct.unpack_from("<4I", context, 0x42e4)),
        }
    finally:
        del prepare, build
        memory.close()


def main():
    """读取显式指定的独立 VA，输出 CPU 夹具证据，线程数必须由调用者提供。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--hardware-threads", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(audit_builder(args.image, args.width, args.height, args.hardware_threads), indent=2))


if __name__ == "__main__":
    main()
