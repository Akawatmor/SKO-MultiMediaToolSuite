#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib/6to2matrixencoder.py
===============================================================================
เครื่องมือเข้ารหัสสัญญาณเสียงมัลติแชนเนลเป็นสเตอริโอเมทริกซ์ Lt/Rt 2.0 (Matrix Encoder)
รองรับการนำเข้าเป็นโมดูล (Importable Module) และการรันเป็น CLI ผ่านคำสั่งโดยตรง

สมการอ้างอิงและมาตรฐานที่รองรับ:
-------------------------------------------------------------------------------
1. Dolby Surround (ds) [Dressler, AES 1993]:
   Lt = L - 0.70710678 * S*
   Rt = R + 0.70710678 * S*
   (ช่อง Surround S เป็นโมโน เลื่อนเฟส +90° เข้า Lt และ -90° เข้า Rt สัมพัทธ์กัน 180°)
   (Surround ผ่าน Band-pass Filter 100 Hz - 7000 Hz ตามมาตรฐานดั้งเดิม)

2. Dolby Pro Logic (pl) [Dolby Laboratories]:
   Lt = L + 0.70710678 * C - 0.70710678 * S*
   Rt = R + 0.70710678 * C + 0.70710678 * S*
   (Surround ผ่าน Band-pass Filter 100 Hz - 7000 Hz ตามมาตรฐานดั้งเดิม)

3. Dolby Pro Logic II (pl2) [Fosgate / Dolby Patents US 5,870,480, US 6,782,109]:
   Lt = L + 0.70710678 * C - 0.81649658 * Ls* - 0.57735027 * Rs*
   Rt = R + 0.70710678 * C + 0.57735027 * Ls* + 0.81649658 * Rs*
   (สัมประสิทธิ์ Major = sqrt(2/3) ≈ 0.8165 / -1.76 dB, Minor = sqrt(1/3) ≈ 0.5774 / -4.77 dB)
   (ช่อง Ls, Rs หมุนเฟส 90° ด้วย Type III FIR Hilbert Transformer และชดเชยดีเลย์ช่องหน้า)

4. PLII Simple / ATSC A/52 §7.8 Lt/Rt Downmix (pl2s):
   Lt = L + 0.70710678 * C - 0.70710678 * (Ls + Rs)
   Rt = R + 0.70710678 * C + 0.70710678 * (Ls + Rs)
   (เมทริกซ์พีชคณิตกลับเฟส 180° แท้จริง ไม่ผ่านการหมุนเฟส 90° ด้วย Hilbert)

หมายเหตุ:
- ช่อง L, R, C (และ LFE หากผสม) ถูกหน่วงเวลาชดเชย (Delay Compensation)
  เท่ากับ Group Delay ของ Hilbert FIR พอดี เพื่อรักษาความสัมพันธ์ของเฟสให้ถูกต้องสมบูรณ์
