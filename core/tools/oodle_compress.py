#!/usr/bin/env python3
import argparse
import ctypes
import struct
from pathlib import Path


COMPRESSORS = {
    "kraken": 8,
    "mermaid": 9,
    "selkie": 11,
    "hydra": 12,
    "leviathan": 13,
}


def fail(message):
    raise SystemExit("ERROR: " + message)


def load_function(dll, name):
    try:
        return getattr(dll, name)
    except AttributeError:
        fail("function not exported by Oodle DLL: %s" % name)


def main():
    parser = argparse.ArgumentParser(description="Compress one raw block with oo2core OodleLZ_Compress.")
    parser.add_argument("--dll", required=True, help="Path to oo2core_*_win64.dll")
    parser.add_argument("--compressor", required=True, choices=sorted(COMPRESSORS))
    parser.add_argument("--level", type=int, default=4)
    parser.add_argument("input")
    parser.add_argument("output")
    args = parser.parse_args()

    src = Path(args.input).read_bytes()
    dll = ctypes.WinDLL(args.dll)
    compress = load_function(dll, "OodleLZ_Compress")
    get_bound = getattr(dll, "OodleLZ_GetCompressedBufferSize", None)

    compress.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    compress.restype = ctypes.c_size_t

    if get_bound:
        get_bound.argtypes = [ctypes.c_size_t]
        get_bound.restype = ctypes.c_size_t
        bound = get_bound(len(src))
    else:
        bound = len(src) + 65536 + len(src) // 8

    src_buf = ctypes.create_string_buffer(src)
    dst_buf = ctypes.create_string_buffer(bound)
    written = compress(
        COMPRESSORS[args.compressor],
        src_buf,
        len(src),
        dst_buf,
        args.level,
        None,
        0,
        0,
        None,
        0,
    )
    if written <= 0 or written > bound:
        fail("OodleLZ_Compress failed")

    Path(args.output).write_bytes(struct.pack("<Q", len(src)) + dst_buf.raw[:written])


if __name__ == "__main__":
    main()
