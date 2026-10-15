#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib/2to6matrixdecoder.py
===============================================================================
เครื่องมือถอดรหัสสัญญาณเสียงสเตอริโอเมทริกซ์ Lt/Rt 2.0 เป็น 5.1 เซอร์ราวด์ (Float32 48 kHz)
รองรับการนำเข้าเป็นโมดูล (Importable Module) และการรันเป็น CLI ผ่านคำสั่งโดยตรง

สถาปัตยกรรมอ้างอิงและมาตรฐานที่รองรับ:
-------------------------------------------------------------------------------
1. Dolby Surround (ds) [Dressler, AES 1993] [E1/E2]:
   - เมทริกซ์แบบ Passive ดั้งเดิม
   - L = Lt, R = Rt, C = 0 (Phantom Center)
   - S = (Rt - Lt) / sqrt(2) (โมโนเซอร์ราวด์)
   - เซอร์ราวด์ผ่าน Low-pass Filter 7 kHz (Butterworth 4th-order) และดีเลย์ 20 ms
   - จ่ายออกแชนเนล Ls = Rs = S

2. Dolby Pro Logic (pl) [Dolby Laboratories / US 4,799,260 / US 7,280,664 B2] [E2]:
   - Active Steering แบบ Pro Logic ดั้งเดิม หรือ PL Emulation ผ่าน Fosgate Matrix
   - เซอร์ราวด์โมโน S ผ่านตัวกรอง LPF 7 kHz และดีเลย์ 10–20 ms (Ls = Rs = S)
   - ช่อง Center แยกอิสระสมบูรณ์ผ่านการหักล้าง Crosstalk ด้วย Steering Servo
   - รองรับทั้ง Fosgate PL Emulation และ Classic Feed-Forward Servo (US 4,799,260)

3. Dolby Pro Logic II Movie (pl2) [Fosgate S3 / US 7,280,664 B2] [E2]:
   - สถาปัตยกรรม Active Matrix Feedback Servo 2 แกน (LR และ FB)
   - ชุดสัมประสิทธิ์ Standard 5x2 ตามสิทธิบัตร Fosgate S3 (James W. Fosgate)
   - สเตอริโอเซอร์ราวด์ Ls, Rs แยกมิติอิสระสมบูรณ์ (Full-Range, ไม่ผ่าน LPF 7k)
   - ดีเลย์เซอร์ราวด์ 10 ms (ชดเชย Haas Precedence Effect)

4. Dolby Pro Logic II Music (pl2music) [Roger Dressler 2000 / Fosgate S3] [E2]:
   - สำหรับสัญญาณเสียงเพลงสเตอริโอ ไม่ผ่านดีเลย์เซอร์ราวด์ (Delay 0 ms)
   - พารามิเตอร์ควบคุมมาตรฐาน DPL2 Music Mode:
     * Center Width (0–7): ควบคุมการกระจายเสียงร้องระหว่าง Center กับ L/R
     * Dimension (-3 ถึง +3): ปรับสมดุลเวทีเสียงระหว่างด้านหน้ากับด้านหลัง
     * Panorama (เปิด/ปิด): ขยายสนามเสียงสเตอริโอด้านหน้าโอบล้อมเข้าสู่เซอร์ราวด์

การจัดชั้นความแม่นยำทางวิศวกรรม:
  [E1 ยืนยันได้]   : Passive Matrix, ลำดับแชนเนล WAV RF64 Extensible, Headroom & Peak Normalization
  [E2 เอกสารสาธารณะ]: วงจร Fosgate Servo Feedback, BPF 788-1878Hz, ค่าคงที่ A0-A4, B0-B5, ตารางสัมประสิทธิ์ 5x2
  [E3 พัฒนาขึ้นใหม่]: การชดเชย Negative Feedback ให้เสถียร, การสกัดเบส LFE Bass Redirection, Auto-Balance
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
import time
from dataclasses import asdict, dataclass, field
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

# ตรวจสอบ Optional Dependency: numba สำหรับเร่งความเร็วลูปตัวอย่าง (JIT)
try:
    import numba
    from numba import njit
    HAVE_NUMBA = True
except ImportError:
    HAVE_NUMBA = False
    def njit(*args, **kwargs):
        def decorator(func):
            return func
        return decorator


# ==============================================================================
# 1. ค่าคงที่และสเปกมาตรฐาน (Constants & Standards)
# ==============================================================================
VERSION = "1.0.0"
SAMPLE_RATE_TARGET = 48000
INV_SQRT2 = np.float64(1.0 / math.sqrt(2.0))  # ≈ 0.7071067811865475 (-3.01 dB)
ATTEN_3DB = np.float64(10.0 ** (-3.0 / 20.0))  # ≈ 0.7079457843841379 (-3.00 dB)

# Latency และ Hop Size
LOOKAHEAD_MS_DEFAULT = 5.0
LOOKAHEAD_SAMPLES_DEFAULT = int(SAMPLE_RATE_TARGET * (LOOKAHEAD_MS_DEFAULT / 1000.0))  # 240 samples @ 48kHz
DEFAULT_HOP_SIZE = 8

# WAVEFORMATEXTENSIBLE Channel Mask Constants
SPEAKER_FRONT_LEFT = 0x0001
SPEAKER_FRONT_RIGHT = 0x0002
SPEAKER_FRONT_CENTER = 0x0004
SPEAKER_LOW_FREQUENCY = 0x0008
SPEAKER_BACK_LEFT = 0x0010
SPEAKER_BACK_RIGHT = 0x0020
SPEAKER_SIDE_LEFT = 0x0200
SPEAKER_SIDE_RIGHT = 0x0400

# Channel Mask สำหรับ 5.1
MASK_51_SIDE = 0x060F  # FL | FR | FC | LFE | SL | SR
MASK_51_BACK = 0x003F  # FL | FR | FC | LFE | BL | BR

STANDARD_ROLES_51 = ("L", "R", "C", "LFE", "Ls", "Rs")

# ค่าคงที่สิทธิบัตร Fosgate S3 (US 7,280,664 B2) [E2]
FOSGATE_A0 = np.float64(0.707106781 * 0.000022)  # ≈ 1.5556349182e-5
FOSGATE_A1 = np.float64(3.182732 / 4.0)          # 0.795683
FOSGATE_A2 = np.float64(128.0)                   # 32 * 4
FOSGATE_A3 = np.float64(0.2375)                  # Directional steering sensitivity
FOSGATE_A4 = np.float64(-0.2400)                 # Quiescent offset

FOSGATE_B0 = np.float64(0.79)
FOSGATE_B1 = np.float64(1.451)
FOSGATE_B2 = np.float64(-0.15415)
FOSGATE_B3 = np.float64(-0.15415)
FOSGATE_B4 = np.float64(-0.21927 / 1.21927)      # ≈ -0.179838756
FOSGATE_B5 = np.float64(1.21927)

# ==============================================================================
# ตารางสัมประสิทธิ์เมทริกซ์ Fosgate S3 5x2 [E2]
# ==============================================================================
# Standard Coefficients (PL2 Movie และ PL2 Music)
M_STANDARD_FIX = np.array([
    [ 0.7400,  0.0000],  # L
    [ 0.5240,  0.5240],  # C
    [ 0.0000,  0.7400],  # R
    [ 0.7600, -0.1700],  # Ls
    [-0.1700,  0.7600],  # Rs
], dtype=np.float64)

M_STANDARD_GL = np.array([
    [ 0.3200,  0.0000],  # L
    [-0.5400,  0.0000],  # C
    [ 0.0000,  0.0000],  # R
    [-0.7720,  0.0000],  # Ls
    [ 0.1920,  0.0000],  # Rs
], dtype=np.float64)

M_STANDARD_GR = np.array([
    [ 0.0000,  0.0000],
    [ 0.0000, -0.5400],
    [ 0.0000,  0.3200],
    [ 0.0000,  0.1920],
    [ 0.0000, -0.7720],
], dtype=np.float64)

M_STANDARD_GF = np.array([
    [-0.3813, -0.3813],
    [ 0.2240,  0.2240],
    [-0.3813, -0.3813],
    [-0.2930, -0.2930],
    [-0.2930, -0.2930],
], dtype=np.float64)

M_STANDARD_GB = np.array([
    [-0.3849,  0.3849],
    [ 0.0000,  0.0000],
    [ 0.3849, -0.3849],
    [ 0.0697, -0.0697],
    [-0.0697,  0.0697],
], dtype=np.float64)

M_STANDARD_GLB = np.array([
    [-0.2850,  0.2850],
    [ 0.0000,  0.0000],
    [ 0.0000,  0.0000],
    [ 0.3510, -0.3510],
    [ 0.3700, -0.3700],
], dtype=np.float64)

M_STANDARD_GRB = np.array([
    [ 0.0000,  0.0000],
    [ 0.0000,  0.0000],
    [ 0.2850, -0.2850],
    [-0.3700,  0.3700],
    [-0.3510,  0.3510],
], dtype=np.float64)

# Pro Logic Emulation Coefficients (PL) [E2]
M_PL_FIX = np.array([
    [ 0.7400,  0.0000],
    [ 0.5240,  0.5240],
    [ 0.0000,  0.7400],
    [ 0.5370, -0.5370],
    [ 0.5370, -0.5370],
], dtype=np.float64)

M_PL_GL = np.array([
    [ 0.3200,  0.0000],
    [-0.5400,  0.0000],
    [ 0.0000,  0.0000],
    [-0.5460,  0.0000],
    [-0.5460,  0.0000],
], dtype=np.float64)

M_PL_GR = np.array([
    [ 0.0000,  0.0000],
    [ 0.0000, -0.5400],
    [ 0.0000,  0.3200],
    [ 0.0000,  0.5460],
    [ 0.0000,  0.5460],
], dtype=np.float64)

M_PL_GF = np.array([
    [-0.3811, -0.3811],
    [ 0.2250,  0.2250],
    [-0.3811, -0.3811],
    [ 0.0000,  0.0000],
    [ 0.0000,  0.0000],
], dtype=np.float64)

M_PL_GB = np.array([
    [-0.3811,  0.3811],
    [ 0.0000,  0.0000],
    [ 0.3811, -0.3811],
    [ 0.0000,  0.0000],
    [ 0.0000,  0.0000],
], dtype=np.float64)

M_PL_GLB = np.zeros((5, 2), dtype=np.float64)
M_PL_GRB = np.zeros((5, 2), dtype=np.float64)


# ==============================================================================
# 2. การประกาศ Enum และ Dataclass (Enums & Config)
# ==============================================================================
class MatrixDecoderError(Exception):
    """ข้อผิดพลาดเฉพาะของโมดูล Matrix Decoder พร้อมรหัสสถานะออก (Exit Code)"""
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


class MatrixMode(str, Enum):
    DS = "ds"
    PL = "pl"
    PL2 = "pl2"
    PL2MUSIC = "pl2music"