===============================================================================
"""

import argparse
import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Dict, List, Optional, Tuple, Union

# บังคับระบบ I/O ให้ใช้ UTF-8 เสมอ เพื่อรองรับ Windows console และชื่อไฟล์ภาษาไทย
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import numpy as np
    import scipy.signal as signal
except ImportError:
    print(
        "ข้อผิดพลาด: จำเป็นต้องติดตั้ง numpy และ scipy กรุณารัน: pip install numpy scipy",
        file=sys.stderr,
    )
    sys.exit(1)

# ==============================================================================
# 1. ค่าคงที่และสเปกมาตรฐาน (Constants & Standards)
# ==============================================================================
VERSION = "1.0.0"
SAMPLE_RATE_TARGET = 48000
INV_SQRT2 = np.float64(1.0 / math.sqrt(2.0))  # ≈ 0.7071067811865475 (-3.01 dB)
SQRT_2_3 = np.float64(math.sqrt(2.0 / 3.0))   # ≈ 0.816496580927726  (-1.76 dB)
SQRT_1_3 = np.float64(math.sqrt(1.0 / 3.0))   # ≈ 0.5773502691896257 (-4.77 dB)

# ค่าสัมประสิทธิ์แบบ Legacy จากไฟล์แนบเดิม
LEGACY_MAJOR = np.float64(10.0 ** (-1.2 / 20.0))  # ≈ 0.87096359 (-1.2 dB)
LEGACY_MINOR = np.float64(10.0 ** (-6.2 / 20.0))  # ≈ 0.48977882 (-6.2 dB)

HILBERT_TAPS_DEFAULT = 2047  # Type III FIR เลขคี่
HILBERT_LATENCY = (HILBERT_TAPS_DEFAULT - 1) // 2  # 1023 ตัวอย่าง

# ลำดับบทบาทช่องเสียงมาตรฐานภายใน
STANDARD_ROLES = ("L", "R", "C", "LFE", "Ls", "Rs", "S", "Lb", "Rb")

# WAVEFORMATEXTENSIBLE Channel Mask Constants
SPEAKER_FRONT_LEFT = 0x0001
SPEAKER_FRONT_RIGHT = 0x0002
SPEAKER_FRONT_CENTER = 0x0004
SPEAKER_LOW_FREQUENCY = 0x0008
SPEAKER_BACK_LEFT = 0x0010
SPEAKER_BACK_RIGHT = 0x0020
SPEAKER_FRONT_LEFT_OF_CENTER = 0x0040
SPEAKER_FRONT_RIGHT_OF_CENTER = 0x0080
SPEAKER_BACK_CENTER = 0x0100
SPEAKER_SIDE_LEFT = 0x0200
SPEAKER_SIDE_RIGHT = 0x0400

MASK_BIT_TO_NAME = {
    0x0001: "FL",
    0x0002: "FR",
    0x0004: "FC",
    0x0008: "LFE",
    0x0010: "BL",
    0x0020: "BR",
    0x0040: "FLC",
    0x0080: "FRC",
    0x0100: "BC",
    0x0200: "SL",
    0x0400: "SR",
}

# ตารางแมป Channel Count -> WAV Order เริ่มต้น เมื่อไม่มี Mask
FALLBACK_CHANNEL_ORDERS = {
    1: ["C"],
    2: ["L", "R"],
    3: ["L", "R", "C"],
    4: ["L", "R", "Ls", "Rs"],
    5: ["L", "R", "C", "Ls", "Rs"],
    6: ["L", "R", "C", "LFE", "Ls", "Rs"],
    8: ["L", "R", "C", "LFE", "Lb", "Rb", "Ls", "Rs"],
}


class MatrixMode(str, Enum):
    DS = "ds"
    PL = "pl"
    PL2 = "pl2"
    PL2S = "pl2s"


class CutoffType(str, Enum):
    HILL = "hill"
    BRICK = "brick"


class HeadroomMode(str, Enum):
    NONE = "none"
    STATIC = "static"
    PEAK = "peak"


@dataclass
class StreamAudioInfo:
    container: str
    codec: str
    sample_rate: int
    channels: int
    bit_depth: int
    channel_layout_str: Optional[str]
    channel_mask: Optional[int]
    total_frames: Optional[int]
    duration_sec: Optional[float]
    source_is_pipe: bool = False


# ==============================================================================
# 2. คลาสช่วยเหลือด้านข้อผิดพลาดและการค้นหาเครื่องมือ (Utilities)
# ==============================================================================
class MatrixEncoderError(Exception):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


def find_ffmpeg_tool(name: str, explicit_path: Optional[str] = None) -> Optional[str]:
    """ค้นหาพาธของ ffmpeg / ffprobe ตามลำดับความสำคัญ"""
    if explicit_path:
        p = Path(explicit_path).expanduser().resolve()
        if p.is_file():
            return str(p)
        candidate = p / (name + (".exe" if os.name == "nt" else ""))
        if candidate.is_file():
            return str(candidate)

    env_val = os.environ.get("FFMPEG_BIN")
    if env_val:
        p = Path(env_val).expanduser().resolve()
        if p.is_file():
            return str(p)
        candidate = p / (name + (".exe" if os.name == "nt" else ""))
        if candidate.is_file():
            return str(candidate)

    # ค้นหาในโฟลเดอร์โปรเจกต์
    cur_dir = Path(__file__).resolve().parent
    for sub in [cur_dir, cur_dir.parent, cur_dir.parent / "tools", cur_dir.parent / "bin"]:
        candidate = sub / (name + (".exe" if os.name == "nt" else ""))
        if candidate.is_file():
            return str(candidate)

    # ค้นหาใน System PATH
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt" and not name.lower().endswith(".exe"):
        found = shutil.which(name + ".exe")
        if found:
            return found
    return None


# ==============================================================================
# 3. I/O Layer: การอ่านไฟล์ WAV และท่อ FFmpeg (WAV & Pipe Streaming)
# ==============================================================================
class WavStreamReader:
    """อ่านข้อมูลไฟล์ WAV/RF64/BW64 แบบสตรีมทีละบล็อก พร้อมแกะ Chunk ละเอียด"""

    def __init__(self, f: BinaryIO):
        self.f = f
        self.info = self._parse_header()
        self.frame_bytes = self.info.channels * (self.info.bit_depth // 8)
        self.remaining_bytes = (
            self.info.total_frames * self.frame_bytes
            if self.info.total_frames is not None
            else None
        )

    def _read_exact(self, n: int) -> bytes:
        chunks = []
        got = 0
        while got < n:
            b = self.f.read(n - got)
            if not b:
                break
            chunks.append(b)
            got += len(b)
        return b"".join(chunks)

    def _parse_header(self) -> StreamAudioInfo:
        head = self._read_exact(12)
        if len(head) < 12:
            raise MatrixEncoderError("ไฟล์มีขนาดสั้นเกินกว่าจะเป็นไฟล์ WAV", 3)
        riff_tag = head[0:4]
        if riff_tag not in (b"RIFF", b"RF64", b"BW64"):
            raise MatrixEncoderError(f"ไม่ใช่ไฟล์ WAV/RF64 ที่ถูกต้อง (พบ Header: {riff_tag!r})", 3)
        if head[8:12] != b"WAVE":
            raise MatrixEncoderError(f"ไม่ใช่ฟอร์แมต WAVE (พบ: {head[8:12]!r})", 3)

        fmt_chunk: Optional[bytes] = None
        ds64_data_size: Optional[int] = None
        data_chunk_size: Optional[int] = None

        while True:
            chunk_hdr = self._read_exact(8)
            if len(chunk_hdr) < 8:
                break
            cid, csize = struct.unpack("<4sI", chunk_hdr)

            if cid == b"ds64":
                ds_body = self._read_exact(csize + (csize & 1))
                if len(ds_body) >= 16:
                    ds64_data_size = struct.unpack("<Q", ds_body[8:16])[0]
            elif cid == b"fmt ":
                fmt_chunk = self._read_exact(csize + (csize & 1))
            elif cid == b"data":
                data_chunk_size = (
                    ds64_data_size
                    if (csize in (0, 0xFFFFFFFF) and ds64_data_size is not None)
                    else csize
                )
                break
            else:
                # ข้าม Chunk อื่น ๆ (LIST, bext, PEAK, fact, JUNK, etc.)
                pad = csize & 1
                to_skip = csize + pad
                try:
                    self.f.seek(to_skip, io.SEEK_CUR)
                except Exception:
                    self._read_exact(to_skip)

        if not fmt_chunk or len(fmt_chunk) < 16:
            raise MatrixEncoderError("ไม่พบ fmt chunk ที่สมบูรณ์ในไฟล์ WAV", 3)
        if data_chunk_size is None:
            raise MatrixEncoderError("ไม่พบ data chunk ในไฟล์ WAV", 3)

        wFormatTag, nChannels, nSamplesPerSec, _, _, wBitsPerSample = struct.unpack(
            "<HHIIHH", fmt_chunk[:16]
        )
        channel_mask = None

        if wFormatTag == 0xFFFE and len(fmt_chunk) >= 40:
            channel_mask = struct.unpack("<I", fmt_chunk[20:24])[0]
            sub_tag = struct.unpack("<H", fmt_chunk[24:26])[0]
            wFormatTag = sub_tag

        frame_bytes = nChannels * (wBitsPerSample // 8)
        total_frames = data_chunk_size // frame_bytes if frame_bytes > 0 else None
        duration = total_frames / nSamplesPerSec if total_frames and nSamplesPerSec else None

        codec_desc = "pcm_float" if wFormatTag == 3 else f"pcm_s{wBitsPerSample}le"
        return StreamAudioInfo(
            container=riff_tag.decode("ascii", errors="replace"),
            codec=codec_desc,
            sample_rate=nSamplesPerSec,
            channels=nChannels,
            bit_depth=wBitsPerSample,
            channel_layout_str=None,
            channel_mask=channel_mask,
            total_frames=total_frames,
            duration_sec=duration,
        )

    def read_frames(self, num_frames: int) -> np.ndarray:
        want_bytes = num_frames * self.frame_bytes
        if self.remaining_bytes is not None:
            want_bytes = min(want_bytes, self.remaining_bytes)

        raw = self._read_exact(want_bytes) if want_bytes > 0 else b""
        if self.remaining_bytes is not None:
            self.remaining_bytes -= len(raw)

        usable_len = (len(raw) // self.frame_bytes) * self.frame_bytes
        raw = raw[:usable_len]
        if not raw:
            return np.zeros((0, self.info.channels), dtype=np.float32)

        # แปลงข้อมูลตามชนิดบิต
        tag = 3 if "float" in self.info.codec else 1
        bits = self.info.bit_depth

        if tag == 3:
            arr = np.frombuffer(raw, dtype="<f4" if bits == 32 else "<f8").astype(np.float32)
        elif bits == 16:
            arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif bits == 24:
            b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            arr = (((v << 8) >> 8).astype(np.float32)) / 8388608.0
        elif bits == 32:
            arr = (np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0).astype(
                np.float32
            )
        elif bits == 8:
            arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        else:
            raise MatrixEncoderError(f"ไม่รองรับบิตเรตขนาด {bits} บิต", 3)

        return arr.reshape(-1, self.info.channels)

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


class FFmpegStreamReader:
    """อ่านสัญญาณเสียงที่ decode ผ่าน FFmpeg pipe เป็น float32 48kHz พร้อมป้องกัน deadlock บน stderr"""

    def __init__(self, proc: subprocess.Popen, info: StreamAudioInfo):
        self.proc = proc
        self.info = info
        self.frame_bytes = info.channels * 4
        self.stderr_lines: List[str] = []

        # Thread อ่าน stderr ป้องกัน buffer เต็มจนเกิด deadlock
        self.stderr_thread = threading.Thread(target=self._capture_stderr, daemon=True)
        self.stderr_thread.start()

    def _capture_stderr(self):
        if not self.proc.stderr:
            return
        try:
            for line in iter(self.proc.stderr.readline, b""):
                if line:
                    decoded = line.decode("utf-8", errors="replace").strip()
                    self.stderr_lines.append(decoded)
        except Exception:
            pass

    def read_frames(self, num_frames: int) -> np.ndarray:
        want_bytes = num_frames * self.frame_bytes
        chunks = []
        got = 0
        while got < want_bytes:
            b = self.proc.stdout.read(want_bytes - got)
            if not b:
                break
            chunks.append(b)
            got += len(b)

        raw = b"".join(chunks)
        usable_len = (len(raw) // self.frame_bytes) * self.frame_bytes
        raw = raw[:usable_len]
        if not raw:
            return np.zeros((0, self.info.channels), dtype=np.float32)

        arr = np.frombuffer(raw, dtype="<f4").astype(np.float32)
        return arr.reshape(-1, self.info.channels)

    def close(self):
        try:
            if self.proc.stdout:
                self.proc.stdout.close()
        except Exception:
            pass
        rc = self.proc.wait()
        if rc != 0 and rc != -15:  # -15 คือ SIGTERM
            err_summary = "\n".join(self.stderr_lines[-10:])
            raise MatrixEncoderError(
                f"FFmpeg ทำงานล้มเหลว (Exit code {rc}):\n{err_summary}", 4
            )


class WavWriterF32:
    """เขียน Float32 Stereo WAV (Lt/Rt) โดยอัตโนมัติเป็น RF64 หากขนาดไฟล์เกิน 4 GB"""

    def __init__(
        self,
        final_path: Path,
        sample_rate: int = SAMPLE_RATE_TARGET,
        channels: int = 2,
    ):
        self.final_path = final_path
        self.part_path = final_path.parent / (final_path.name + ".part")
        self.sample_rate = sample_rate
        self.channels = channels
        self.total_bytes_written = 0

        self.f = open(self.part_path, "wb")
        # เขียนจองพื้นที่ Header เบื้องต้น 128 ไบต์ (เผื่อ ds64 และ fmt extensible)
        self.header_reserve_size = 128
        self.f.write(b"\0" * self.header_reserve_size)

    def write_frames(self, block: np.ndarray):
        if block.size == 0:
            return
        b = np.ascontiguousarray(block, dtype="<f4").tobytes()
        self.f.write(b)
        self.total_bytes_written += len(b)

    def close(self, abort: bool = False):
        try:
            self.f.flush()
        except Exception:
            pass

        if abort:
            self.f.close()
            if self.part_path.exists():
                try:
                    self.part_path.unlink()
                except Exception:
                    pass
            return

        data_size = self.total_bytes_written
        is_rf64 = data_size > (0xFFFFFFFF - 512)

        self.f.seek(0)
        if not is_rf64:
            # มาตรฐาน WAVEFORMATEXTENSIBLE ปกติ (ขนาด < 4 GB)
            cb_size = 22
            fmt_size = 40
            junk_size = self.header_reserve_size - 12 - (8 + fmt_size) - 8 - 8  # 52 bytes
            riff_size = 4 + (8 + fmt_size) + (8 + junk_size) + (8 + data_size)

            hdr = io.BytesIO()
            hdr.write(b"RIFF")
            hdr.write(struct.pack("<I", riff_size))
            hdr.write(b"WAVE")

            # fmt chunk
            hdr.write(b"fmt ")
            hdr.write(struct.pack("<I", fmt_size))
            hdr.write(
                struct.pack(
                    "<HHIIHH",
                    0xFFFE,
                    self.channels,
                    self.sample_rate,
                    self.sample_rate * self.channels * 4,
                    self.channels * 4,
                    32,
                )
            )
            hdr.write(struct.pack("<H", cb_size))
            hdr.write(struct.pack("<H", 32))  # wValidBitsPerSample
            hdr.write(struct.pack("<I", 0x0003))  # dwChannelMask: FL | FR
            hdr.write(
                b"\x03\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
            )

            # JUNK chunk วางคั่นก่อน data chunk
            hdr.write(b"JUNK")
            hdr.write(struct.pack("<I", junk_size))
            hdr.write(b"\0" * junk_size)

            # data chunk header (ต้องอยู่ท้ายสุดก่อน raw audio พอดี)
            hdr.write(b"data")
            hdr.write(struct.pack("<I", data_size))

            header_bytes = hdr.getvalue()
            self.f.write(header_bytes)
        else:
            # มาตรฐาน RF64 สำหรับไฟล์ขนาดเกิน 4 GB
            fmt_size = 40
            ds64_size = 28
            junk_size_rf64 = self.header_reserve_size - 12 - (8 + ds64_size) - (8 + fmt_size) - 8 - 8  # 16 bytes
            riff_size_64 = 4 + (8 + ds64_size) + (8 + fmt_size) + (8 + junk_size_rf64) + (8 + data_size)
            sample_count_64 = data_size // (self.channels * 4)

            hdr = io.BytesIO()
            hdr.write(b"RF64")
            hdr.write(struct.pack("<I", 0xFFFFFFFF))
            hdr.write(b"WAVE")

            # ds64 chunk (28 bytes payload)
            hdr.write(b"ds64")
            hdr.write(struct.pack("<I", 28))
            hdr.write(struct.pack("<QQQI", riff_size_64, data_size, sample_count_64, 0))

            # fmt chunk
            hdr.write(b"fmt ")
            hdr.write(struct.pack("<I", fmt_size))
            hdr.write(
                struct.pack(
                    "<HHIIHH",
                    0xFFFE,
                    self.channels,
                    self.sample_rate,
                    self.sample_rate * self.channels * 4,
                    self.channels * 4,
                    32,
                )
            )
            hdr.write(struct.pack("<H", 22))
            hdr.write(struct.pack("<H", 32))
            hdr.write(struct.pack("<I", 0x0003))
            hdr.write(
                b"\x03\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
            )

            # JUNK chunk วางคั่นก่อน data chunk
            hdr.write(b"JUNK")
            hdr.write(struct.pack("<I", junk_size_rf64))
            hdr.write(b"\0" * junk_size_rf64)

            # data chunk header
            hdr.write(b"data")
            hdr.write(struct.pack("<I", 0xFFFFFFFF))

            header_bytes = hdr.getvalue()
            self.f.write(header_bytes)

        self.f.close()
        # Atomic rename จาก .part เป็นไฟล์เป้าหมายจริง
        if self.final_path.exists():
            self.final_path.unlink()
        self.part_path.rename(self.final_path)


# ==============================================================================
# 4. DSP Engine: ตัวกรองและดีเลย์ (IIR SOS, Kaiser FIR, Hilbert Type III, Delay Line)
# ==============================================================================
class BiquadSOSFilter:
    """IIR Butterworth Filter ออกแบบผ่าน Bilinear Transform + Pre-warping ที่ 48kHz (State float64)"""

    def __init__(
        self,
        btype: str,
        cutoff_hz: Union[float, Tuple[float, float]],
        slope_db: int = 12,
        fs: int = SAMPLE_RATE_TARGET,
    ):
        self.fs = fs
        self.btype = btype
        self.slope_db = slope_db

        # จำนวน Pole = slope / 6
        poles = max(1, slope_db // 6)
        if btype == "bandpass":
            # สำหรับ bandpass order N ของ scipy จะให้ความชัน 6*N ต่อข้าง
            order = max(1, poles // 2)
            self.sos = signal.butter(
                N=order, Wn=cutoff_hz, btype="bandpass", output="sos", fs=fs
            )
        else:
            self.sos = signal.butter(
                N=poles, Wn=cutoff_hz, btype=btype, output="sos", fs=fs
            )

        # เก็บ state zi สำหรับ streaming
        self.zi = signal.sosfilt_zi(self.sos).astype(np.float64)

    def process_block(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        # คำนวณด้วย float64 เพื่อเสถียรภาพตัวเลข แล้วแปลงกลับ float32
        x64 = x.astype(np.float64)
        filtered, self.zi = signal.sosfilt(self.sos, x64, axis=0, zi=self.zi)
        return filtered.astype(np.float32)


class KaiserFIRFilter:
    """FIR Linear-Phase Filter ออกแบบด้วย Kaiser Window รักษา State ข้ามบล็อก"""

    def __init__(
        self,
        cutoff_hz: Union[float, Tuple[float, float]],
        btype: str,
        taps: int = 549,
        beta: float = 8.6,
        fs: int = SAMPLE_RATE_TARGET,
    ):
        if taps % 2 == 0:
            taps += 1  # บังคับเป็นเลขคี่ (Type I)
        self.taps = taps
        self.latency_samples = (taps - 1) // 2

        pass_zero = True if btype == "lowpass" else False
        if btype == "bandpass":
            pass_zero = False

        self.b = signal.firwin(
            numtaps=taps,
            cutoff=cutoff_hz,
            window=("kaiser", beta),
            pass_zero=pass_zero,
            fs=fs,
        ).astype(np.float64)
        self.zi = signal.lfilter_zi(self.b, 1.0).astype(np.float64) * 0.0

    def process_block(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        x64 = x.astype(np.float64)
        filtered, self.zi = signal.lfilter(self.b, 1.0, x64, axis=0, zi=self.zi)
        return filtered.astype(np.float32)

    def flush_tail(self) -> np.ndarray:
        zero_tail = np.zeros(self.latency_samples, dtype=np.float64)
        filtered, self.zi = signal.lfilter(self.b, 1.0, zero_tail, axis=0, zi=self.zi)
        return filtered.astype(np.float32)


class HilbertTransformerFIR:
    """Type III FIR Hilbert Transformer (ความยาวเลขคี่) เลื่อนเฟส +90° ย่าน 100-20000 Hz"""

    def __init__(self, taps: int = HILBERT_TAPS_DEFAULT, beta: float = 8.0):
        if taps % 2 == 0:
            taps += 1
        self.taps = taps
        self.mid = (taps - 1) // 2
        self.latency_samples = self.mid

        n = np.arange(-self.mid, self.mid + 1)
        h = np.zeros(taps, dtype=np.float64)
        odd = (n % 2) != 0
        w = signal.windows.kaiser(taps, beta)
        h[odd] = (2.0 / (np.pi * n[odd])) * w[odd]
        self.h = h

        self.zi = signal.lfilter_zi(self.h, 1.0).astype(np.float64) * 0.0

    def process_block(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        x64 = x.astype(np.float64)
        filtered, self.zi = signal.lfilter(self.h, 1.0, x64, axis=0, zi=self.zi)
        return filtered.astype(np.float32)

    def flush_tail(self) -> np.ndarray:
        zero_tail = np.zeros(self.latency_samples, dtype=np.float64)
        filtered, self.zi = signal.lfilter(self.h, 1.0, zero_tail, axis=0, zi=self.zi)
        return filtered.astype(np.float32)


class DelayLine:
    """Ring Buffer ชดเชยความหน่วง (Delay Line) เพื่อจัด Latency ทุกช่องให้ตรงกันเป๊ะ"""

    def __init__(self, delay_samples: int):
        self.delay_samples = delay_samples
        self.buffer = np.zeros(delay_samples, dtype=np.float32)

    def process_block(self, x: np.ndarray) -> np.ndarray:
        if self.delay_samples == 0:
            return x
        if x.size == 0:
            return x

        combined = np.concatenate([self.buffer, x])
        out = combined[: len(x)]
        self.buffer = combined[len(x) :]
        return out

    def flush_tail(self) -> np.ndarray:
        if self.delay_samples == 0:
            return np.zeros(0, dtype=np.float32)
        out = self.buffer.copy()
        self.buffer.fill(0)
        return out


# ==============================================================================
# 5. Routing, Channel Mapping & Compatibility
# ==============================================================================
class ChannelMapper:
    """แมป channel mask หรือ layout string ของอินพุตเข้าสู่ Roles ภายใน"""

    def __init__(
        self,
        info: StreamAudioInfo,
        layout_override: Optional[str] = None,
    ):
        self.info = info
        self.override = layout_override
        self.warnings: List[str] = []
        self.dropped_channels: List[Tuple[int, str]] = []
        self.assigned_roles: List[Optional[str]] = [None] * info.channels

        self._resolve_mapping()

    def _resolve_mapping(self):
        nch = self.info.channels

        if self.override and self.override.lower() != "auto":
            tokens = [t.strip().upper() for t in self.override.split(",")]
            if len(tokens) != nch:
                raise MatrixEncoderError(
                    f"--layout ระบุ {len(tokens)} ช่อง แต่ไฟล์อินพุตมี {nch} ช่อง", 2
                )
            for idx, r in enumerate(tokens):
                if r in ("L", "R", "C", "LFE", "LS", "RS", "S", "LB", "RB"):
                    # แปลงตัวพิมพ์มาตรฐาน
                    norm = {
                        "LS": "Ls",
                        "RS": "Rs",
                        "LB": "Lb",
                        "RB": "Rb",
                    }.get(r, r)
                    self.assigned_roles[idx] = norm
                elif r in ("NONE", "DROP", "X"):
                    self.dropped_channels.append((idx, r))
                else:
                    self.dropped_channels.append((idx, r))
                    self.warnings.append(f"ตัดช่องที่ไม่รู้จักที่ระบุใน --layout: '{r}' (ช่องที่ {idx})")
            return

        # ตรวจสอบผ่าน Channel Mask
        mask = self.info.channel_mask
        if mask and mask > 0:
            current_bit = 1
            idx = 0
            while current_bit <= 0x40000 and idx < nch:
                if mask & current_bit:
                    role_str = MASK_BIT_TO_NAME.get(current_bit, f"UNKNOWN_{current_bit:X}")
                    if role_str == "FL":
                        self.assigned_roles[idx] = "L"
                    elif role_str == "FR":
                        self.assigned_roles[idx] = "R"
                    elif role_str == "FC":
                        self.assigned_roles[idx] = "C"
                    elif role_str == "LFE":
                        self.assigned_roles[idx] = "LFE"
                    elif role_str in ("BL", "SL"):
                        # ถ้าเป็น 7.1 (มีทั้ง BL และ SL) BL จะเป็น Lb, SL จะเป็น Ls
                        if (mask & 0x0030) and (mask & 0x0600):
                            self.assigned_roles[idx] = "Lb" if role_str == "BL" else "Ls"
                        else:
                            self.assigned_roles[idx] = "Ls"
                    elif role_str in ("BR", "SR"):
                        if (mask & 0x0030) and (mask & 0x0600):
                            self.assigned_roles[idx] = "Rb" if role_str == "BR" else "Rs"
                        else:
                            self.assigned_roles[idx] = "Rs"
                    elif role_str == "BC":
                        self.assigned_roles[idx] = "S"
                    elif role_str in ("FLC", "FRC"):
                        self.assigned_roles[idx] = role_str
                    else:
                        self.dropped_channels.append((idx, role_str))
                        self.warnings.append(
                            f"ตรวจพบช่องสัญญาณพิเศษ '{role_str}' (ช่องที่ {idx}) ซึ่งเกินมาตรฐาน จึงตัดทิ้ง"
                        )
                    idx += 1
                current_bit <<= 1
            return

        # ตรวจสอบผ่าน Layout String ของ FFmpeg (ถ้ามี)
        layout_str = (self.info.channel_layout_str or "").lower()
        if layout_str:
            layout_map = {
                "mono": ["C"],
                "stereo": ["L", "R"],
                "3.0": ["L", "R", "C"],
                "3.0(back)": ["L", "R", "S"],
                "quad": ["L", "R", "Ls", "Rs"],
                "quad(side)": ["L", "R", "Ls", "Rs"],
                "4.0": ["L", "R", "C", "S"],
                "5.0": ["L", "R", "C", "Ls", "Rs"],
                "5.0(side)": ["L", "R", "C", "Ls", "Rs"],
                "5.1": ["L", "R", "C", "LFE", "Ls", "Rs"],
                "5.1(side)": ["L", "R", "C", "LFE", "Ls", "Rs"],
                "6.1": ["L", "R", "C", "LFE", "S", "Ls", "Rs"],
                "6.1(back)": ["L", "R", "C", "LFE", "S", "Ls", "Rs"],
                "7.1": ["L", "R", "C", "LFE", "Lb", "Rb", "Ls", "Rs"],
                "7.1(wide)": ["L", "R", "C", "LFE", "Ls", "Rs", "FLC", "FRC"],
            }
            if layout_str in layout_map and len(layout_map[layout_str]) == nch:
                self.assigned_roles = list(layout_map[layout_str])
                return

        # Fallback เมื่อไม่มีทั้ง Mask และ Layout string
        if nch in FALLBACK_CHANNEL_ORDERS:
            self.assigned_roles = list(FALLBACK_CHANNEL_ORDERS[nch])
            if nch == 4:
                self.warnings.append(
                    "ไฟล์ 4 ช่องไม่มี Channel Mask -> สมมติเลย์เอาต์ L, R, Ls, Rs (Quad) "
                    "หากเป็น L, R, C, S ให้ใช้ --layout 'L,R,C,S'"
                )
            elif nch == 8:
                self.warnings.append(
                    "ไฟล์ 8 ช่องไม่มี Channel Mask -> สมมติเลย์เอาต์ 7.1 WAV (L, R, C, LFE, Lb, Rb, Ls, Rs)"
                )
            else:
                self.warnings.append(
                    f"ไฟล์ไม่มี Channel Mask -> สมมติเลย์เอาต์ตามจำนวนช่อง: {', '.join(self.assigned_roles)}"
                )
            return

        raise MatrixEncoderError(
            f"ไม่สามารถระบุเลย์เอาต์ของไฟล์ {nch} ช่องได้โดยอัตโนมัติ กรุณาระบุเลย์เอาต์ด้วย --layout",
            3,
        )


# ==============================================================================
# 6. Matrix & Routing Engine (Fold-back, Compatibility & Coefficients)
# ==============================================================================
class MatrixEngine:
    """ประมวลผลการคำนวณ Matrix และจัดการความเข้ากันได้ของ Role สู่ Lt/Rt"""

    def __init__(
        self,
        mode: MatrixMode,
        raw_roles: List[Optional[str]],
        back_gain_db: float = -3.0103,
        lfe_enabled: bool = False,
        lfe_gain_db: float = 0.0,
        trim_dict: Optional[Dict[str, float]] = None,
        pl2_preset: str = "standard",
        out_gain_db: float = 0.0,
        headroom_mode: HeadroomMode = HeadroomMode.NONE,
    ):
        self.mode = mode
        self.raw_roles = raw_roles
        self.back_gain = float(10.0 ** (back_gain_db / 20.0))
        self.lfe_enabled = lfe_enabled
        self.lfe_gain = float(10.0 ** (lfe_gain_db / 20.0))
        self.trim_dict = trim_dict or {}
        self.pl2_preset = pl2_preset
        self.out_gain = float(10.0 ** (out_gain_db / 20.0))
        self.headroom_mode = headroom_mode

        self.rules_applied: List[str] = []
        self.warnings: List[str] = []

        self._validate_and_build_routing()

    def _validate_and_build_routing(self):
        present_roles = [r for r in self.raw_roles if r]

        if len(present_roles) == 1 and present_roles[0] == "C":
            raise MatrixEncoderError(
                "ไฟล์เสียงอินพุตเป็นโมโน (1 ช่อง) ไม่สามารถทำ Matrix Downmix เป็น 2.0 Lt/Rt ได้",
                3,
            )

        # ตรวจสอบ 2.0 bypass
        if len(present_roles) == 2 and set(present_roles) == {"L", "R"}:
            self.warnings.append(
                "ไฟล์อินพุตเป็นสเตอริโอ 2.0 ไม่มีข้อมูลเซอร์ราวด์ ระบบจะบายพาส Lt=L, Rt=R"
            )

        # ตรวจสอบ LFE
        if "LFE" in present_roles:
            if not self.lfe_enabled:
                self.rules_applied.append(
                    "ตรวจพบช่อง LFE แต่ถูกตัดทิ้ง (ค่าเริ่มต้นไม่ผสม LFE เพื่อป้องกันเบสบวม)"
                )
            else:
                self.rules_applied.append(
                    f"ผสมช่อง LFE เข้าสู่ Lt และ Rt ด้วยเกน {20*math.log10(self.lfe_gain):+.1f} dB หลังกรอง Low-pass"
                )
        elif self.lfe_enabled:
            self.warnings.append("เปิดตัวเลือก --lfe แต่อินพุตไม่มีช่อง LFE ระบบจะเพิกเฉย")

        # ตรวจสอบ 7.1 Fold-back
        if "Lb" in present_roles or "Rb" in present_roles:
            self.rules_applied.append(
                f"7.1 Fold-back: พับช่อง Lb/Rb เข้าสู่ Ls/Rs ด้วยเกน {20*math.log10(self.back_gain):+.2f} dB"
            )

        # ตรวจสอบ FLC/FRC
        if "FLC" in present_roles or "FRC" in present_roles:
            self.rules_applied.append("พับช่อง Front Center Left/Right เข้าสู่ L/R ที่ -3.01 dB")

        # ตรวจสอบ Compatibility ของ Surround ตามโหมด
        has_surround = any(r in present_roles for r in ("Ls", "Rs", "S", "Lb", "Rb"))
        if not has_surround:
            self.warnings.append("อินพุตไม่มีช่องเซอร์ราวด์ (Surround) สัญญาณเอาต์พุตจะไม่มีมิติหลัง")

        if self.mode in (MatrixMode.DS, MatrixMode.PL):
            if ("Ls" in present_roles or "Rs" in present_roles) and "S" not in present_roles:
                self.rules_applied.append(
                    "Compatibility: รวม Ls + Rs เป็นโมโนเซอร์ราวด์ S ที่เกน -3.01 dB สำหรับโหมด "
                    + self.mode.value
                )
            if self.mode == MatrixMode.DS and "C" in present_roles:
                self.rules_applied.append(
                    "Compatibility: พับช่อง Center เข้าสู่ L และ R ที่ -3.01 dB (โหมด ds ไม่มี Center)"
                )
        else:  # pl2, pl2s
            if "S" in present_roles and not ("Ls" in present_roles or "Rs" in present_roles):
                self.rules_applied.append(
                    "Compatibility: แยกโมโนเซอร์ราวด์ S ออกเป็น Ls และ Rs ที่เกน -3.01 dB สำหรับ "
                    + self.mode.value
                )

    def get_role_gain(self, role: str) -> float:
        """ดึงค่าตัวคูณ Trim รายช่อง"""
        if role in self.trim_dict:
            val = self.trim_dict[role]
            if val == -float("inf"):
                return 0.0
            return float(10.0 ** (val / 20.0))
        return 1.0

    def compute_static_headroom_scale(self) -> float:
        """คำนวณตัวคูณ Headroom แบบ worst-case เพื่อป้องกัน Peak Clip"""
        if self.headroom_mode != HeadroomMode.STATIC:
            return 1.0

        # คำนวณผลรวมสัมประสิทธิ์สูงสุดในกรณีแย่ที่สุด
        if self.mode == MatrixMode.DS:
            base_sum = 1.0 + float(INV_SQRT2)
        elif self.mode == MatrixMode.PL:
            base_sum = 1.0 + float(INV_SQRT2) + float(INV_SQRT2)
        elif self.mode == MatrixMode.PL2:
            maj = float(LEGACY_MAJOR if self.pl2_preset == "legacy" else SQRT_2_3)
            minor = float(LEGACY_MINOR if self.pl2_preset == "legacy" else SQRT_1_3)
            base_sum = 1.0 + float(INV_SQRT2) + maj + minor
        else:  # pl2s
            base_sum = 1.0 + float(INV_SQRT2) + float(INV_SQRT2) + float(INV_SQRT2)

        if self.lfe_enabled and "LFE" in self.raw_roles:
            base_sum += self.lfe_gain

        return 1.0 / base_sum if base_sum > 0 else 1.0

    def get_matrix_table(self) -> List[Dict[str, Union[str, float]]]:
        """ส่งออกตารางสัมประสิทธิ์สำหรับ Dry-run"""
        head_scale = self.compute_static_headroom_scale() * self.out_gain
        table = []

        maj = float(LEGACY_MAJOR if self.pl2_preset == "legacy" else SQRT_2_3)
        minor = float(LEGACY_MINOR if self.pl2_preset == "legacy" else SQRT_1_3)

        for idx, role in enumerate(self.raw_roles):
            if not role:
                table.append(
                    {
                        "index": idx,
                        "role": "DROPPED",
                        "lt_coef": 0.0,
                        "lt_db": -999.0,
                        "rt_coef": 0.0,
                        "rt_db": -999.0,
                    }
                )
                continue

            trim = self.get_role_gain(role)
            lt_c = 0.0
            rt_c = 0.0

            if role == "L":
                lt_c = 1.0 * trim
            elif role == "R":
                rt_c = 1.0 * trim
            elif role == "C":
                lt_c = float(INV_SQRT2) * trim
                rt_c = float(INV_SQRT2) * trim
            elif role == "LFE":
                if self.lfe_enabled:
                    lt_c = self.lfe_gain * trim
                    rt_c = self.lfe_gain * trim
            elif role == "S":
                lt_c = -float(INV_SQRT2) * trim
                rt_c = float(INV_SQRT2) * trim
            elif role == "Ls":
                if self.mode in (MatrixMode.DS, MatrixMode.PL):
                    # Ls รวมเข้า S ที่ -3dB
                    lt_c = -float(INV_SQRT2 * INV_SQRT2) * trim
                    rt_c = float(INV_SQRT2 * INV_SQRT2) * trim
                elif self.mode == MatrixMode.PL2:
                    lt_c = -maj * trim
                    rt_c = minor * trim
                else:  # pl2s
                    lt_c = -float(INV_SQRT2) * trim
                    rt_c = float(INV_SQRT2) * trim
            elif role == "Rs":
                if self.mode in (MatrixMode.DS, MatrixMode.PL):
                    lt_c = -float(INV_SQRT2 * INV_SQRT2) * trim
                    rt_c = float(INV_SQRT2 * INV_SQRT2) * trim
                elif self.mode == MatrixMode.PL2:
                    lt_c = -minor * trim
                    rt_c = maj * trim
                else:  # pl2s
                    lt_c = -float(INV_SQRT2) * trim
                    rt_c = float(INV_SQRT2) * trim
            elif role == "Lb":
                # พับเข้า Ls
                back_mult = self.back_gain * trim
                if self.mode in (MatrixMode.DS, MatrixMode.PL):
                    lt_c = -float(INV_SQRT2 * INV_SQRT2) * back_mult
                    rt_c = float(INV_SQRT2 * INV_SQRT2) * back_mult
                elif self.mode == MatrixMode.PL2:
                    lt_c = -maj * back_mult
                    rt_c = minor * back_mult
                else:
                    lt_c = -float(INV_SQRT2) * back_mult
                    rt_c = float(INV_SQRT2) * back_mult
            elif role == "Rb":
                back_mult = self.back_gain * trim
                if self.mode in (MatrixMode.DS, MatrixMode.PL):
                    lt_c = -float(INV_SQRT2 * INV_SQRT2) * back_mult
                    rt_c = float(INV_SQRT2 * INV_SQRT2) * back_mult
                elif self.mode == MatrixMode.PL2:
                    lt_c = -minor * back_mult
                    rt_c = maj * back_mult
                else:
                    lt_c = -float(INV_SQRT2) * back_mult
                    rt_c = float(INV_SQRT2) * back_mult
            elif role == "FLC":
                lt_c = float(INV_SQRT2) * trim
            elif role == "FRC":
                rt_c = float(INV_SQRT2) * trim

            final_lt = lt_c * head_scale
            final_rt = rt_c * head_scale
            lt_db = 20 * math.log10(abs(final_lt)) if abs(final_lt) > 1e-7 else -999.0
            rt_db = 20 * math.log10(abs(final_rt)) if abs(final_rt) > 1e-7 else -999.0

            table.append(
                {
                    "index": idx,
                    "role": role,
                    "lt_coef": final_lt,
                    "lt_db": lt_db,
                    "rt_coef": final_rt,
                    "rt_db": rt_db,
                }
            )

        return table


# ==============================================================================
# 7. Pipeline Orchestrator (Block Streaming & Normalization)
# ==============================================================================
class EncoderPipeline:
    """ตัวควบคุม Pipeline การประมวลผลตั้งแต่ต้นจนจบแบบ Block-by-block"""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.input_path = Path(args.input).expanduser().resolve()
        self.output_path = (
            Path(args.output).expanduser().resolve()
            if args.output
            else self.input_path.parent / (self.input_path.name + ".ltrt.wav")
        )
        self.ffmpeg_bin = find_ffmpeg_tool("ffmpeg", args.ffmpeg)
        self.ffprobe_bin = find_ffmpeg_tool("ffprobe", args.ffmpeg)

        self._validate_paths()
        self.info = self._probe_audio_source()
        self.mapper = ChannelMapper(self.info, args.layout)
        self.engine = MatrixEngine(
            mode=MatrixMode(args.mode),
            raw_roles=self.mapper.assigned_roles,
            back_gain_db=args.back_gain,
            lfe_enabled=args.lfe,
            lfe_gain_db=args.lfe_gain,
            trim_dict=self._parse_trims(args.trim),
            pl2_preset=args.pl2_preset,
            out_gain_db=args.out_gain,
            headroom_mode=HeadroomMode(args.headroom),
        )

        self._setup_dsp()

    def _validate_paths(self):
        if not self.input_path.exists():
            raise MatrixEncoderError(f"ไม่พบไฟล์อินพุต: {self.input_path}", 1)
        if self.input_path == self.output_path:
            raise MatrixEncoderError("ไฟล์เอาต์พุตต้องไม่ซ้ำกับไฟล์อินพุต", 2)
        if self.output_path.exists() and not self.args.yes and not self.args.dry_run:
            raise MatrixEncoderError(
                f"ไฟล์เอาต์พุต '{self.output_path.name}' มีอยู่แล้ว กรุณาระบุ -y เพื่อเขียนทับ",
                2,
            )

    def _parse_trims(self, trim_list: Optional[List[str]]) -> Dict[str, float]:
        trims = {}
        if not trim_list:
            return trims
        for item in trim_list:
            if "=" not in item:
                raise MatrixEncoderError(f"รูปแบบ --trim ไม่ถูกต้อง: '{item}' (ต้องเป็น CH=dB)", 2)
            ch, val_str = item.split("=", 1)
            ch = ch.strip().upper()
            norm = {"LS": "Ls", "RS": "Rs", "LB": "Lb", "RB": "Rb"}.get(ch, ch)
            if norm not in STANDARD_ROLES:
                raise MatrixEncoderError(f"ไม่รู้จักช่องเสียง '{ch}' ในคำสั่ง --trim", 2)
            val_str = val_str.strip().lower()
            if val_str in ("-inf", "mute", "off"):
                trims[norm] = -float("inf")
            else:
                try:
                    val = float(val_str)
                    if not (-96.0 <= val <= 12.0):
                        raise ValueError()
                    trims[norm] = val
                except ValueError:
                    raise MatrixEncoderError(
                        f"ค่า dB ใน --trim {ch}={val_str} ต้องอยู่ระหว่าง -96 ถึง +12 dB", 2
                    )
        return trims

    def _probe_audio_source(self) -> StreamAudioInfo:
        # ลองเปิดเป็น WAV ภายในก่อน
        is_wav = False
        try:
            with open(self.input_path, "rb") as f:
                head = f.read(12)
                if len(head) >= 12 and head[:4] in (b"RIFF", b"RF64", b"BW64") and head[8:12] == b"WAVE":
                    is_wav = True
        except Exception:
            pass

        if is_wav:
            with open(self.input_path, "rb") as f:
                reader = WavStreamReader(f)
                info = reader.info
                # หากไม่มี FFmpeg จะรับเฉพาะ Float32 48kHz เท่านั้น
                if not self.ffmpeg_bin:
                    if info.sample_rate != SAMPLE_RATE_TARGET or "float" not in info.codec:
                        raise MatrixEncoderError(
                            f"ตรวจไม่พบ FFmpeg ในระบบ และไฟล์ WAV ไม่ใช่ 32-bit Float 48 kHz "
                            f"(พบ: {info.codec}, {info.sample_rate} Hz) กรุณาติดตั้ง FFmpeg",
                            4,
                        )
                    return info
                # หากมี FFmpeg และไฟล์เป็น 48kHz float32 อยู่แล้ว อ่านตรงได้เลย
                if info.sample_rate == SAMPLE_RATE_TARGET and "float" in info.codec:
                    return info

        # หากไม่ใช่ WAV หรือต้อง resample ใช้ ffprobe ตรวจสอบ
        if not self.ffprobe_bin or not self.ffmpeg_bin:
            raise MatrixEncoderError(
                "จำเป็นต้องใช้ FFmpeg และ FFprobe เพื่ออ่านและแปลงไฟล์ชนิดนี้ กรุณาติดตั้ง FFmpeg ในระบบ",
                4,
            )

        cmd = [
            self.ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            f"a:{self.args.stream}",
            "-show_entries",
            "stream=index,codec_name,channels,channel_layout,sample_rate,bits_per_raw_sample,duration",
            "-show_entries",
            "format=format_name,duration",
            "-of",
            "json",
            str(self.input_path),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            meta = json.loads(res.stdout)
        except Exception as e:
            raise MatrixEncoderError(f"FFprobe ตรวจสอบไฟล์ล้มเหลว: {e}", 4)

        streams = meta.get("streams", [])
        if not streams:
            raise MatrixEncoderError(
                f"ไม่พบสตรีมเสียงหมายเลข {self.args.stream} ในไฟล์ {self.input_path.name}", 3
            )

        s = streams[0]
        fmt = meta.get("format", {})
        dur = float(s.get("duration") or fmt.get("duration") or 0.0)
        nch = int(s.get("channels") or 0)
        sr = int(s.get("sample_rate") or 0)

        return StreamAudioInfo(
            container=fmt.get("format_name", "unknown"),
            codec=s.get("codec_name", "unknown"),
            sample_rate=sr,
            channels=nch,
            bit_depth=int(s.get("bits_per_raw_sample") or 16),
            channel_layout_str=s.get("channel_layout"),
            channel_mask=None,
            total_frames=int(dur * SAMPLE_RATE_TARGET) if dur > 0 else None,
            duration_sec=dur,
            source_is_pipe=True,
        )

    def _setup_dsp(self):
        # 1. Hilbert FIR Type III (เฉพาะ ds, pl, pl2)
        self.use_hilbert = self.engine.mode in (
            MatrixMode.DS,
            MatrixMode.PL,
            MatrixMode.PL2,
        )
        self.hilbert_latency = HILBERT_LATENCY if self.use_hilbert else 0
        self.hilbert_ls = HilbertTransformerFIR() if self.use_hilbert else None
        self.hilbert_rs = HilbertTransformerFIR() if self.use_hilbert else None
        self.hilbert_s = HilbertTransformerFIR() if self.use_hilbert else None

        self.front_delays: Dict[str, DelayLine] = {
            "L": DelayLine(self.hilbert_latency),
            "R": DelayLine(self.hilbert_latency),
            "C": DelayLine(self.hilbert_latency),
            "LFE": DelayLine(self.hilbert_latency),
        }

        # 2. Surround Cutoff Filters
        hp_str = str(self.args.surround_hp).lower()
        lp_str = str(self.args.surround_lp).lower()

        # ค่าเริ่มต้นตามโหมด
        if hp_str == "auto":
            hp_hz = 100.0 if self.engine.mode in (MatrixMode.DS, MatrixMode.PL) else None
        elif hp_str == "off":
            hp_hz = None
        else:
            try:
                hp_hz = float(hp_str)
            except ValueError:
                raise MatrixEncoderError(f"ค่า --surround-hp ไม่ถูกต้อง: {hp_str}", 2)

        if lp_str == "auto":
            lp_hz = 7000.0 if self.engine.mode in (MatrixMode.DS, MatrixMode.PL) else None
        elif lp_str == "off":
            lp_hz = None
        else:
            try:
                lp_hz = float(lp_str)
            except ValueError:
                raise MatrixEncoderError(f"ค่า --surround-lp ไม่ถูกต้อง: {lp_str}", 2)

        if hp_hz and lp_hz and lp_hz <= hp_hz:
            raise MatrixEncoderError(
                f"ความถี่ Surround Cutoff ขัดแย้ง: LP ({lp_hz} Hz) ต้องมากกว่า HP ({hp_hz} Hz)", 2
            )

        self.surround_filters: Dict[str, Union[BiquadSOSFilter, KaiserFIRFilter]] = {}
        cutoff_type = CutoffType(self.args.cutoff_type)
        if hp_hz or lp_hz:
            for r in ("S", "Ls", "Rs"):
                if hp_hz and lp_hz:
                    if cutoff_type == CutoffType.HILL:
                        self.surround_filters[r] = BiquadSOSFilter(
                            "bandpass", (hp_hz, lp_hz), slope_db=self.args.slope
                        )
                    else:
                        self.surround_filters[r] = KaiserFIRFilter((hp_hz, lp_hz), btype="bandpass")
                elif hp_hz:
                    if cutoff_type == CutoffType.HILL:
                        self.surround_filters[r] = BiquadSOSFilter(
                            "highpass", hp_hz, slope_db=self.args.slope
                        )
                    else:
                        self.surround_filters[r] = KaiserFIRFilter(hp_hz, btype="highpass")
                elif lp_hz:
                    if cutoff_type == CutoffType.HILL:
                        self.surround_filters[r] = BiquadSOSFilter(
                            "lowpass", lp_hz, slope_db=self.args.slope
                        )
                    else:
                        self.surround_filters[r] = KaiserFIRFilter(lp_hz, btype="lowpass")

        # 3. LFE Low-Pass Filter
        self.lfe_filter = None
        if self.engine.lfe_enabled and "LFE" in self.mapper.assigned_roles:
            lfe_lp_str = str(self.args.lfe_lp).lower()
            if lfe_lp_str != "off":
                try:
                    lfe_hz = float(lfe_lp_str)
                    if not (10.0 <= lfe_hz <= 2000.0):
                        raise ValueError()
                    if cutoff_type == CutoffType.HILL:
                        self.lfe_filter = BiquadSOSFilter("lowpass", lfe_hz, slope_db=self.args.slope)
                    else:
                        self.lfe_filter = KaiserFIRFilter(lfe_hz, btype="lowpass")
                except ValueError:
                    raise MatrixEncoderError(f"ค่า --lfe-lp ไม่ถูกต้อง: {lfe_lp_str}", 2)

    def _open_input_stream(self) -> Union[WavStreamReader, FFmpegStreamReader]:
        if not self.info.source_is_pipe:
            f = open(self.input_path, "rb")
            return WavStreamReader(f)

        cmd = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-drc_scale",
            "0",
            "-i",
            str(self.input_path),
            "-map",
            f"0:a:{self.args.stream}",
            "-vn",
            "-sn",
            "-dn",
            "-ar",
            str(SAMPLE_RATE_TARGET),
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            "-",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1024 * 1024,
        )
        return FFmpegStreamReader(proc, self.info)

    def dry_run(self) -> Dict[str, any]:
        """ประมวลผลเฉพาะ Metadata และสรุปแผนงาน"""
        matrix_table = self.engine.get_matrix_table()
        surr_latency = self.hilbert_latency
        if any(isinstance(f, KaiserFIRFilter) for f in self.surround_filters.values()):
            first_fir = next(f for f in self.surround_filters.values() if isinstance(f, KaiserFIRFilter))
            surr_latency += first_fir.latency_samples

        total_frames = self.info.total_frames or 0
        est_output_bytes = 128 + total_frames * 2 * 4 if total_frames > 0 else 0
        is_rf64 = est_output_bytes > (0xFFFFFFFF - 512)

        ffmpeg_cmd = (
            f"ffmpeg -i '{self.input_path.name}' -map 0:a:{self.args.stream} "
            f"-vn -ar 48000 -c:a pcm_f32le -f f32le -"
            if self.info.source_is_pipe
            else "Direct WAV Stream"
        )

        res = {
            "input": {
                "file": str(self.input_path),
                "container": self.info.container,
                "codec": self.info.codec,
                "sample_rate": self.info.sample_rate,
                "channels": self.info.channels,
                "duration_sec": self.info.duration_sec,
                "total_frames": total_frames,
                "layout_detected": self.info.channel_layout_str,
                "channel_mask": hex(self.info.channel_mask) if self.info.channel_mask else None,
            },
            "mapping": {
                "assigned_roles": self.mapper.assigned_roles,
                "dropped_channels": self.mapper.dropped_channels,
            },
            "compatibility_rules": self.engine.rules_applied,
            "warnings": self.mapper.warnings + self.engine.warnings,
            "matrix_2xN": matrix_table,
            "dsp": {
                "mode": self.engine.mode.value,
                "pl2_preset": self.args.pl2_preset,
                "hilbert_filter": "FIR Type III (2047 taps)" if self.use_hilbert else "None",
                "cutoff_type": self.args.cutoff_type,
                "surround_filter": (
                    f"{self.args.cutoff_type} HP={self.args.surround_hp} LP={self.args.surround_lp}"
                    if self.surround_filters
                    else "off"
                ),
                "lfe_filter": (
                    f"{self.args.cutoff_type} LP={self.args.lfe_lp}"
                    if self.lfe_filter
                    else "off"
                ),
                "global_latency_samples": surr_latency,
                "global_latency_ms": round(surr_latency / SAMPLE_RATE_TARGET * 1000.0, 3),
            },
            "output": {
                "target_file": str(self.output_path),
                "estimated_size_bytes": est_output_bytes,
                "format": "RF64 (>4GB)" if is_rf64 else "WAVEFORMATEXTENSIBLE",
                "peak_headroom_scale": self.engine.compute_static_headroom_scale(),
            },
            "ffmpeg_command": ffmpeg_cmd,
        }
        return res

    def run(self) -> int:
        """รันการแปลงสัญญาณเสียงจริงแบบ Block-by-block"""
        if self.args.dry_run:
            info_dict = self.dry_run()
            if self.args.json:
                print(json.dumps(info_dict, ensure_ascii=False, indent=2))
            else:
                self._print_dry_run_text(info_dict)
            return 0

        # สแกนสองรอบหากระบุ --headroom peak
        peak_scale = 1.0
        if self.engine.headroom_mode == HeadroomMode.PEAK:
            if not self.args.quiet:
                print("[PASS 1/2] กำลังสแกนหาค่าพีค (Peak Normalization)...", file=sys.stderr)
            max_peak = self._scan_peak()
            if max_peak > 1.0:
                peak_scale = 1.0 / max_peak
                if not self.args.quiet:
                    print(
                        f"         ตรวจพบพีคเกิน 0 dBFS (+{20*math.log10(max_peak):.2f} dB) "
                        f"ปรับลดสเกลลง {20*math.log10(peak_scale):.2f} dB",
                        file=sys.stderr,
                    )
            # รีเซ็ต DSP State
            self._setup_dsp()

        if not self.args.quiet:
            print(
                f"[STAGE] กำลังประมวลผล Matrix Downmix -> {self.output_path.name} "
                f"({self.engine.mode.value.upper()})...",
                file=sys.stderr,
            )

        stream = self._open_input_stream()
        writer = WavWriterF32(self.output_path)
        block_size = max(HILBERT_TAPS_DEFAULT, self.args.block_size)

        total_frames = self.info.total_frames or 0
        processed_frames = 0
        overall_peak = 0.0
        nan_inf_count = 0

        static_scale = self.engine.compute_static_headroom_scale() * self.engine.out_gain * peak_scale
        latency = self.hilbert_latency
        warmup_dropped = 0

        try:
            while True:
                chunk = stream.read_frames(block_size)
                if chunk.shape[0] == 0:
                    break

                # ตรวจสอบและแทนค่า NaN/Inf
                invalid = ~np.isfinite(chunk)
                if np.any(invalid):
                    nan_inf_count += int(np.sum(invalid))
                    chunk[invalid] = 0.0

                lt, rt = self._process_block_audio(chunk, static_scale)

                out_stereo = np.column_stack((lt, rt)).astype(np.float32)

                # ตัด delay warmup จากหัวสัญญาณเพื่อให้อินพุตกับเอาต์พุตมีเวลาตรงกัน
                if warmup_dropped < latency:
                    drop = min(latency - warmup_dropped, len(out_stereo))
                    warmup_dropped += drop
                    out_stereo = out_stereo[drop:]

                if len(out_stereo) > 0:
                    cur_peak = float(np.max(np.abs(out_stereo)))
                    if cur_peak > overall_peak:
                        overall_peak = cur_peak
                    writer.write_frames(out_stereo)

                processed_frames += chunk.shape[0]
                if not self.args.quiet and total_frames > 0:
                    pct = min(100.0, (processed_frames / total_frames) * 100.0)
                    sys.stderr.write(
                        f"\r  ความคืบหน้า: {pct:5.1f}% [{processed_frames:,}/{total_frames:,} เฟรม]"
                    )
                    sys.stderr.flush()

            # Flush Tail ของ Delay Line และ Filters
            tail_lt, tail_rt = self._flush_dsp_tails(static_scale)
            if tail_lt.size > 0:
                tail_stereo = np.column_stack((tail_lt, tail_rt)).astype(np.float32)
                if warmup_dropped < latency:
                    drop = min(latency - warmup_dropped, len(tail_stereo))
                    warmup_dropped += drop
                    tail_stereo = tail_stereo[drop:]
                if len(tail_stereo) > 0:
                    cur_peak = float(np.max(np.abs(tail_stereo)))
                    if cur_peak > overall_peak:
                        overall_peak = cur_peak
                    writer.write_frames(tail_stereo)

            if not self.args.quiet and total_frames > 0:
                sys.stderr.write("\r  ความคืบหน้า: 100.0% เสร็จสมบูรณ์!                \n")
                sys.stderr.flush()

            writer.close()
        except KeyboardInterrupt:
            writer.close(abort=True)
            stream.close()
            print("\n[ABORT] ผู้ใช้ยกเลิกการทำงาน", file=sys.stderr)
            return 130
        except Exception as e:
            writer.close(abort=True)
            stream.close()
            raise e
        finally:
            stream.close()

        # รายงานสรุป
        peak_db = 20 * math.log10(overall_peak) if overall_peak > 1e-7 else -99.0
        if not self.args.quiet:
            print("=" * 70, file=sys.stderr)
            print("[SUCCESS] การแปลงสัญญาณสำเร็จเรียบร้อย!", file=sys.stderr)
            print(f"  ไฟล์ผลลัพธ์: {self.output_path}", file=sys.stderr)
            print(f"  ระดับพีคสูงสุด (Peak): {peak_db:.2f} dBFS", file=sys.stderr)
            if nan_inf_count > 0:
                print(
                    f"  [WARNING] ตรวจพบตัวอย่าง NaN/Inf จำนวน {nan_inf_count} ตัวอย่าง (ถูกแทนด้วย 0.0)",
                    file=sys.stderr,
                )
            if peak_db > 0.0:
                print(
                    f"  [WARNING] สัญญาณเกิดการคลิปเกิน 0 dBFS (+{peak_db:.2f} dB) "
                    f"แนะนำให้ใช้ --headroom static หรือ --headroom peak",
                    file=sys.stderr,
                )
            print("=" * 70, file=sys.stderr)

        return 0

    def _scan_peak(self) -> float:
        """สแกนไฟล์เพื่อหาค่าพีคสูงสุดสำหรับรอบที่ 2"""
        stream = self._open_input_stream()
        block_size = max(HILBERT_TAPS_DEFAULT, self.args.block_size)
        max_p = 0.0
        try:
            while True:
                chunk = stream.read_frames(block_size)
                if chunk.shape[0] == 0:
                    break
                lt, rt = self._process_block_audio(chunk, 1.0)
                cur_p = max(float(np.max(np.abs(lt))), float(np.max(np.abs(rt))))
                if cur_p > max_p:
                    max_p = cur_p
        finally:
            stream.close()
        return max_p

    def _process_block_audio(
        self, chunk: np.ndarray, scale: float
    ) -> Tuple[np.ndarray, np.ndarray]:
        """ประมวลผลบล็อกสัญญาณเสียงทีละขั้นตอน"""
        n_frames = chunk.shape[0]
        role_map: Dict[str, np.ndarray] = {}

        # ดึงช่องตามบทบาท
        for idx, role in enumerate(self.mapper.assigned_roles):
            if role:
                role_map[role] = chunk[:, idx].copy()

        # 1. 7.1 Fold-back: พับ Lb/Rb เข้า Ls/Rs
        if "Lb" in role_map and "Rb" in role_map:
            bg = np.float32(self.engine.back_gain)
            role_map["Ls"] = role_map.get("Ls", np.zeros(n_frames, np.float32)) + bg * role_map.pop("Lb")
            role_map["Rs"] = role_map.get("Rs", np.zeros(n_frames, np.float32)) + bg * role_map.pop("Rb")

        # 2. FLC/FRC Fold-in
        if "FLC" in role_map:
            role_map["L"] = role_map.get("L", np.zeros(n_frames, np.float32)) + np.float32(INV_SQRT2) * role_map.pop("FLC")
        if "FRC" in role_map:
            role_map["R"] = role_map.get("R", np.zeros(n_frames, np.float32)) + np.float32(INV_SQRT2) * role_map.pop("FRC")

        # 3. Compatibility การแปลงระหว่าง Mono S และ Stereo Ls/Rs
        if self.engine.mode in (MatrixMode.DS, MatrixMode.PL):
            if "Ls" in role_map and "Rs" in role_map:
                # รวมเป็นโมโน S
                s = np.float32(INV_SQRT2) * (role_map.pop("Ls") + role_map.pop("Rs"))
                role_map["S"] = s
            if self.engine.mode == MatrixMode.DS and "C" in role_map:
                c_sig = role_map.pop("C")
                role_map["L"] = role_map.get("L", np.zeros(n_frames, np.float32)) + np.float32(INV_SQRT2) * c_sig
                role_map["R"] = role_map.get("R", np.zeros(n_frames, np.float32)) + np.float32(INV_SQRT2) * c_sig
        else:  # pl2, pl2s
            if "S" in role_map and not ("Ls" in role_map or "Rs" in role_map):
                s = role_map.pop("S")
                role_map["Ls"] = np.float32(INV_SQRT2) * s
                role_map["Rs"] = np.float32(INV_SQRT2) * s

        # 4. ใช้ตัวคูณ Trim รายช่อง
        for role in list(role_map.keys()):
            t = self.engine.get_role_gain(role)
            if t != 1.0:
                role_map[role] *= np.float32(t)

        # 5. กรอง Surround Cutoff
        for role in ("S", "Ls", "Rs"):
            if role in role_map and role in self.surround_filters:
                role_map[role] = self.surround_filters[role].process_block(role_map[role])

        # 6. กรอง LFE Cutoff
        lfe_sig = None
        if self.engine.lfe_enabled and "LFE" in role_map:
            lfe_raw = role_map["LFE"]
            lfe_sig = self.lfe_filter.process_block(lfe_raw) if self.lfe_filter else lfe_raw
            lfe_sig = lfe_sig * np.float32(self.engine.lfe_gain)

        # 7. เลื่อนเฟส Surround 90° (Hilbert) และชดเชยดีเลย์ช่องหน้า
        if self.use_hilbert:
            if "S" in role_map and self.hilbert_s:
                role_map["S"] = self.hilbert_s.process_block(role_map["S"])
            if "Ls" in role_map and self.hilbert_ls:
                role_map["Ls"] = self.hilbert_ls.process_block(role_map["Ls"])
            if "Rs" in role_map and self.hilbert_rs:
                role_map["Rs"] = self.hilbert_rs.process_block(role_map["Rs"])

            # หน่วงเวลาช่องหน้า L, R, C (และ LFE) ให้เท่ากับ Latency ของ Hilbert
            for role in ("L", "R", "C"):
                if role in role_map:
                    role_map[role] = self.front_delays[role].process_block(role_map[role])
            if lfe_sig is not None:
                lfe_sig = self.front_delays["LFE"].process_block(lfe_sig)

        # 8. คำนวณผลรวม Matrix ตามโหมด
        lt = role_map.get("L", np.zeros(n_frames, np.float32)).copy()
        rt = role_map.get("R", np.zeros(n_frames, np.float32)).copy()

        if "C" in role_map:
            c = role_map["C"]
            lt += np.float32(INV_SQRT2) * c
            rt += np.float32(INV_SQRT2) * c

        if self.engine.mode in (MatrixMode.DS, MatrixMode.PL):
            if "S" in role_map:
                s = role_map["S"]
                lt -= np.float32(INV_SQRT2) * s
                rt += np.float32(INV_SQRT2) * s
        elif self.engine.mode == MatrixMode.PL2:
            maj = np.float32(LEGACY_MAJOR if self.engine.pl2_preset == "legacy" else SQRT_2_3)
            minor = np.float32(LEGACY_MINOR if self.engine.pl2_preset == "legacy" else SQRT_1_3)
            ls = role_map.get("Ls", np.zeros(n_frames, np.float32))
            rs = role_map.get("Rs", np.zeros(n_frames, np.float32))
            lt += -maj * ls - minor * rs
            rt += minor * ls + maj * rs
        elif self.engine.mode == MatrixMode.PL2S:
            ls = role_map.get("Ls", np.zeros(n_frames, np.float32))
            rs = role_map.get("Rs", np.zeros(n_frames, np.float32))
            lt -= np.float32(INV_SQRT2) * (ls + rs)
            rt += np.float32(INV_SQRT2) * (ls + rs)

        if lfe_sig is not None:
            lt += lfe_sig
            rt += lfe_sig

        if scale != 1.0:
            lt *= np.float32(scale)
            rt *= np.float32(scale)

        return lt, rt

    def _flush_dsp_tails(self, scale: float) -> Tuple[np.ndarray, np.ndarray]:
        """Flush หางสัญญาณที่ค้างใน Delay Line และ Filter เพื่อความถูกต้องของเฟสท้ายไฟล์"""
        if not self.use_hilbert or self.hilbert_latency == 0:
            return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)

        tail_len = self.hilbert_latency
        zeros = np.zeros((tail_len, self.info.channels), dtype=np.float32)
        return self._process_block_audio(zeros, scale)

    def _print_dry_run_text(self, d: Dict[str, any]):
        """พิมพ์ผลการวิเคราะห์ Dry-run ในรูปแบบข้อความสวยงาม"""
        print("=" * 75)
        print("  6to2matrixencoder - สรุปแผนการทำงานและการตั้งค่า (DRY RUN)")
        print("=" * 75)
        inp = d["input"]
        print(f"ข้อมูลอินพุต: {inp['file']}")
        print(
            f"  ฟอร์แมต: {inp['container']} | Codec: {inp['codec']} | "
            f"{inp['sample_rate']} Hz | {inp['channels']} ช่อง | ความยาว: {inp['duration_sec']:.2f} วินาที"
        )
        print(f"  เลย์เอาต์ที่ตรวจพบ: {inp['layout_detected']} (Mask: {inp['channel_mask']})")
        print("-" * 75)
        print("การกำหนดบทบาทช่องสัญญาณ (Channel Mapping):")
        for idx, r in enumerate(d["mapping"]["assigned_roles"]):
            status = f"-> บทบาท '{r}'" if r else "-> ตัดทิ้ง (Dropped)"
            print(f"  ช่องที่ {idx}: {status}")

        if d["compatibility_rules"]:
            print("-" * 75)
            print("กฎความเข้ากันได้ที่ถูกนำมาใช้ (Compatibility Rules):")
            for rule in d["compatibility_rules"]:
                print(f"  * {rule}")

        if d["warnings"]:
            print("-" * 75)
            print("ข้อความแจ้งเตือน (Warnings):")
            for w in d["warnings"]:
                print(f"  [WARN] {w}")

        print("-" * 75)
        print(f"การประมวลผล DSP (โหมด: {d['dsp']['mode'].upper()}):")
        print(f"  - Hilbert Phase Shift: {d['dsp']['hilbert_filter']}")
        print(f"  - Surround Filter: {d['dsp']['surround_filter']}")
        print(f"  - LFE Filter: {d['dsp']['lfe_filter']}")
        print(
            f"  - Delay ชดเชยช่องหน้า: {d['dsp']['global_latency_samples']} ตัวอย่าง "
            f"({d['dsp']['global_latency_ms']} ms)"
        )
        print(f"  - ตัวคูณ Headroom Scale: {d['output']['peak_headroom_scale']:.4f}")

        print("-" * 75)
        print("ตารางสัมประสิทธิ์เมทริกซ์ 2xN สรุปสุดท้าย (Final Matrix Coefficients):")
        print("  Index | บทบาท | Lt สัมประสิทธิ์ | Lt (dB)   | Rt สัมประสิทธิ์ | Rt (dB)")
        print("  " + "-" * 67)
        for row in d["matrix_2xN"]:
            lt_db_str = f"{row['lt_db']:+.2f} dB" if row["lt_db"] > -900 else "-inf dB"
            rt_db_str = f"{row['rt_db']:+.2f} dB" if row["rt_db"] > -900 else "-inf dB"
            print(
                f"  {row['index']:5d} | {row['role']:5s} | "
                f"{row['lt_coef']:14.4f} | {lt_db_str:9s} | "
                f"{row['rt_coef']:14.4f} | {rt_db_str:9s}"
            )

        print("-" * 75)
        print("ข้อมูลไฟล์เอาต์พุต:")
        out = d["output"]
        print(f"  ปลายทาง: {out['target_file']}")
        print(
            f"  ฟอร์แมต Header: {out['format']} | "
            f"ประมาณขนาดไฟล์: {out['estimated_size_bytes'] / (1024*1024):.2f} MB"
        )
        print(f"  คำสั่ง FFmpeg: {d['ffmpeg_command']}")
        print("=" * 75)


# ==============================================================================
# 8. Command-Line Interface (CLI Arguments & Thai Help)
# ==============================================================================
HELP_TEXT_THAI = """
โปรแกรมแปลงสัญญาณเสียงมัลติแชนเนล (5.1, 7.1, Quad, ฯลฯ) เป็น Matrix Lt/Rt 2.0 ช่องสัญญาณ
ประมวลผลด้วยความละเอียด 32-bit Float 48 kHz รองรับมาตรฐาน Dolby Surround, Pro Logic และ Pro Logic II

