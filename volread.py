#!/usr/bin/env python3
"""
volread.py - Minimal reader for Starsiege Tribes / Darkstar PVOL archives.

Mirrors engine/Core/code/volstrm.cpp VolumeRStream::openVolume:
  off 0 : DWORD "PVOL"
  off 4 : DWORD stringBlockOffset
  [data VBLK blocks ...]
  at stringBlockOffset : "vols" block = raw string table
  next                 : "voli" block = packed VolumeItem[] (17 bytes each)

VolumeItem (pack(1), 17 bytes):
  DWORD ID; INT32 stringOffset; DWORD blockOffset; UINT32 size; UINT8 compressType
A file's bytes live at blockOffset + 8 (skip the VBLK 8-byte block header),
length = size, when compressType == 0 (STRM_COMPRESS_NONE).

Seek-based: only the directory region is read up front, so indexing hundreds of
VOLs is cheap. File payloads are read on demand in read().
"""

import struct


def _fourcc(s):
    return struct.unpack("<I", s.encode("ascii"))[0]

PVOL = _fourcc("PVOL")
VOLS = _fourcc("vols")
VOLI = _fourcc("voli")

ITEM_SIZE = 17  # DWORD + int32 + DWORD + uint32 + uint8, packed


class VolEntry:
    __slots__ = ("name", "block_offset", "size", "compress")

    def __init__(self, name, block_offset, size, compress):
        self.name = name
        self.block_offset = block_offset
        self.size = size
        self.compress = compress


class Vol:
    def __init__(self, path):
        self.path = path
        self.entries = []          # list[VolEntry]
        self._by_name = {}         # lower basename -> VolEntry
        with open(path, "rb") as f:
            self._parse(f)

    def _parse(self, f):
        head = f.read(8)
        if len(head) < 8:
            raise ValueError(f"{self.path}: too small to be a VOL")
        ident, string_block_off = struct.unpack("<II", head)
        if ident != PVOL:
            raise ValueError(f"{self.path}: not a PVOL (magic={ident:#x})")

        # --- string table: a "vols" block at string_block_off ---
        f.seek(string_block_off)
        sid, ssize = struct.unpack("<II", f.read(8))
        if sid != VOLS:
            raise ValueError(f"{self.path}: expected 'vols' block, got {sid:#x}")
        string_table = f.read(ssize)

        # --- id dictionary: the "voli" block immediately after the strings ---
        iid, isize = struct.unpack("<II", f.read(8))
        if iid != VOLI:
            # some volumes word-align the string block; nudge forward a byte
            f.seek(-7, 1)
            iid, isize = struct.unpack("<II", f.read(8))
            if iid != VOLI:
                raise ValueError(f"{self.path}: expected 'voli' block, got {iid:#x}")
        items = f.read(isize)
        n = isize // ITEM_SIZE

        for i in range(n):
            _id, str_off, block_off, size, comp = struct.unpack_from(
                "<IiIIB", items, i * ITEM_SIZE)
            if str_off == -1:
                name = ""
            else:
                end = string_table.find(b"\x00", str_off)
                name = string_table[str_off:end].decode("latin1")
            e = VolEntry(name, block_off, size, comp)
            self.entries.append(e)
            if name:
                self._by_name[name.lower()] = e

    def names(self):
        return [e.name for e in self.entries if e.name]

    def has(self, name):
        return name.lower() in self._by_name

    def read(self, name):
        """Return the uncompressed bytes for an entry (compressType 0 only)."""
        e = self._by_name.get(name.lower())
        if e is None:
            return None
        if e.compress != 0:
            raise NotImplementedError(
                f"{name}: compressType {e.compress} (RLE/LZ/LZH) not supported")
        with open(self.path, "rb") as f:
            f.seek(e.block_offset + 8)  # skip VBLK 8-byte block header
            return f.read(e.size)


if __name__ == "__main__":
    import sys
    v = Vol(sys.argv[1])
    print(f"{len(v.entries)} entries")
    for e in v.entries:
        print(f"  {e.name:32s} off={e.block_offset:>10} size={e.size:>8} comp={e.compress}")