class SteeringEngine(str, Enum):
    FOSGATE = "fosgate"          # S3 Active Matrix Feedback Servo (ค่าเริ่มต้น)
    FEEDFORWARD = "feedforward"  # Pro Logic US 4,799,260 Feed-Forward Servo
    PASSIVE = "passive"          # เมทริกซ์คงที่ ล้วน (ไม่มี Steering)
    PAIRINV = "pairinv"          # Pair-Inverse Algebraic Solver


class ServoImpl(str, Enum):
    FAST = "fast"                # กรองบล็อก + Hop Interpolation (Pure Python/NumPy)
    EXACT = "exact"              # Sample-by-sample ภายใน Servo Loop (ใช้ Numba JIT หากมี)


class OutLayout(str, Enum):
    SIDE_51 = "5.1side"          # Mask 0x060F (FL, FR, FC, LFE, SL, SR)
    BACK_51 = "5.1back"          # Mask 0x003F (FL, FR, FC, LFE, BL, BR)


class LfeMode(str, Enum):
    SILENT = "silent"            # LFE เป็น 0.0 เงียบสนิท (ค่าเริ่มต้น)
    BASS = "bass"                # สกัดเบสจากคู่หน้าผ่าน Butterworth Low-Pass


class HeadroomMode(str, Enum):
    NONE = "none"
    STATIC = "static"
    PEAK = "peak"


@dataclass
class DecoderConfig:
    mode: MatrixMode = MatrixMode.PL2
    steering: bool = True
    steer_engine: SteeringEngine = SteeringEngine.FOSGATE
    servo_impl: ServoImpl = ServoImpl.FAST
    coef_set: str = "auto"       # 'auto', 'standard', 'pl-emulation'
    lookahead_ms: float = 5.0
    hop_size: int = DEFAULT_HOP_SIZE
    servo_env_tc_ms: float = 800.0
    servo_fast_tc_ms: float = 2.5
    servo_params: Dict[str, float] = field(default_factory=dict)
    
    # Music Mode Controls [E2]
    center_width: float = 3.0    # 0 (center only) to 7 (phantom L/R)
    dimension: float = 0.0       # -3 (rear) to +3 (front)
    panorama: bool = False       # Wrap front stereo to surrounds
    
    # Surround Processing
    surround_delay_ms: float = 10.0
    surround_lpf: str = "off"    # 'off', '7k'
    
    # Auto-Balance [E3]
    input_balance: str = "off"   # 'off', 'auto', or float dB
    
    # Audio Path & Gains
    makeup_gain_db: float = 0.0
    out_gain_db: float = 0.0
    trims: Dict[str, float] = field(default_factory=lambda: {r: 0.0 for r in STANDARD_ROLES_51})
    headroom: HeadroomMode = HeadroomMode.NONE
    
    # LFE
    lfe_mode: LfeMode = LfeMode.SILENT
    lfe_lp_hz: float = 120.0
    lfe_gain_db: float = 0.0
    lfe_redirect: bool = False
    
    # Output Layout & System
    out_layout: OutLayout = OutLayout.SIDE_51
    block_size: int = 65536
    ffmpeg_bin: Optional[str] = None


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
# 3. เครื่องมือค้นหาและ I/O Layer (WAV / Pipe Streaming)
# ==============================================================================
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

    cur_dir = Path(__file__).resolve().parent
    for sub in [cur_dir, cur_dir.parent, cur_dir.parent / "tools", cur_dir.parent / "bin"]:
        candidate = sub / (name + (".exe" if os.name == "nt" else ""))
        if candidate.is_file():
            return str(candidate)

    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt" and not name.lower().endswith(".exe"):
        found = shutil.which(name + ".exe")
        if found:
            return found
    return None


class WavStreamReader:
    """อ่านข้อมูลไฟล์ WAV/RF64/BW64 แบบสตรีมทีละบล็อก พร้อมแยกแยะ Header ละเอียด"""

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
            raise MatrixDecoderError("ไฟล์มีขนาดสั้นเกินกว่าจะเป็นไฟล์ WAV", 3)
        riff_tag = head[0:4]
        if riff_tag not in (b"RIFF", b"RF64", b"BW64"):
            raise MatrixDecoderError(f"ไม่ใช่ไฟล์ WAV/RF64 ที่ถูกต้อง (พบ Header: {riff_tag!r})", 3)
        if head[8:12] != b"WAVE":
            raise MatrixDecoderError(f"ไม่ใช่ฟอร์แมต WAVE (พบ: {head[8:12]!r})", 3)

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
                pad = csize & 1
                to_skip = csize + pad
                try:
                    self.f.seek(to_skip, io.SEEK_CUR)
                except Exception:
                    self._read_exact(to_skip)

        if not fmt_chunk or len(fmt_chunk) < 16:
            raise MatrixDecoderError("ไม่พบ fmt chunk ที่สมบูรณ์ในไฟล์ WAV", 3)
        if data_chunk_size is None:
            raise MatrixDecoderError("ไม่พบ data chunk ในไฟล์ WAV", 3)

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
            raise MatrixDecoderError(f"ไม่รองรับขนาดบิต {bits} บิต", 3)

        return arr.reshape(-1, self.info.channels)

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


class FFmpegStreamReader:
    """อ่านสัญญาณเสียงผ่าน FFmpeg Pipe แปลงเป็น float32 48kHz อัตโนมัติ ป้องกัน Deadlock บน stderr"""

    def __init__(self, proc: subprocess.Popen, info: StreamAudioInfo):
        self.proc = proc
        self.info = info
        self.frame_bytes = info.channels * 4
        self.stderr_lines: List[str] = []

        self.stderr_thread = threading.Thread(target=self._capture_stderr, daemon=True)
        self.stderr_thread.start()

    def _capture_stderr(self):
        if not self.proc.stderr:
            return
        try:
            for line in iter(self.proc.stderr.readline, b""):
                if line:
                    self.stderr_lines.append(line.decode("utf-8", errors="replace").strip())
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
        if rc != 0 and rc != -15:
            err_summary = "\n".join(self.stderr_lines[-10:])
            raise MatrixDecoderError(
                f"FFmpeg ทำงานล้มเหลว (Exit code {rc}):\n{err_summary}", 4
            )


class WavWriterF32:
    """เขียน Float32 5.1/6-Channel WAV พร้อมสลับเป็น RF64 อัตโนมัติหากขนาดไฟล์เกิน 4 GB"""

    def __init__(
        self,
        final_path: Path,
        sample_rate: int = SAMPLE_RATE_TARGET,
        channels: int = 6,
        channel_mask: int = MASK_51_SIDE,
    ):
        self.final_path = final_path
        self.part_path = final_path.parent / (final_path.name + ".part")
        self.sample_rate = sample_rate
        self.channels = channels
        self.channel_mask = channel_mask
        self.total_bytes_written = 0

        self.f = open(self.part_path, "wb")
        # สำรองพื้นที่ Header 128 ไบต์ เผื่อ ds64 และ fmt extensible
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
            cb_size = 22
            fmt_size = 40
            junk_size = self.header_reserve_size - 12 - (8 + fmt_size) - 8 - 8  # 52 bytes
            riff_size = 4 + (8 + fmt_size) + (8 + junk_size) + (8 + data_size)

            hdr = io.BytesIO()
            hdr.write(b"RIFF")
            hdr.write(struct.pack("<I", riff_size))
            hdr.write(b"WAVE")

            # fmt chunk (WAVEFORMATEXTENSIBLE)
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
            hdr.write(struct.pack("<I", self.channel_mask))
            # SubFormat GUID for KSDATAFORMAT_SUBTYPE_IEEE_FLOAT
            hdr.write(
                b"\x03\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
            )

            # JUNK chunk คั่นก่อน data chunk
            hdr.write(b"JUNK")
            hdr.write(struct.pack("<I", junk_size))
            hdr.write(b"\0" * junk_size)

            # data chunk header
            hdr.write(b"data")
            hdr.write(struct.pack("<I", data_size))

            self.f.write(hdr.getvalue())
        else:
            fmt_size = 40
            ds64_size = 28
            junk_size_rf64 = self.header_reserve_size - 12 - (8 + ds64_size) - (8 + fmt_size) - 8 - 8
            sample_count_64 = data_size // (self.channels * 4)

            hdr = io.BytesIO()
            hdr.write(b"RF64")
            hdr.write(struct.pack("<I", 0xFFFFFFFF))
            hdr.write(b"WAVE")

            # ds64 chunk
            hdr.write(b"ds64")
            hdr.write(struct.pack("<I", ds64_size))
            hdr.write(struct.pack("<Q", 4 + (8 + ds64_size) + (8 + fmt_size) + (8 + junk_size_rf64) + (8 + data_size)))
            hdr.write(struct.pack("<Q", data_size))
            hdr.write(struct.pack("<Q", sample_count_64))
            hdr.write(struct.pack("<I", 0))  # table length

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
            hdr.write(struct.pack("<I", self.channel_mask))
            hdr.write(
                b"\x03\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
            )

            # JUNK chunk
            if junk_size_rf64 > 0:
                hdr.write(b"JUNK")
                hdr.write(struct.pack("<I", junk_size_rf64))
                hdr.write(b"\0" * junk_size_rf64)

            # data chunk
            hdr.write(b"data")
            hdr.write(struct.pack("<I", 0xFFFFFFFF))

            self.f.write(hdr.getvalue())

        self.f.close()
        if self.final_path.exists():
            try:
                self.final_path.unlink()
            except Exception:
                pass
        self.part_path.rename(self.final_path)


# ==============================================================================
# 4. ตัวกรอง DSP และดีเลย์ไลน์ (Filters & Delay Lines)
# ==============================================================================
class DelayLine:
    """ดีเลย์ไลน์แบบ Circular Buffer รักษาความต่อเนื่องข้ามบล็อก 100%"""

    def __init__(self, delay_samples: int):
        self.delay_samples = max(0, int(delay_samples))
        self.buffer = np.zeros(self.delay_samples, dtype=np.float32)
        self.write_ptr = 0

    def process_block(self, signal_in: np.ndarray) -> np.ndarray:
        if self.delay_samples == 0:
            return signal_in.copy()

        n = len(signal_in)
        out = np.empty(n, dtype=np.float32)
        
        # ถอดสัญญาณที่หน่วงออกและเขียนตัวอย่างใหม่เข้า buffer
        for i in range(n):
            out[i] = self.buffer[self.write_ptr]
            self.buffer[self.write_ptr] = signal_in[i]
            self.write_ptr = (self.write_ptr + 1) % self.delay_samples
            
        return out

    def flush(self) -> np.ndarray:
        """ปล่อยสัญญาณที่ค้างใน buffer ออกมาตอนจบไฟล์"""
        if self.delay_samples == 0:
            return np.zeros(0, dtype=np.float32)
        out = np.empty(self.delay_samples, dtype=np.float32)
        for i in range(self.delay_samples):
            out[i] = self.buffer[self.write_ptr]
            self.buffer[self.write_ptr] = 0.0
            self.write_ptr = (self.write_ptr + 1) % self.delay_samples
        return out