ตารางโหมดเมทริกซ์ (--mode):
  ds      Dolby Surround ดั้งเดิม (L, R, S โมโนเซอร์ราวด์ เลื่อนเฟส ±90 องศา)
  pl      Dolby Pro Logic (L, R, C, S โมโนเซอร์ราวด์ เลื่อนเฟส ±90 องศา)
  pl2     Dolby Pro Logic II [ค่าเริ่มต้น] (L, R, C, Ls, Rs เซอร์ราวด์สเตอริโอ เลื่อนเฟส 90 องศา ด้วย FIR Hilbert)
  pl2s    PLII Simple / ATSC A/52 §7.8 (L, R, C, Ls, Rs แบบกลับเฟส 180 องศา ไม่ใช้ Hilbert)

ตารางช่องสัญญาณที่รองรับ:
  1/0 (Mono)       : ไม่รองรับการดาวน์มิกซ์ (ส่งกลับ Exit Code 3)
  2/0 (Stereo)     : บายพาส Lt=L, Rt=R พร้อมแจ้งเตือน
  3/0 (3.0)        : L, R, C
  2/1 หรือ 3/1     : L, R, (C), S (โมโนเซอร์ราวด์)
  2/2 หรือ 3/2     : 4.0 Quad หรือ 5.0/5.1 Surround (L, R, C, Ls, Rs, .LFE)
  3/2/2 (7.1)      : L, R, C, LFE, Ls, Rs, Lb, Rb (พับ Lb/Rb เข้า Ls/Rs ที่ -3.01 dB)

ตัวอย่างคำสั่งการใช้งาน:
  1. แปลงไฟล์ 5.1 ทั่วไปเป็น Dolby Pro Logic II (ค่าเริ่มต้น):
     python lib/6to2matrixencoder.py -i movie_51.wav -o movie_ltrt.wav

  2. แปลงไฟล์ 7.1 MKV เป็น Dolby Pro Logic พร้อมตรวจสอบข้อมูลก่อนรันจริง (Dry-run):
     python lib/6to2matrixencoder.py -i movie_71.mkv --mode pl --dry-run

  3. แปลงไฟล์ 5.1 โดยเปิดผสมช่อง LFE พร้อมลดเกน LFE ลง -10 dB:
     python lib/6to2matrixencoder.py -i concert_51.wav --lfe --lfe-gain -10.0

  4. ปรับเกน Center ลดลง -3 dB และชดเชย Headroom แบบ static:
     python lib/6to2matrixencoder.py -i input.wav --trim C=-3.0 --headroom static

  5. ระบุลำดับช่องสัญญาณของไฟล์ PCM ดิบด้วยตนเอง:
     python lib/6to2matrixencoder.py -i raw_6ch.wav --layout "L,R,C,LFE,Ls,Rs"

  6. ใช้ตัวกรอง Surround แบบ FIR Kaiser ความชันสูง:
     python lib/6to2matrixencoder.py -i audio.wav --cutoff-type brick --surround-lp 7000