def design_fosgate_bpf(fs: int = SAMPLE_RATE_TARGET) -> np.ndarray:
    """
    สร้างตัวกรอง BPF สำหรับ Control Path ตามสเปกสิทธิบัตร Fosgate S3 [E2]
    - High-pass: Zeros ที่ 0, 641 Hz; Poles ที่ 788, 1878 Hz
    - Low-pass: 2 Poles ที่ 13,466 Hz (Butterworth)
    """
    zeros_s = np.array([0.0, -2.0 * np.pi * 641.0])
    poles_s = np.array([-2.0 * np.pi * 788.0, -2.0 * np.pi * 1878.0])
    k_s = 1.0

    z_d, p_d, k_d = signal.bilinear_zpk(zeros_s, poles_s, k_s, fs)
    sos_hp = signal.zpk2sos(z_d, p_d, k_d)
    sos_lp = signal.butter(2, 13466.0, btype="low", fs=fs, output="sos")
    return np.vstack([sos_hp, sos_lp]).astype(np.float64)


class ContinuousSosFilter:
    """ตัวกรอง IIR SOS ที่เก็บรักษาเวกเตอร์สถานะ zi ข้ามบล็อกอย่างต่อเนื่อง"""

    def __init__(self, sos: np.ndarray):
        self.sos = sos
        self.zi = signal.sosfilt_zi(sos)

    def process_block(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x.copy()
        y, self.zi = signal.sosfilt(self.sos, x, zi=self.zi)
        return y.astype(np.float32)


# ==============================================================================
# 5. ฟังก์ชันเชิงตัวเลขแบบ JIT (Numba Acceleration Functions)
# ==============================================================================
@njit(fastmath=True)
def _pwl_transfer(u: float) -> float:
    """ฟังก์ชันไม่เชิงเส้นแบบ 3 ช่วงของ Fosgate S3 [E2]"""
    abs_u = abs(u)
    if abs_u < 0.240714:
        return 2.871432 * u
    elif u >= 0.240714:
        return 0.406707 * u + 0.593293
    else:
        return 0.406707 * u - 0.593293


@njit(fastmath=True)
def _compute_servo_sample_loop(
    lt_bpf: np.ndarray,
    rt_bpf: np.ndarray,
    e_state: np.ndarray,      # [e_L, e_R, e_F, e_B]
    fast_state: np.ndarray,   # [LR, FB]
    alpha_800: float,
    alpha_2_5: float,
    a0: float,
    a1: float,
    a2: float,
    a3: float,
    a4: float,
    b0: float,
    b1: float,
    b2: float,
    b3: float,
    b4: float,
    b5: float,
    hop_size: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """ลูปตัวอย่างคำนวณ Servo สำหรับโหมด Exact หรือ Fast Hop พร้อมเร่งด้วย Numba"""
    n_samples = len(lt_bpf)
    n_hops = n_samples // hop_size
    hop_gains = np.empty((n_hops, 6), dtype=np.float64)

    e_L = e_state[0]
    e_R = e_state[1]
    e_F = e_state[2]
    e_B = e_state[3]
    LR = fast_state[0]
    FB = fast_state[1]

    idx = 0
    for h in range(n_hops):
        # รันสะสมตัวอย่างภายใน Hop
        for _ in range(hop_size):
            x_L = abs(lt_bpf[idx])
            x_R = abs(rt_bpf[idx])
            ft_val = (lt_bpf[idx] + rt_bpf[idx]) * 0.5
            bt_val = (rt_bpf[idx] - lt_bpf[idx]) * 0.5
            x_F = abs(ft_val)
            x_B = abs(bt_val)

            e_L = alpha_800 * e_L + (1.0 - alpha_800) * x_L
            e_R = alpha_800 * e_R + (1.0 - alpha_800) * x_R
            e_F = alpha_800 * e_F + (1.0 - alpha_800) * x_F
            e_B = alpha_800 * e_B + (1.0 - alpha_800) * x_B
            idx += 1

        # คำนวณแกน LR
        l_L = a1 * math.log2(max(a0 * e_L, 1e-12))
        l_R = a1 * math.log2(max(a0 * e_R, 1e-12))
        d_LR = l_L - l_R
        v_LR = _pwl_transfer(a2 * d_LR)
        LR = alpha_2_5 * LR + (1.0 - alpha_2_5) * v_LR

        # คำนวณแกน FB
        l_F = a1 * math.log2(max(a0 * e_F, 1e-12))
        l_B = a1 * math.log2(max(a0 * e_B, 1e-12))
        d_FB = l_F - l_B
        v_FB = _pwl_transfer(a2 * d_FB)
        FB = alpha_2_5 * FB + (1.0 - alpha_2_5) * v_FB

        # คำนวณเกน 4 ทิศทาง (gL, gR, gF, gB)
        # เมื่อ Left เด่น (LR > 0), gL เพิ่มขึ้นไปสู่ 1.0 เพื่อหักล้าง Crosstalk
        gL = min(max(2.0 ** (a4 + a3 * LR), 0.0), 1.0) if LR > 0 else 0.0
        gR = min(max(2.0 ** (a4 - a3 * LR), 0.0), 1.0) if LR < 0 else 0.0
        gF = min(max(2.0 ** (a4 + a3 * FB), 0.0), 1.0) if FB > 0 else 0.0
        gB = min(max(2.0 ** (a4 - a3 * FB), 0.0), 1.0) if FB < 0 else 0.0

        # Hemispherical Gating: เกน Front Left/Right จะทำงานเฉพาะเมื่อทิศทางไม่อยู่ในซีกหลัง
        front_gate = max(0.0, 1.0 - gB)
        gL *= front_gate
        gR *= front_gate

        # ลอจิกเซอร์ราวด์ด้านหลัง (gLB, gRB)
        p1 = max(-LR, b0 * FB)
        p2 = max(LR, b0 * FB)
        q1 = min(0.0, p1 * b1)
        q2 = min(0.0, p2 * b1)
        r1 = q1 * b2 + b3
        r2 = q2 * b2 + b3
        raw_lb = (2.0 ** r1 + b4) * b5
        raw_rb = (2.0 ** r2 + b4) * b5
        g_rest = (2.0 ** b3 + b4) * b5
        norm_scale = max(1.0 - g_rest, 1e-6)
        gLB = min(max((raw_lb - g_rest) / norm_scale, 0.0), 1.0) * gB
        gRB = min(max((raw_rb - g_rest) / norm_scale, 0.0), 1.0) * gB

        hop_gains[h, 0] = gL
        hop_gains[h, 1] = gR
        hop_gains[h, 2] = gF
        hop_gains[h, 3] = gB
        hop_gains[h, 4] = gLB
        hop_gains[h, 5] = gRB

    new_e = np.array([e_L, e_R, e_F, e_B], dtype=np.float64)
    new_fast = np.array([LR, FB], dtype=np.float64)
    return hop_gains, new_e, new_fast


# ==============================================================================
# 6. Fosgate Steering Servo & Adaptive Matrix (Core Processing Engine)
# ==============================================================================
class FosgateServoControlPath:
    """วงจรประมวลผลเส้นทางควบคุม Feedback Servo ของ Fosgate S3 [E2]"""

    def __init__(self, config: DecoderConfig, fs: int = SAMPLE_RATE_TARGET):
        self.config = config
        self.fs = fs
        self.hop_size = config.hop_size

        # สร้างตัวกรอง BPF สำหรับ Control Path
        sos_bpf = design_fosgate_bpf(fs)
        self.bpf_lt = ContinuousSosFilter(sos_bpf)
        self.bpf_rt = ContinuousSosFilter(sos_bpf)

        # สัมประสิทธิ์การเฉลี่ย IIR
        self.alpha_800 = math.exp(-1.0 / ((config.servo_env_tc_ms / 1000.0) * fs))
        self.alpha_2_5 = math.exp(-float(self.hop_size) / ((config.servo_fast_tc_ms / 1000.0) * fs))

        # ค่าคงที่สิทธิบัตร พร้อมรองรับการ override ผ่าน config
        self.a0 = float(config.servo_params.get("a0", FOSGATE_A0))
        self.a1 = float(config.servo_params.get("a1", FOSGATE_A1))
        self.a2 = float(config.servo_params.get("a2", FOSGATE_A2))
        self.a3 = float(config.servo_params.get("a3", FOSGATE_A3))
        self.a4 = float(config.servo_params.get("a4", FOSGATE_A4))
        self.b0 = float(config.servo_params.get("b0", FOSGATE_B0))
        self.b1 = float(config.servo_params.get("b1", FOSGATE_B1))
        self.b2 = float(config.servo_params.get("b2", FOSGATE_B2))
        self.b3 = float(config.servo_params.get("b3", FOSGATE_B3))
        self.b4 = float(config.servo_params.get("b4", FOSGATE_B4))
        self.b5 = float(config.servo_params.get("b5", FOSGATE_B5))

        # เวกเตอร์สถานะสำหรับความต่อเนื่องข้ามบล็อก
        self.e_state = np.zeros(4, dtype=np.float64)       # [e_L, e_R, e_F, e_B]
        self.fast_state = np.zeros(2, dtype=np.float64)    # [LR, FB]
        self.last_hop_gain = np.zeros(6, dtype=np.float64) # สำหรับ Interpolation ต่อหัวบล็อก

        # เศษตัวอย่างที่เหลือท้ายบล็อก
        self.rem_lt = np.zeros(0, dtype=np.float32)
        self.rem_rt = np.zeros(0, dtype=np.float32)

    def process_block(self, lt_prime: np.ndarray, rt_prime: np.ndarray) -> np.ndarray:
        """
        ประมวลผลสัญญาณบล็อกและส่งกลับเมทริกซ์เกน Interpolated ขนาด (N_samples, 6)
        คอลัมน์: [gL, gR, gF, gB, gLB, gRB]
        """
        n_samples = len(lt_prime)
        if n_samples == 0:
            return np.zeros((0, 6), dtype=np.float64)

        # 1. รวมเศษตัวอย่างจากบล็อกก่อนหน้า
        if self.rem_lt.size > 0:
            lt_full = np.concatenate([self.rem_lt, lt_prime])
            rt_full = np.concatenate([self.rem_rt, rt_prime])
        else:
            lt_full = lt_prime
            rt_full = rt_prime

        # 2. กรองผ่าน Control BPF
        lt_bpf = self.bpf_lt.process_block(lt_full)
        rt_bpf = self.bpf_rt.process_block(rt_full)

        total_avail = len(lt_bpf)
        n_hops = total_avail // self.hop_size
        usable_samples = n_hops * self.hop_size

        # เก็บเศษตัวอย่างไว้สำหรับบล็อกถัดไป
        rem_count = total_avail - usable_samples
        if rem_count > 0:
            self.rem_lt = lt_full[-rem_count:].copy()
            self.rem_rt = rt_full[-rem_count:].copy()
        else:
            self.rem_lt = np.zeros(0, dtype=np.float32)
            self.rem_rt = np.zeros(0, dtype=np.float32)

        if n_hops == 0:
            # กรณีบล็อกสั้นกว่า hop size ให้คงเกนเดิม
            return np.repeat(self.last_hop_gain[np.newaxis, :], n_samples, axis=0)

        # 3. คำนวณ Servo Hop Gains ด้วย JIT / Vectorized Loop
        hop_gains, self.e_state, self.fast_state = _compute_servo_sample_loop(
            lt_bpf[:usable_samples],
            rt_bpf[:usable_samples],
            self.e_state,
            self.fast_state,
            self.alpha_800,
            self.alpha_2_5,
            self.a0,
            self.a1,
            self.a2,
            self.a3,
            self.a4,
            self.b0,
            self.b1,
            self.b2,
            self.b3,
            self.b4,
            self.b5,
            self.hop_size,
        )

        # 4. Interpolate เกนแบบเส้นตรง (Linear Interpolation) ระหว่าง Hop
        # เพื่อป้องกัน Zipper Noise อย่างสมบูรณ์
        all_hops = np.vstack([self.last_hop_gain[np.newaxis, :], hop_gains])
        self.last_hop_gain = hop_gains[-1].copy()

        # สร้างพิกัดตัวอย่างสำหรับ interpolation
        x_knots = np.arange(n_hops + 1) * self.hop_size
        x_targets = np.arange(usable_samples)
        
        sample_gains = np.empty((usable_samples, 6), dtype=np.float64)
        for col in range(6):
            sample_gains[:, col] = np.interp(x_targets, x_knots, all_hops[:, col])

        # ปรับความยาวให้ตรงกับ n_samples ของอินพุตบล็อกนี้พอดี
        if usable_samples >= n_samples:
            return sample_gains[:n_samples]
        else:
            # กรณีมีเศษตัวอย่างค้าง ให้ยืดแถวสุดท้ายชดเชย
            pad_count = n_samples - usable_samples
            pad = np.repeat(sample_gains[-1:, :], pad_count, axis=0)
            return np.vstack([sample_gains, pad])


class FeedForwardServoControlPath:
    """วงจรประมวลผล Feed-Forward Servo ตามสิทธิบัตร Pro Logic ดั้งเดิม (US 4,799,260) [E2]"""

    def __init__(self, config: DecoderConfig, fs: int = SAMPLE_RATE_TARGET):
        self.config = config
        self.fs = fs
        self.hop_size = config.hop_size
        self.alpha_fast = math.exp(-float(self.hop_size) / ((config.servo_fast_tc_ms / 1000.0) * fs))
        self.alpha_env = math.exp(-1.0 / ((config.servo_env_tc_ms / 1000.0) * fs))
        self.e_state = np.zeros(4, dtype=np.float64)
        self.last_gain = np.zeros(6, dtype=np.float64)

    def process_block(self, lt: np.ndarray, rt: np.ndarray) -> np.ndarray:
        n = len(lt)
        if n == 0:
            return np.zeros((0, 6), dtype=np.float64)
        
        ft = (lt + rt) * 0.5
        bt = (rt - lt) * 0.5

        # คำนวณ Dominance ระดับบล็อก
        rms_L = float(np.sqrt(np.mean(lt ** 2) + 1e-12))
        rms_R = float(np.sqrt(np.mean(rt ** 2) + 1e-12))
        rms_F = float(np.sqrt(np.mean(ft ** 2) + 1e-12))
        rms_B = float(np.sqrt(np.mean(bt ** 2) + 1e-12))

        # คำนวณความต่าง Logarithmic
        d_LR = 20.0 * math.log10(rms_L / rms_R)
        d_FB = 20.0 * math.log10(rms_F / rms_B)

        gL = min(max(d_LR / 12.0, 0.0), 1.0) if d_LR > 0 else 0.0
        gR = min(max(-d_LR / 12.0, 0.0), 1.0) if d_LR < 0 else 0.0
        gF = min(max(d_FB / 12.0, 0.0), 1.0) if d_FB > 0 else 0.0
        gB = min(max(-d_FB / 12.0, 0.0), 1.0) if d_FB < 0 else 0.0

        target_gain = np.array([gL, gR, gF, gB, 0.0, 0.0], dtype=np.float64)
        
        # Smooth ข้ามบล็อก
        smoothed_gain = self.alpha_fast * self.last_gain + (1.0 - self.alpha_fast) * target_gain
        self.last_gain = smoothed_gain.copy()

        return np.repeat(smoothed_gain[np.newaxis, :], n, axis=0)


class FosgateAdaptiveMatrix:
    """เมทริกซ์ 5x2 ที่แปรผันตามเกนควบคุมของ Servo (Standard หรือ PL-Emulation) [E2]"""

    def __init__(self, coef_set: str = "standard"):
        self.coef_set = coef_set
        if coef_set == "pl-emulation":
            self.M_fix = M_PL_FIX
            self.M_gL = M_PL_GL
            self.M_gR = M_PL_GR
            self.M_gF = M_PL_GF
            self.M_gB = M_PL_GB
            self.M_gLB = M_PL_GLB
            self.M_gRB = M_PL_GRB
        else:
            self.M_fix = M_STANDARD_FIX
            self.M_gL = M_STANDARD_GL
            self.M_gR = M_STANDARD_GR
            self.M_gF = M_STANDARD_GF
            self.M_gB = M_STANDARD_GB
            self.M_gLB = M_STANDARD_GLB
            self.M_gRB = M_STANDARD_GRB

    def apply(self, lt: np.ndarray, rt: np.ndarray, gains: np.ndarray) -> np.ndarray:
        """
        คูณสัญญาณอินพุต Lt, Rt ด้วยเมทริกซ์แปรผัน M(n)
        lt, rt: เวกเตอร์ 1D ขนาด (N,)
        gains: ขนาด (N, 6) -> [gL, gR, gF, gB, gLB, gRB]
        ส่งกลับ: อาเรย์ 2D ขนาด (N, 5) -> [L, C, R, Ls, Rs]
        """
        n = len(lt)
        if n == 0:
            return np.zeros((0, 5), dtype=np.float32)

        # 1. ส่วนประกอบเมทริกซ์คงที่ M_fix
        # out = [Lt, Rt] @ M_fix.T
        out = np.empty((n, 5), dtype=np.float64)
        out[:, 0] = self.M_fix[0, 0] * lt + self.M_fix[0, 1] * rt  # L
        out[:, 1] = self.M_fix[1, 0] * lt + self.M_fix[1, 1] * rt  # C
        out[:, 2] = self.M_fix[2, 0] * lt + self.M_fix[2, 1] * rt  # R
        out[:, 3] = self.M_fix[3, 0] * lt + self.M_fix[3, 1] * rt  # Ls
        out[:, 4] = self.M_fix[4, 0] * lt + self.M_fix[4, 1] * rt  # Rs

        # 2. บวกส่วนแปรผันตามเกน g_k แต่ละตัว
        if gains.size > 0:
            gL = gains[:, 0]
            gR = gains[:, 1]
            gF = gains[:, 2]
            gB = gains[:, 3]
            gLB = gains[:, 4]
            gRB = gains[:, 5]

            # gL
            out[:, 0] += gL * (self.M_gL[0, 0] * lt + self.M_gL[0, 1] * rt)
            out[:, 1] += gL * (self.M_gL[1, 0] * lt + self.M_gL[1, 1] * rt)
            out[:, 3] += gL * (self.M_gL[3, 0] * lt + self.M_gL[3, 1] * rt)
            out[:, 4] += gL * (self.M_gL[4, 0] * lt + self.M_gL[4, 1] * rt)

            # gR
            out[:, 1] += gR * (self.M_gR[1, 0] * lt + self.M_gR[1, 1] * rt)
            out[:, 2] += gR * (self.M_gR[2, 0] * lt + self.M_gR[2, 1] * rt)
            out[:, 3] += gR * (self.M_gR[3, 0] * lt + self.M_gR[3, 1] * rt)
            out[:, 4] += gR * (self.M_gR[4, 0] * lt + self.M_gR[4, 1] * rt)

            # gF
            out[:, 0] += gF * (self.M_gF[0, 0] * lt + self.M_gF[0, 1] * rt)
            out[:, 1] += gF * (self.M_gF[1, 0] * lt + self.M_gF[1, 1] * rt)
            out[:, 2] += gF * (self.M_gF[2, 0] * lt + self.M_gF[2, 1] * rt)
            out[:, 3] += gF * (self.M_gF[3, 0] * lt + self.M_gF[3, 1] * rt)
            out[:, 4] += gF * (self.M_gF[4, 0] * lt + self.M_gF[4, 1] * rt)

            # gB
            out[:, 0] += gB * (self.M_gB[0, 0] * lt + self.M_gB[0, 1] * rt)
            out[:, 2] += gB * (self.M_gB[2, 0] * lt + self.M_gB[2, 1] * rt)
            out[:, 3] += gB * (self.M_gB[3, 0] * lt + self.M_gB[3, 1] * rt)
            out[:, 4] += gB * (self.M_gB[4, 0] * lt + self.M_gB[4, 1] * rt)

            # gLB
            out[:, 0] += gLB * (self.M_gLB[0, 0] * lt + self.M_gLB[0, 1] * rt)
            out[:, 3] += gLB * (self.M_gLB[3, 0] * lt + self.M_gLB[3, 1] * rt)
            out[:, 4] += gLB * (self.M_gLB[4, 0] * lt + self.M_gLB[4, 1] * rt)

            # gRB
            out[:, 2] += gRB * (self.M_gRB[2, 0] * lt + self.M_gRB[2, 1] * rt)
            out[:, 3] += gRB * (self.M_gRB[3, 0] * lt + self.M_gRB[3, 1] * rt)
            out[:, 4] += gRB * (self.M_gRB[4, 0] * lt + self.M_gRB[4, 1] * rt)

        return out.astype(np.float32)


# ==============================================================================
# 7. วงจรปรับสมดุลและโมดูล Music Mode (Auto-Balance & Music Processing)
# ==============================================================================
class AutoBalanceProcessor:
    """ระบบตรวจจับและชดเชยสมดุลระดับเสียง Lt/Rt (--input-balance auto/dB) [E3]"""

    def __init__(self, mode_str: str, fs: int = SAMPLE_RATE_TARGET):
        self.mode_str = mode_str.lower().strip()
        self.fs = fs
        self.static_gain_l = 1.0
        self.static_gain_r = 1.0

        if self.mode_str not in ("off", "auto"):
            # กำหนดเป็นค่า dB คงที่ เช่น "+1.5" หรือ "-2.0"
            try:
                db_val = float(self.mode_str)
                # ค่าบวกคือ Lt ดังกว่า Rt -> ชดเชยโดยลด Lt เพิ่ม Rt
                self.static_gain_l = 10.0 ** (-db_val / 40.0)
                self.static_gain_r = 10.0 ** (+db_val / 40.0)
            except ValueError:
                raise MatrixDecoderError(f"ค่า --input-balance ({mode_str}) ไม่ถูกต้อง", 2)

        # เวลาเฉลี่ย 2.5 วินาที สำหรับโหมด adaptive auto
        self.alpha_bal = math.exp(-1.0 / (2.5 * fs))
        self.rms_l = 1e-4
        self.rms_r = 1e-4

    def process_block(self, lt: np.ndarray, rt: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        if self.mode_str == "off":
            return lt, rt

        if self.mode_str != "auto":
            return lt * np.float32(self.static_gain_l), rt * np.float32(self.static_gain_r)

        # โหมด Auto: คำนวณความต่าง RMS ในช่วงเสียงที่มีพลังงาน
        cur_rms_l = float(np.sqrt(np.mean(lt ** 2) + 1e-12))
        cur_rms_r = float(np.sqrt(np.mean(rt ** 2) + 1e-12))

        # ตรวจจับเฉพาะช่วงที่สัญญาณสูงกว่า -50 dBFS
        if cur_rms_l > 0.003 and cur_rms_r > 0.003:
            self.rms_l = self.alpha_bal * self.rms_l + (1.0 - self.alpha_bal) * cur_rms_l
            self.rms_r = self.alpha_bal * self.rms_r + (1.0 - self.alpha_bal) * cur_rms_r

        imb_db = 20.0 * math.log10(max(self.rms_l / self.rms_r, 1e-6))
        # จำกัดการชดเชยสูงสุดไม่เกิน ±3 dB เพื่อความปลอดภัย
        clamped_db = min(max(imb_db, -3.0), 3.0)

        corr_l = 10.0 ** (-clamped_db / 40.0)
        corr_r = 10.0 ** (+clamped_db / 40.0)

        return lt * np.float32(corr_l), rt * np.float32(corr_r)


def apply_music_mode_controls(
    channels_5: np.ndarray,
    center_width: float = 3.0,
    dimension: float = 0.0,
    panorama: bool = False,
) -> np.ndarray:
    """
    ประยุกต์พารามิเตอร์ของ Dolby Pro Logic II Music Mode [E2]:
    1. Center Width (0–7): แบ่งเสียงร้อง Center เข้าคู่หน้า L/R แบบ Constant Power
    2. Dimension (-3 ถึง +3): ปรับสมดุลระหว่าง Front Channels กับ Surround Channels
    3. Panorama (เปิด/ปิด): ขยายเวทีเสียงคู่หน้าโอบล้อมเข้าเซอร์ราวด์
    channels_5: อาเรย์ (N, 5) -> [L, C, R, Ls, Rs]
    """
    out = channels_5.copy()
    L = out[:, 0]
    C = out[:, 1]
    R = out[:, 2]
    Ls = out[:, 3]
    Rs = out[:, 4]

    # 1. Center Width Control (Constant-Power Split)
    # w = cw / 7.0 (0.0 ถึง 1.0)
    # C_out = C * sqrt(1 - w)
    # L_out = L + C * sqrt(w / 2)
    # R_out = R + C * sqrt(w / 2)
    cw_clamped = min(max(center_width, 0.0), 7.0)
    w = cw_clamped / 7.0
    if w > 0.0:
        c_scale = math.sqrt(1.0 - w)
        lr_bleed = math.sqrt(w / 2.0)
        out[:, 0] = L + np.float32(lr_bleed) * C
        out[:, 2] = R + np.float32(lr_bleed) * C
        out[:, 1] = C * np.float32(c_scale)

    # 2. Dimension Control (-3 ถึง +3)
    # แต่ละสเต็ปปรับเกน 1.0 dB
    dim_clamped = min(max(dimension, -3.0), 3.0)
    if dim_clamped != 0.0:
        front_gain = np.float32(10.0 ** (dim_clamped * 1.0 / 20.0))
        surr_gain = np.float32(10.0 ** (-dim_clamped * 1.0 / 20.0))
        out[:, 0] *= front_gain
        out[:, 1] *= front_gain
        out[:, 2] *= front_gain
        out[:, 3] *= surr_gain
        out[:, 4] *= surr_gain

    # 3. Panorama Control
    # ห่อเสียง L/R จากคู่หน้าเข้า Ls/Rs
    if panorama:
        pano_factor = np.float32(INV_SQRT2 * 0.5)  # -6.0 dB
        out[:, 3] += pano_factor * out[:, 0]
        out[:, 4] += pano_factor * out[:, 2]

    return out


# ==============================================================================
# 8. ไปป์ไลน์ DSP หลัก (Complete DSP Pipeline)
# ==============================================================================
class DecoderDSPPipeline:
    """ไปป์ไลน์ DSP รวมจัดการ Signal Flow, Delay Lines, Filters, Adaptive Matrix และ LFE [E1/E2/E3]"""

    def __init__(self, config: DecoderConfig, fs: int = SAMPLE_RATE_TARGET):
        self.config = config
        self.fs = fs

        # 1. Input Balance
        self.auto_balancer = AutoBalanceProcessor(config.input_balance, fs)

        # 2. Lookahead Delay สำหรับ Audio Path (5.0 ms = 240 ตัวอย่าง)
        self.lookahead_samples = int(round(fs * (config.lookahead_ms / 1000.0)))
        self.audio_delay_lt = DelayLine(self.lookahead_samples)
        self.audio_delay_rt = DelayLine(self.lookahead_samples)

        # 3. Steering Servo Engine
        if not config.steering or config.steer_engine == SteeringEngine.PASSIVE:
            self.servo_path = None
        elif config.steer_engine == SteeringEngine.FEEDFORWARD:
            self.servo_path = FeedForwardServoControlPath(config, fs)
        else:
            self.servo_path = FosgateServoControlPath(config, fs)

        # 4. Adaptive Matrix
        coef_set = config.coef_set
        if coef_set == "auto":
            coef_set = "pl-emulation" if config.mode == MatrixMode.PL else "standard"
        self.matrix = FosgateAdaptiveMatrix(coef_set)

        # 5. Surround Delay Line และ Filter
        surr_delay_samples = int(round(fs * (config.surround_delay_ms / 1000.0)))
        self.surr_delay_ls = DelayLine(surr_delay_samples)
        self.surr_delay_rs = DelayLine(surr_delay_samples)

        # Surround 7 kHz Low-Pass Filter
        self.surr_filter_ls = None
        self.surr_filter_rs = None
        if config.surround_lpf == "7k":
            sos_7k = signal.butter(4, 7000.0, btype="low", fs=fs, output="sos")
            self.surr_filter_ls = ContinuousSosFilter(sos_7k)
            self.surr_filter_rs = ContinuousSosFilter(sos_7k)

        # 6. LFE Processor (Bass Redirection)
        self.lfe_filter = None
        self.lfe_highpass = None
        if config.lfe_mode == LfeMode.BASS:
            sos_lfe = signal.butter(4, config.lfe_lp_hz, btype="low", fs=fs, output="sos")
            self.lfe_filter = ContinuousSosFilter(sos_lfe)
            if config.lfe_redirect:
                sos_hp = signal.butter(4, config.lfe_lp_hz, btype="high", fs=fs, output="sos")
                self.lfe_hp_l = ContinuousSosFilter(sos_hp)
                self.lfe_hp_c = ContinuousSosFilter(sos_hp)
                self.lfe_hp_r = ContinuousSosFilter(sos_hp)

        # 7. Channel Trims & Makeup Gain
        self.makeup_scale = np.float32(10.0 ** (config.makeup_gain_db / 20.0))
        self.master_scale = np.float32(10.0 ** (config.out_gain_db / 20.0))
        self.trim_scales = {
            role: np.float32(10.0 ** (config.trims.get(role, 0.0) / 20.0))
            for role in STANDARD_ROLES_51
        }

    def process_block(self, block_2ch: np.ndarray, headroom_scale: float = 1.0) -> np.ndarray:
        """
        ประมวลผลบล็อก 2 แชนเนล [Lt, Rt] ส่งออกเป็น 6 แชนเนล [FL, FR, FC, LFE, SL, SR]
        """
        n_samples = block_2ch.shape[0]
        if n_samples == 0:
            return np.zeros((0, 6), dtype=np.float32)

        lt_raw = block_2ch[:, 0]
        rt_raw = block_2ch[:, 1]

        # 1. Auto-Balance
        lt_bal, rt_bal = self.auto_balancer.process_block(lt_raw, rt_raw)

        # 2. ลดเกน -3.0 dB ป้องกัน Clipping ภายใน Matrix [E2]
        lt_prime = (lt_bal * np.float32(ATTEN_3DB)).astype(np.float32)
        rt_prime = (rt_bal * np.float32(ATTEN_3DB)).astype(np.float32)

        # 3. Audio Path Delay (5.0 ms)
        lt_audio = self.audio_delay_lt.process_block(lt_prime)
        rt_audio = self.audio_delay_rt.process_block(rt_prime)

        # 4. คำนวณเกน Steering Servo (Control Path)
        if self.servo_path is not None:
            gains = self.servo_path.process_block(lt_prime, rt_prime)
        else:
            gains = np.zeros((0, 6), dtype=np.float64)

        # 5. โหมด Dolby Surround (ds) Passive
        if self.config.mode == MatrixMode.DS:
            out_5 = np.empty((n_samples, 5), dtype=np.float32)
            out_5[:, 0] = lt_audio                          # L
            out_5[:, 1] = 0.0                               # C (Phantom)
            out_5[:, 2] = rt_audio                          # R
            s_mono = (rt_audio - lt_audio) * np.float32(INV_SQRT2)
            out_5[:, 3] = s_mono                            # Ls
            out_5[:, 4] = s_mono                            # Rs
        else:
            # 6. Fosgate Adaptive Matrix Multiplier (5x2)
            out_5 = self.matrix.apply(lt_audio, rt_audio, gains)

        # 7. เซอร์ราวด์ LPF 7 kHz (สำหรับ ds / pl)
        if self.surr_filter_ls and self.surr_filter_rs:
            out_5[:, 3] = self.surr_filter_ls.process_block(out_5[:, 3])
            out_5[:, 4] = self.surr_filter_rs.process_block(out_5[:, 4])

        # 8. ดีเลย์เซอร์ราวด์ (Surround Delay Line)
        out_5[:, 3] = self.surr_delay_ls.process_block(out_5[:, 3])
        out_5[:, 4] = self.surr_delay_rs.process_block(out_5[:, 4])

        # 9. Music Mode Controls (Center Width, Dimension, Panorama)
        if self.config.mode == MatrixMode.PL2MUSIC:
            out_5 = apply_music_mode_controls(
                out_5,
                center_width=self.config.center_width,
                dimension=self.config.dimension,
                panorama=self.config.panorama,
            )

        # 10. การประมวลผลช่อง LFE (Bass Redirection)
        lfe_channel = np.zeros(n_samples, dtype=np.float32)
        if self.config.lfe_mode == LfeMode.BASS and self.lfe_filter:
            mono_bass = 0.5 * (out_5[:, 0] + out_5[:, 2])
            lfe_filtered = self.lfe_filter.process_block(mono_bass)
            lfe_gain = np.float32(10.0 ** (self.config.lfe_gain_db / 20.0))
            lfe_channel = lfe_filtered * lfe_gain

            if self.config.lfe_redirect:
                out_5[:, 0] = self.lfe_hp_l.process_block(out_5[:, 0])
                out_5[:, 1] = self.lfe_hp_c.process_block(out_5[:, 1])
                out_5[:, 2] = self.lfe_hp_r.process_block(out_5[:, 2])

        # 11. รวบรวม 6 แชนเนล [FL, FR, FC, LFE, SL, SR]
        out_6ch = np.empty((n_samples, 6), dtype=np.float32)
        out_6ch[:, 0] = out_5[:, 0]       # FL (L)
        out_6ch[:, 1] = out_5[:, 2]       # FR (R)
        out_6ch[:, 2] = out_5[:, 1]       # FC (C)
        out_6ch[:, 3] = lfe_channel       # LFE
        out_6ch[:, 4] = out_5[:, 3]       # SL / BL (Ls)
        out_6ch[:, 5] = out_5[:, 4]       # SR / BR (Rs)

        # 12. ใช้ตัวคูณ Channel Trim, Makeup Gain, Master Gain และ Headroom
        total_scale = self.makeup_scale * self.master_scale * np.float32(headroom_scale)
        out_6ch[:, 0] *= self.trim_scales["L"] * total_scale
        out_6ch[:, 1] *= self.trim_scales["R"] * total_scale
        out_6ch[:, 2] *= self.trim_scales["C"] * total_scale
        out_6ch[:, 3] *= self.trim_scales["LFE"] * total_scale
        out_6ch[:, 4] *= self.trim_scales["Ls"] * total_scale
        out_6ch[:, 5] *= self.trim_scales["Rs"] * total_scale

        return out_6ch

    def flush(self, headroom_scale: float = 1.0) -> np.ndarray:
        """ปล่อยสัญญาณค้างใน Delay Lines ตอนจบไฟล์ เพื่อให้จำนวนตัวอย่างเอาต์พุตเท่ากับอินพุตเป๊ะ [E1]"""
        flush_lt = self.audio_delay_lt.flush()
        flush_rt = self.audio_delay_rt.flush()
        n = len(flush_lt)
        if n == 0:
            return np.zeros((0, 6), dtype=np.float32)

        # ประมวลผลบล็อก Flush ผ่านเมทริกซ์คงที่
        zeros_gains = np.zeros((0, 6), dtype=np.float64)
        out_5 = self.matrix.apply(flush_lt, flush_rt, zeros_gains)

        out_6ch = np.empty((n, 6), dtype=np.float32)
        out_6ch[:, 0] = out_5[:, 0]
        out_6ch[:, 1] = out_5[:, 2]
        out_6ch[:, 2] = out_5[:, 1]
        out_6ch[:, 3] = 0.0
        out_6ch[:, 4] = self.surr_delay_ls.process_block(out_5[:, 3])
        out_6ch[:, 5] = self.surr_delay_rs.process_block(out_5[:, 4])

        total_scale = self.makeup_scale * self.master_scale * np.float32(headroom_scale)
        for i, role in enumerate(["L", "R", "C", "LFE", "Ls", "Rs"]):
            out_6ch[:, i] *= self.trim_scales[role] * total_scale

        return out_6ch


# ==============================================================================
# 9. คลาสควบคุมวงจรการทำงานและการจัดการไฟล์ (Pipeline Runner)
# ==============================================================================
class DecoderPipeline:
    """ตัวควบคุมวงรอบการทำงานหลัก: I/O, Two-pass Scan, Block Streaming และ Progress Display"""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.input_path = Path(args.input).expanduser().resolve()

        if not self.input_path.is_file():
            raise MatrixDecoderError(f"ไม่พบไฟล์นำเข้า: {self.input_path}", 5)

        # กำหนดชื่อไฟล์ผลลัพธ์
        if args.output:
            self.output_path = Path(args.output).expanduser().resolve()
        else:
            stem = self.input_path.stem
            self.output_path = self.input_path.parent / f"{stem}.51.wav"

        # ตรวจสอบการเขียนทับ
        if self.output_path.exists() and not args.yes and not args.dry_run:
            ans = input(f"ไฟล์ '{self.output_path.name}' มีอยู่แล้ว ต้องการเขียนทับหรือไม่? [y/N]: ")
            if ans.lower().strip() not in ("y", "yes", "1"):
                print("ยกเลิกการทำงานตามคำสั่งผู้ใช้", file=sys.stderr)
                sys.exit(0)

        # ตรวจสอบเครื่องมือ FFmpeg หากจำเป็น
        self.ffmpeg_path = find_ffmpeg_tool("ffmpeg", args.ffmpeg)
        self.ffprobe_path = find_ffmpeg_tool("ffprobe", args.ffmpeg)

        # วิเคราะห์ข้อมูลไฟล์เสียงนำเข้า
        self.audio_info = self._inspect_input()

        # สร้าง Config
        self.config = self._build_config()

    def _inspect_input(self) -> StreamAudioInfo:
        """ตรวจสอบความสมบูรณ์และจำนวนช่องสัญญาณของไฟล์อินพุต"""
        info = None
        # ลองเปิดด้วย Native WAV Parser ก่อน
        try:
            with open(self.input_path, "rb") as f:
                reader = WavStreamReader(f)
                info = reader.info
        except Exception:
            pass

        # หากเปิดด้วย Native ไม่ได้ หรือต้องการตรวจสอบลึก ให้ใช้ ffprobe
        if info is None and self.ffprobe_path:
            cmd = [
                self.ffprobe_path,
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                str(self.input_path),
            ]
            try:
                out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
                data = json.loads(out)
                streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
                if not streams:
                    raise MatrixDecoderError("ไม่พบสตรีมเสียงในไฟล์นำเข้า", 3)
                s = streams[0]
                ch = int(s.get("channels", 2))
                sr = int(s.get("sample_rate", 48000))
                dur = float(s.get("duration", 0.0))
                total_f = int(dur * sr) if dur > 0 else None
                info = StreamAudioInfo(
                    container="media",
                    codec=s.get("codec_name", "unknown"),
                    sample_rate=sr,
                    channels=ch,
                    bit_depth=32,
                    channel_layout_str=s.get("channel_layout"),
                    channel_mask=None,
                    total_frames=total_f,
                    duration_sec=dur,
                    source_is_pipe=True,
                )
            except Exception:
                pass

        if info is None:
            raise MatrixDecoderError("ไม่สามารถอ่านข้อมูลไฟล์เสียงนำเข้าได้", 3)

        # ตรวจสอบข้อกำหนดด้านแชนเนลอินพุต
        if info.channels == 1:
            raise MatrixDecoderError(
                "ไฟล์เสียงอินพุตเป็นโมโน (1 ช่อง) ไม่รองรับการถอดรหัสเมทริกซ์ Lt/Rt (ต้องมี 2 ช่อง)", 3
            )
        if info.channels > 2:
            raise MatrixDecoderError(
                f"ไฟล์เสียงอินพุตมี {info.channels} ช่องสัญญาณ (ต้องการสัญญาณสเตอริโอ 2 ช่อง Lt/Rt) "
                "หากเป็นไฟล์มัลติแชนเนล กรุณาเข้ารหัสด้วย 6to2matrixencoder.py ก่อน", 3
            )

        return info

    def _build_config(self) -> DecoderConfig:
        """แปลงอาร์กิวเมนต์ CLI เป็น DecoderConfig"""
        trims = {role: 0.0 for role in STANDARD_ROLES_51}
        if self.args.trim:
            for item in self.args.trim:
                parts = item.split("=", 1)
                if len(parts) == 2:
                    k = parts[0].strip().upper()
                    if k == "SL": k = "Ls"
                    elif k == "SR": k = "Rs"
                    elif k == "FL": k = "L"
                    elif k == "FR": k = "R"
                    elif k == "FC": k = "C"
                    try:
                        trims[k] = float(parts[1])
                    except ValueError:
                        raise MatrixDecoderError(f"ค่า Trim '{item}' ไม่ถูกต้อง", 2)

        servo_params = {}
        if self.args.servo_param:
            for item in self.args.servo_param:
                parts = item.split("=", 1)
                if len(parts) == 2:
                    servo_params[parts[0].strip().lower()] = float(parts[1])

        # ดีเลย์เริ่มต้นตามโหมด
        surr_delay = self.args.surround_delay
        if surr_delay is None:
            if self.args.mode == "ds":
                surr_delay = 20.0
            elif self.args.mode == "pl":
                surr_delay = 10.0
            elif self.args.mode == "pl2music":
                surr_delay = 0.0
            else:
                surr_delay = 10.0

        # LPF เริ่มต้นตามโหมด
        surr_lpf = self.args.surround_lpf
        if surr_lpf is None:
            surr_lpf = "7k" if self.args.mode in ("ds", "pl") else "off"

        return DecoderConfig(
            mode=MatrixMode(self.args.mode),
            steering=self.args.steering == "on",
            steer_engine=SteeringEngine(self.args.steer_engine),
            servo_impl=ServoImpl(self.args.servo_impl),
            coef_set=self.args.coef_set,
            lookahead_ms=self.args.lookahead,
            hop_size=self.args.hop,
            servo_env_tc_ms=self.args.servo_env_tc,
            servo_fast_tc_ms=self.args.servo_fast_tc,
            servo_params=servo_params,
            center_width=self.args.center_width,
            dimension=self.args.dimension,
            panorama=self.args.panorama,
            surround_delay_ms=surr_delay,
            surround_lpf=surr_lpf,
            input_balance=self.args.input_balance,
            makeup_gain_db=self.args.makeup_gain,
            out_gain_db=self.args.out_gain,
            trims=trims,
            headroom=HeadroomMode(self.args.headroom),
            lfe_mode=LfeMode(self.args.lfe_mode),
            lfe_lp_hz=self.args.lfe_lp,
            lfe_gain_db=self.args.lfe_gain,
            lfe_redirect=self.args.lfe_redirect,
            out_layout=OutLayout(self.args.out_layout),
            block_size=self.args.block_size,
            ffmpeg_bin=self.args.ffmpeg,
        )

    def _open_input_stream(self) -> Union[WavStreamReader, FFmpegStreamReader]:
        """เปิดสตรีมเสียงอินพุต หากเป็น WAV 48kHz เปิดตรง หากไม่ใช่ใช้ท่อ FFmpeg"""
        is_direct_wav = (
            self.input_path.suffix.lower() == ".wav"
            and self.audio_info.sample_rate == SAMPLE_RATE_TARGET
            and self.audio_info.channels == 2
        )

        if is_direct_wav:
            try:
                f = open(self.input_path, "rb")
                return WavStreamReader(f)
            except Exception:
                pass

        if not self.ffmpeg_path:
            raise MatrixDecoderError(
                "ไฟล์เสียงนำเข้าไม่ใช่ WAV 48 kHz 2 ช่อง และไม่พบโปรแกรม FFmpeg เพื่อแปลงสัญญาณ "
                "กรุณาติดตั้ง FFmpeg หรือระบุพาธด้วย --ffmpeg", 4
            )

        cmd = [
            self.ffmpeg_path,
            "-hide_banner",
            "-v", "error",
            "-i", str(self.input_path),
            "-vn",
            "-ar", str(SAMPLE_RATE_TARGET),
            "-ac", "2",
            "-f", "f32le",
            "-",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1024 * 1024,
        )
        pipe_info = StreamAudioInfo(
            container="pipe",
            codec="pcm_f32le",
            sample_rate=SAMPLE_RATE_TARGET,
            channels=2,
            bit_depth=32,
            channel_layout_str="stereo",
            channel_mask=0x0003,
            total_frames=self.audio_info.total_frames,
            duration_sec=self.audio_info.duration_sec,
            source_is_pipe=True,
        )
        return FFmpegStreamReader(proc, pipe_info)

    def _scan_peak(self) -> float:
        """สแกนไฟล์หาค่าพีคสูงสุดสำหรับ Headroom Mode: PEAK"""
        stream = self._open_input_stream()
        pipeline = DecoderDSPPipeline(self.config, SAMPLE_RATE_TARGET)
        block_size = self.config.block_size
        max_p = 0.0
        try:
            while True:
                chunk = stream.read_frames(block_size)
                if chunk.shape[0] == 0:
                    break
                out_block = pipeline.process_block(chunk, 1.0)
                if out_block.size > 0:
                    cur_p = float(np.max(np.abs(out_block)))
                    if cur_p > max_p:
                        max_p = cur_p
            flush_block = pipeline.flush(1.0)
            if flush_block.size > 0:
                cur_p = float(np.max(np.abs(flush_block)))
                if cur_p > max_p:
                    max_p = cur_p
        finally:
            stream.close()
        return max_p

    def run(self) -> int:
        """รันกระบวนการถอดรหัสเต็มรูปแบบ"""
        # โหมดตรวจสอบโดยไม่เขียนไฟล์ (--dry-run)
        if self.args.dry_run:
            info_dict = {
                "input_file": str(self.input_path),
                "output_file": str(self.output_path),
                "sample_rate": SAMPLE_RATE_TARGET,
                "input_channels": self.audio_info.channels,
                "output_channels": 6,
                "mode": self.config.mode.value,
                "steering": self.config.steering,
                "steer_engine": self.config.steer_engine.value,
                "servo_impl": self.config.servo_impl.value,
                "coef_set": self.config.coef_set,
                "surround_delay_ms": self.config.surround_delay_ms,
                "surround_lpf": self.config.surround_lpf,
                "center_width": self.config.center_width,
                "dimension": self.config.dimension,
                "panorama": self.config.panorama,
                "lfe_mode": self.config.lfe_mode.value,
                "numba_accelerated": HAVE_NUMBA,
            }
            if self.args.json:
                print(json.dumps(info_dict, indent=2, ensure_ascii=False))
            else:
                print("=" * 70)
                print(f"[DRY-RUN] ตรวจสอบการกำหนดค่าสำหรับถอดรหัส: {self.input_path.name}")
                print(f"  โหมด: {self.config.mode.value.upper()} (Steering: {self.config.steering})")
                print(f"  เครื่องยนต์ควบคุม: {self.config.steer_engine.value} (Servo: {self.config.servo_impl.value})")
                print(f"  Numba JIT: {'เปิดใช้งาน (เร่งความเร็วสูง)' if HAVE_NUMBA else 'ไม่พบ (รันโหมด Pure Python)'}")
                print(f"  ดีเลย์เซอร์ราวด์: {self.config.surround_delay_ms} ms, ตัวกรอง: {self.config.surround_lpf}")
                if self.config.mode == MatrixMode.PL2MUSIC:
                    print(f"  พารามิเตอร์ดนตรี: Center Width={self.config.center_width}, Dimension={self.config.dimension}, Panorama={self.config.panorama}")
                print(f"  LFE: {self.config.lfe_mode.value} (LP: {self.config.lfe_lp_hz} Hz)")
                print("=" * 70)
            return 0

        # จัดการ Headroom
        headroom_scale = 1.0
        if self.config.headroom == HeadroomMode.STATIC:
            headroom_scale = 10.0 ** (-3.0 / 20.0)
        elif self.config.headroom == HeadroomMode.PEAK:
            if not self.args.quiet:
                print("[PASS 1/2] กำลังสแกนหาจุดพีคของสัญญาณเสียง...", file=sys.stderr)
            scanned_peak = self._scan_peak()
            if scanned_peak > 1.0:
                headroom_scale = 1.0 / scanned_peak
                if not self.args.quiet:
                    print(f"  ตรวจพบพีคเกิน 0 dBFS ({20*math.log10(scanned_peak):+.2f} dB) ลดระดับอัตโนมัติรอบที่ 2", file=sys.stderr)

        mask = MASK_51_SIDE if self.config.out_layout == OutLayout.SIDE_51 else MASK_51_BACK
        writer = WavWriterF32(
            self.output_path,
            sample_rate=SAMPLE_RATE_TARGET,
            channels=6,
            channel_mask=mask,
        )

        pipeline = DecoderDSPPipeline(self.config, SAMPLE_RATE_TARGET)
        stream = self._open_input_stream()

        total_frames = self.audio_info.total_frames or 0
        frames_processed = 0
        peak_per_ch = np.zeros(6, dtype=np.float64)
        nan_inf_count = 0
        start_time = time.time()
        last_ui_time = 0.0

        if not self.args.quiet:
            print("=" * 70, file=sys.stderr)
            print(f"เริ่มการถอดรหัส Matrix 2.0 -> 5.1 Surround ด้วยโหมด: {self.config.mode.value.upper()}", file=sys.stderr)
            print(f"  ไฟล์นำเข้า: {self.input_path}", file=sys.stderr)
            print(f"  ไฟล์ผลลัพธ์: {self.output_path}", file=sys.stderr)
            print(f"  ระบบประมวลผล: {self.config.steer_engine.value} ({'Numba JIT' if HAVE_NUMBA else 'Pure Python'})", file=sys.stderr)
            print("=" * 70, file=sys.stderr)

        warmup_dropped = 0
        latency = pipeline.lookahead_samples

        try:
            while True:
                chunk = stream.read_frames(self.config.block_size)
                if chunk.shape[0] == 0:
                    break

                out_block = pipeline.process_block(chunk, headroom_scale)

                # ตรวจสอบ NaN / Inf
                if not np.isfinite(out_block).all():
                    bad_mask = ~np.isfinite(out_block)
                    nan_inf_count += int(np.sum(bad_mask))
                    out_block = np.nan_to_num(out_block, nan=0.0, posinf=1.0, neginf=-1.0)

                # ตัดส่วน Warmup ของ Look-Ahead Delay ออก เพื่อรักษาความยาวเฟรมให้ตรงกับต้นฉบับเป๊ะ
                if warmup_dropped < latency:
                    drop = min(latency - warmup_dropped, out_block.shape[0])
                    warmup_dropped += drop
                    out_block = out_block[drop:]

                if out_block.shape[0] > 0:
                    # ติดตาม Peak แต่ละแชนเนล
                    for c in range(6):
                        c_peak = float(np.max(np.abs(out_block[:, c])))
                        if c_peak > peak_per_ch[c]:
                            peak_per_ch[c] = c_peak

                    writer.write_frames(out_block)
                frames_processed += chunk.shape[0]

                # อัปเดตความคืบหน้าบน UI
                now = time.time()
                if not self.args.quiet and (now - last_ui_time >= 0.25):
                    last_ui_time = now
                    elapsed = now - start_time
                    speed = (frames_processed / SAMPLE_RATE_TARGET) / max(elapsed, 0.001)
                    if total_frames > 0:
                        pct = min(100.0, (frames_processed / total_frames) * 100.0)
                        rem_sec = (total_frames - frames_processed) / max(frames_processed / elapsed, 1.0)
                        sys.stderr.write(
                            f"\r  ความคืบหน้า: {pct:5.1f}% | ความเร็ว: {speed:5.1f}x | ETA: {rem_sec:4.0f}s"
                        )
                    else:
                        sys.stderr.write(
                            f"\r  ประมวลผลแล้ว: {frames_processed / SAMPLE_RATE_TARGET:6.1f}s | ความเร็ว: {speed:5.1f}x"
                        )
                    sys.stderr.flush()

            # Flush สัญญาณที่ค้างใน Delay Line ตอนจบไฟล์
            flush_block = pipeline.flush(headroom_scale)
            if flush_block.size > 0:
                for c in range(6):
                    c_peak = float(np.max(np.abs(flush_block[:, c])))
                    if c_peak > peak_per_ch[c]:
                        peak_per_ch[c] = c_peak
                writer.write_frames(flush_block)

            if not self.args.quiet:
                sys.stderr.write("\r  ความคืบหน้า: 100.0% เสร็จสมบูรณ์!                          \n")
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
        overall_peak = float(np.max(peak_per_ch))
        peak_db = 20 * math.log10(overall_peak) if overall_peak > 1e-7 else -99.0

        if not self.args.quiet:
            print("=" * 70, file=sys.stderr)
            print("[SUCCESS] การถอดรหัสสัญญาณ Matrix สำเร็จเรียบร้อย!", file=sys.stderr)
            print(f"  ไฟล์ผลลัพธ์: {self.output_path}", file=sys.stderr)
            print("  ระดับพีคแต่ละช่องเสียง (True Peak):", file=sys.stderr)
            labels = ["FL", "FR", "FC", "LFE", "SL/BL", "SR/BR"]
            for idx, label in enumerate(labels):
                val_db = 20 * math.log10(peak_per_ch[idx]) if peak_per_ch[idx] > 1e-7 else -99.0
                print(f"    - ช่อง {label:5s}: {val_db:+6.2f} dBFS", file=sys.stderr)
            if nan_inf_count > 0:
                print(f"  [WARNING] ตรวจพบตัวอย่าง NaN/Inf จำนวน {nan_inf_count} ตัวอย่าง", file=sys.stderr)
            if peak_db > 0.0:
                print(
                    f"  [WARNING] สัญญาณเกิดการคลิปเกิน 0 dBFS (+{peak_db:.2f} dB) "
                    f"แนะนำให้ใช้ --headroom static หรือ --headroom peak", file=sys.stderr
                )
            print("=" * 70, file=sys.stderr)

        return 0


# ==============================================================================
# 10. CLI Parser และ HELP ภาษาไทย (CLI Argument Definitions)
# ==============================================================================
HELP_TEXT_THAI = """
โปรแกรมถอดรหัสสัญญาณเสียง Matrix Lt/Rt 2.0 เป็น 5.1 เซอร์ราวด์ (Float32 48 kHz)
พัฒนาขึ้นตามสถาปัตยกรรม Active Steering Matrix ของสิทธิบัตรสาธารณะ US 7,280,664 B2 (James W. Fosgate)
หมายเหตุ: โปรแกรมนี้เป็นการพัฒนาอิสระตามเอกสารสาธารณะ ไม่ได้รับการรับรองจาก Dolby Laboratories
เครื่องหมายการค้า Dolby, Pro Logic, Dolby Surround เป็นของ Dolby Laboratories

ตารางโหมดการถอดรหัส (--mode):
  ds        Dolby Surround ดั้งเดิม (Passive Matrix, S โมโน LPF 7kHz, Delay 20ms, Phantom Center)
  pl        Dolby Pro Logic (PL Emulation Active Steering, S โมโน LPF 7kHz, Delay 10ms, Center อิสระ)
  pl2       Dolby Pro Logic II Movie [ค่าเริ่มต้น] (Standard Active Steering เต็มรูปแบบ, Ls/Rs สเตอริโอ Full-range)
  pl2music  Dolby Pro Logic II Music (Standard Active Steering, ไม่มี Delay เซอร์ราวด์, ปรับ Center Width ได้)

ระดับความแม่นยำทางวิศวกรรม:
  [E1] ยืนยันได้จากคณิตศาสตร์: Passive Matrix, WAV RF64, Channel Mask, Peak Normalization
  [E2] เอกสารสาธารณะ/สิทธิบัตร: วงจร Fosgate Servo Feedback, BPF 788-1878Hz, ค่าคงที่ A0-A4, B0-B5
  [E3] พัฒนาขึ้นใหม่: ลอจิกจำลองความเสถียร Negative Feedback, การสร้างสัญญาณ LFE จากเบส, Auto-Balance

ตัวอย่างคำสั่งใช้งาน:
  1. ถอดรหัสไฟล์ Lt/Rt ทั่วไปเป็น 5.1 ด้วย Dolby Pro Logic II (ค่าเริ่มต้น):
     python lib/2to6matrixdecoder.py -i movie_ltrt.wav -o movie_51.wav

  2. ถอดรหัสเพลงสเตอริโอด้วยโหมด Pro Logic II Music พร้อมปรับ Center Width:
     python lib/2to6matrixdecoder.py -i music_ltrt.wav -o music_51.wav --mode pl2music --center-width 4.0

  3. ถอดรหัส Pro Logic ดั้งเดิม พร้อมจำลอง Delay เซอร์ราวด์ 20 ms:
     python lib/2to6matrixdecoder.py -i classic_ltrt.wav --mode pl --surround-delay 20

  4. ถอดรหัสแบบ Passive เมทริกซ์ล้วน (ไม่เปิดระบบ Active Steering):
     python lib/2to6matrixdecoder.py -i input.wav --steering off

  5. สกัดเบสความถี่ต่ำกว่า 120 Hz เข้าช่อง LFE พร้อมปรับชดเชยเกน Center -1.5 dB:
     python lib/2to6matrixdecoder.py -i track.wav --lfe-mode bass --lfe-lp 120 --trim C=-1.5

  6. ตรวจสอบการกำหนดค่าและสัมประสิทธิ์เมทริกซ์โดยไม่เขียนไฟล์ (Dry-run):
     python lib/2to6matrixdecoder.py -i input.wav --dry-run

รหัสสถานะการสิ้นสุด (Exit Codes):
  0   สำเร็จเรียบร้อย
  1   เกิดข้อผิดพลาดทั่วไป
  2   การกำหนดอาร์กิวเมนต์ไม่ถูกต้อง
  3   ข้อมูลอินพุตไม่รองรับ (ไฟล์โมโน หรือมากกว่า 2 ช่อง)
  4   FFmpeg ทำงานล้มเหลว หรือไม่พบเครื่องมือ
  5   เกิดข้อผิดพลาดในการอ่าน/เขียนไฟล์ (I/O Error)
  130 ผู้ใช้ยกเลิกการทำงาน (Ctrl+C)
"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="2to6matrixdecoder.py",
        description=HELP_TEXT_THAI,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-i", "--input", required=True, help="พาธไฟล์เสียงสเตอริโอนำเข้า Lt/Rt (WAV หรือมัลติมีเดียใด ๆ)")
    parser.add_argument("-o", "--output", help="พาธไฟล์ WAV 5.1 ผลลัพธ์ (ค่าเริ่มต้น: <input>.51.wav)")
    parser.add_argument("-y", "--yes", action="store_true", help="เขียนทับไฟล์ผลลัพธ์เดิมโดยไม่ถาม")
    parser.add_argument(
        "--mode",
        choices=["ds", "pl", "pl2", "pl2music"],
        default="pl2",
        help="โหมดการถอดรหัสเมทริกซ์: ds, pl, pl2, pl2music (ค่าเริ่มต้น: pl2)",
    )
    parser.add_argument(
        "--steering",
        choices=["on", "off"],
        default="on",
        help="เปิด/ปิด ระบบ Active Steering Servo (ค่าเริ่มต้น: on)",
    )
    parser.add_argument(
        "--steer-engine",
        choices=["fosgate", "feedforward", "passive", "pairinv"],
        default="fosgate",
        help="โครงสร้างเครื่องยนต์ Steering: fosgate (S3 Feedback), feedforward (US 4,799,260), passive",
    )
    parser.add_argument(
        "--servo-impl",
        choices=["fast", "exact"],
        default="fast",
        help="รูปแบบการคำนวณ Servo: fast (Hop Interpolation) หรือ exact (Sample-by-sample ผ่าน Numba JIT)",
    )
    parser.add_argument(
        "--coef-set",
        choices=["auto", "standard", "pl-emulation"],
        default="auto",
        help="ชุดสัมประสิทธิ์เมทริกซ์: auto, standard (5.1 เต็มรูปแบบ), pl-emulation (เซอร์ราวด์โมโน)",
    )
    parser.add_argument("--lookahead", type=float, default=5.0, help="ขนาด Look-ahead Delay ของ Audio Path (ms, ค่าเริ่มต้น 5.0)")
    parser.add_argument("--hop", type=int, default=8, help="ขนาด Hop Size สำหรับคำนวณ Servo (samples, ค่าเริ่มต้น 8)")
    parser.add_argument("--servo-env-tc", type=float, default=800.0, help="Time Constant ตัวเกลี่ย Envelope ของ Servo (ms, ค่าเริ่มต้น 800.0)")
    parser.add_argument("--servo-fast-tc", type=float, default=2.5, help="Time Constant ตัวเกลี่ยเร็วของ Servo (ms, ค่าเริ่มต้น 2.5)")
    parser.add_argument("--servo-param", action="append", help="ปรับแต่งตัวแปรภายใน Servo เช่น --servo-param a3=0.25 (ระบุซ้ำได้)")

    # พารามิเตอร์ Music Mode
    parser.add_argument("--center-width", type=float, default=3.0, help="DPL2 Music: ความกว้าง Center Width (0.0 ถึง 7.0, ค่าเริ่มต้น 3.0)")
    parser.add_argument("--dimension", type=float, default=0.0, help="DPL2 Music: สมดุลมิติหน้า-หลัง Dimension (-3.0 ถึง +3.0, ค่าเริ่มต้น 0.0)")
    parser.add_argument("--panorama", action="store_true", help="DPL2 Music: เปิดใช้ฟังก์ชันโอบล้อม Panorama")

    # เซอร์ราวด์และการปรับแต่ง
    parser.add_argument("--surround-delay", type=float, default=None, help="ดีเลย์แชนเนลเซอร์ราวด์ (ms, ค่าเริ่มต้น: pl/pl2=10, ds=20, pl2music=0)")
    parser.add_argument("--surround-lpf", choices=["off", "7k"], default=None, help="ตัวกรอง Low-pass เซอร์ราวด์ 7 kHz (ค่าเริ่มต้น: ds/pl=7k, pl2=off)")
    parser.add_argument("--input-balance", default="off", help="ปรับสมดุลอินพุต: off, auto, หรือระบุค่า dB เช่น +1.5, -2.0")

    # เกนและการจัดการ Headroom
    parser.add_argument("--makeup-gain", type=float, default=0.0, help="เกนชดเชย Makeup Gain (dB, ค่าเริ่มต้น 0.0)")
    parser.add_argument("--out-gain", type=float, default=0.0, help="เกนขยายสุดท้ายของสัญญาณผลลัพธ์ (dB, ค่าเริ่มต้น 0.0)")
    parser.add_argument("--trim", action="append", help="ปรับเกนรายช่อง เช่น --trim C=-1.5 --trim LFE=+2.0")
    parser.add_argument("--headroom", choices=["none", "static", "peak"], default="none", help="การจัดการ Headroom: none, static, peak")

    # LFE
    parser.add_argument("--lfe-mode", choices=["silent", "bass"], default="silent", help="โหมดช่อง LFE: silent (เงียบ 0.0) หรือ bass (ดึงจากเบสคู่หน้า)")
    parser.add_argument("--lfe-lp", type=float, default=120.0, help="ความถี่ Low-pass ของช่อง LFE เมื่อเปิดโหมด bass (Hz, ค่าเริ่มต้น 120.0)")
    parser.add_argument("--lfe-gain", type=float, default=0.0, help="เกนขยายช่อง LFE เมื่อเปิดโหมด bass (dB, ค่าเริ่มต้น 0.0)")
    parser.add_argument("--lfe-redirect", action="store_true", help="หักลบเบสที่ดึงเข้า LFE ออกจากคู่หน้าด้วย High-pass Filter")

    # เลย์เอาต์และระบบ
    parser.add_argument("--out-layout", choices=["5.1side", "5.1back"], default="5.1side", help="มาตรฐานเลย์เอาต์แชนเนล: 5.1side (0x060F) หรือ 5.1back (0x003F)")
    parser.add_argument("--block-size", type=int, default=65536, help="ขนาดบล็อกประมวลผล (เฟรม, ค่าเริ่มต้น 65536)")
    parser.add_argument("--ffmpeg", help="ระบุพาธไปยังโปรแกรม ffmpeg / ffprobe")
    parser.add_argument("--dry-run", action="store_true", help="ตรวจสอบข้อมูลและคำนวณค่าโดยไม่เขียนไฟล์")
    parser.add_argument("--json", action="store_true", help="ส่งออกข้อมูล dry-run ในรูปแบบ JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="แสดงข้อความการทำงานอย่างละเอียด")
    parser.add_argument("-q", "--quiet", action="store_true", help="ปิดการแสดงแถบความคืบหน้า")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--list-modes", action="store_true", help="แสดงรายละเอียดของทุกโหมดแล้วออก")
    return parser


def main():
    parser = build_arg_parser()
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(2)

    if "--list-modes" in sys.argv:
        print("โหมดการถอดรหัส Matrix ที่รองรับ:")
        print("  ds       : Dolby Surround ดั้งเดิม (L, R, C phantom, S mono 7kHz LPF, Delay 20ms)")
        print("  pl       : Dolby Pro Logic (L, R, C discrete, S mono 7kHz LPF, Delay 10ms)")
        print("  pl2      : Dolby Pro Logic II Movie (L, R, C, Ls, Rs stereo discrete full-range, Delay 10ms)")
        print("  pl2music : Dolby Pro Logic II Music (Full-range, Delay 0ms, Center Width, Dimension, Panorama)")
        sys.exit(0)

    args = parser.parse_args()

    try:
        pipeline = DecoderPipeline(args)
        rc = pipeline.run()
        sys.exit(rc)
    except MatrixDecoderError as e:
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