ข้อจำกัดที่ควรทราบ:
  - วงจรลดเสียงรบกวน Dolby NR (Modified B-Type) เป็นสิทธิ์เฉพาะของ Dolby Laboratories
    โปรแกรมนี้จึงไม่ได้จำลองวงจรบีบอัดไดนามิก NR ดังกล่าว แต่ใช้การจำกัดแบนด์วิดท์ 100Hz-7kHz แทน
  - ไฟล์โมโน (1 ช่อง) ไม่สามารถแปลงเป็น Lt/Rt ได้ ระบบจะแจ้งข้อผิดพลาด
  - หากไม่มีโปรแกรม FFmpeg ติดตั้งในระบบ โปรแกรมจะรองรับเฉพาะไฟล์ WAV 32-bit Float 48 kHz เท่านั้น

รหัสสถานะการสิ้นสุด (Exit Codes):
  0   สำเร็จเรียบร้อย
  1   เกิดข้อผิดพลาดทั่วไป
  2   การกำหนดอาร์กิวเมนต์ไม่ถูกต้อง
  3   เลย์เอาต์ช่องสัญญาณอินพุตไม่รองรับ (เช่น ไฟล์โมโน)
  4   ไม่พบ FFmpeg หรือ FFmpeg ทำงานล้มเหลว
  5   เกิดข้อผิดพลาดในการอ่าน/เขียนไฟล์ (I/O Error)
  130 ผู้ใช้ยกเลิกการทำงาน (Ctrl+C)
"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="6to2matrixencoder.py",
        description=HELP_TEXT_THAI,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-i", "--input", required=True, help="พาธไฟล์เสียงนำเข้า (WAV หรือมัลติมีเดียใด ๆ)")
    parser.add_argument("-o", "--output", help="พาธไฟล์ WAV ผลลัพธ์ (ค่าเริ่มต้น: <input>.ltrt.wav)")
    parser.add_argument("-y", "--yes", action="store_true", help="เขียนทับไฟล์ผลลัพธ์เดิมโดยไม่ถาม")
    parser.add_argument(
        "--mode",
        choices=["ds", "pl", "pl2", "pl2s"],
        default="pl2",
        help="โหมดการเข้ารหัสเมทริกซ์ (ค่าเริ่มต้น: pl2)",
    )
    parser.add_argument(
        "--pl2-preset",
        choices=["standard", "legacy"],
        default="legacy",
        help="สูตรสัมประสิทธิ์ PLII: standard (-1.76/-4.77 dB) หรือ legacy (-1.2/-6.2 dB ตามไฟล์แนบ)",
    )
    parser.add_argument(
        "--layout",
        default="auto",
        help="กำหนดลำดับบทบาทช่องเสียงเอง คั่นด้วยจุลภาค เช่น 'L,R,C,LFE,Ls,Rs'",
    )
    parser.add_argument("--stream", type=int, default=0, help="ดัชนีสตรีมเสียงในไฟล์ (ค่าเริ่มต้น: 0)")
    parser.add_argument("--lfe", action="store_true", help="เปิดการผสมช่อง LFE เข้าสู่ Lt/Rt")
    parser.add_argument("--lfe-gain", type=float, default=0.0, help="เกนขยายของช่อง LFE เมื่อเปิดผสม (dB)")
    parser.add_argument("--lfe-lp", default="120", help="ความถี่ Low-pass ของช่อง LFE (Hz หรือ off)")
    parser.add_argument(
        "--surround-hp",
        default="auto",
        help="ความถี่ High-pass ของเซอร์ราวด์ (auto, off, หรือตัวเลข Hz)",
    )
    parser.add_argument(
        "--surround-lp",
        default="auto",
        help="ความถี่ Low-pass ของเซอร์ราวด์ (auto, off, หรือตัวเลข Hz)",
    )
    parser.add_argument(
        "--cutoff-type",
        choices=["hill", "brick"],
        default="hill",
        help="ชนิดตัวกรอง: hill (IIR Butterworth) หรือ brick (FIR Kaiser)",
    )
    parser.add_argument(
        "--slope",
        type=int,
        default=12,
        help="ความชันของตัวกรอง IIR (dB/oct): 6, 12, 24, 48, 96",
    )
    parser.add_argument(
        "--back-gain",
        type=float,
        default=-3.0103,
        help="เกนการพับช่อง Lb/Rb เข้าสู่ Ls/Rs สำหรับ 7.1 (dB)",
    )
    parser.add_argument(
        "--trim",
        action="append",
        help="ปรับเกนรายช่อง เช่น --trim C=-3.0 --trim Ls=-1.5 (ระบุซ้ำได้หลายช่อง)",
    )
    parser.add_argument(
        "--headroom",
        choices=["none", "static", "peak"],
        default="none",
        help="การจัดการ Headroom: none, static (ลดเกนตาม worst-case), peak (สแกนสองรอบ)",
    )
    parser.add_argument("--out-gain", type=float, default=0.0, help="เกนขยายสุดท้ายของสัญญาณ Lt/Rt (dB)")
    parser.add_argument(
        "--block-size",
        type=int,
        default=65536,
        help="ขนาดบล็อกประมวลผล (เฟรม, ค่าเริ่มต้น 65536)",
    )
    parser.add_argument("--ffmpeg", help="ระบุพาธไปยังโปรแกรม ffmpeg โดยตรง")
    parser.add_argument("--dry-run", action="store_true", help="ตรวจสอบข้อมูลและคำนวณค่าโดยไม่เขียนไฟล์")
    parser.add_argument("--json", action="store_true", help="ส่งออกข้อมูล dry-run ในรูปแบบ JSON สำหรับเครื่องอ่าน")
    parser.add_argument("-v", "--verbose", action="store_true", help="แสดงข้อความการทำงานอย่างละเอียด")
    parser.add_argument("-q", "--quiet", action="store_true", help="ปิดการแสดงแถบความคืบหน้า")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--list-modes",
        action="store_true",
        help="แสดงรายละเอียดและสมการของทุกโหมดแล้วออก",
    )
    parser.add_argument(
        "--list-layouts",
        action="store_true",
        help="แสดงเลย์เอาต์ช่องเสียงที่รองรับแล้วออก",
    )
    return parser


def main():
    parser = build_arg_parser()
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(2)

    if "--list-modes" in sys.argv:
        print("โหมดการเข้ารหัส Matrix ที่รองรับ:")
        print("  ds   : Dolby Surround (L, R, S mono ±90°)")
        print("  pl   : Dolby Pro Logic (L, R, C, S mono ±90°)")
        print("  pl2  : Dolby Pro Logic II (L, R, C, Ls, Rs 90° FIR Hilbert)")
        print("  pl2s : PLII Simple / ATSC A/52 §7.8 (L, R, C, Ls, Rs 180° Inversion)")
        sys.exit(0)

    if "--list-layouts" in sys.argv:
        print("เลย์เอาต์ช่องสัญญาณที่รองรับ:")
        for ch_count, roles in FALLBACK_CHANNEL_ORDERS.items():
            print(f"  {ch_count} ช่อง: {', '.join(roles)}")
        sys.exit(0)

    args = parser.parse_args()

    # ตรวจสอบ slope ของ IIR
    if args.slope % 6 != 0 or args.slope <= 0:
        print(f"ข้อผิดพลาด: ค่า --slope ({args.slope}) ต้องเป็นจำนวนเต็มบวกที่หารด้วย 6 ลงตัว", file=sys.stderr)
        sys.exit(2)

    try:
        pipeline = EncoderPipeline(args)
        rc = pipeline.run()
        sys.exit(rc)
    except MatrixEncoderError as e:
        print(f"\n[ข้อผิดพลาด] {e.message}", file=sys.stderr)
        sys.exit(e.exit_code)
    except Exception as e:
        print(f"\n[ข้อผิดพลาดร้ายแรง] {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
