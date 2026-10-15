#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib/ddp_encode.py
===============================================================================
Wrapper สำหรับ Dolby Encoding Engine (DEE) — เข้ารหัส WAV → AC3 / EAC3
รองรับการนำเข้าเป็นโมดูล (Importable Module) และการรันเป็น CLI โดยตรง

ฟอร์แมตที่รองรับ:
  - WAV polyfile (5.1 / 7.1) → AC3  (Dolby Digital)
  - WAV polyfile (5.1)       → EAC3 (Dolby Digital Plus)
  - WAV polyfile (7.1)       → EAC3 7.1  (ddp71)
  - WAV แยกไฟล์ต่อ channel  → AC3 / EAC3  (wav_list mode)

ฟอร์แมตอื่น ๆ (TrueHD, DTS, PCM ฯลฯ):
  → ใช้ ffmpeg wrapper โดยตรง (ไม่อยู่ในโมดูลนี้)

กระบวนการทำงาน:
  1. สร้างไฟล์ XML Job Config ชั่วคราวในโฟลเดอร์ temp
  2. เรียก dee <job.xml> รอจนเสร็จ
  3. ลบไฟล์ temp ทิ้ง (เว้นแต่ --keep-temp)
  4. รายงานผลลัพธ์

ข้อมูลพารามิเตอร์ XML ทั้งหมด (ที่มี comment ใน template ของ DEE):
  ดู build_encode_xml() สำหรับรายละเอียดครบถ้วน
===============================================================================
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.dom.minidom as minidom
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

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

# ==============================================================================
# 1. ค่าคงที่
# ==============================================================================
VERSION = "1.0.0"

# ชุดค่า passthrough ที่ DEE รองรับ (สำหรับ validation)
VALID_ENCODER_MODE   = {"dd", "ddp", "ddp71"}
VALID_BSTREAM_MODE   = {
    "complete_main", "music_and_effects", "visually_impaired",
    "hearing_impaired", "dialogue", "commentary", "emergency", "voice_over",
}
VALID_DOWNMIX_CFG    = {"off", "mono", "stereo", "5.1"}
VALID_DRC_PROFILE    = {"film_standard", "film_light", "music_standard",
                        "music_light", "speech"}
VALID_MIX_CENTER     = {"+3", "+1.5", "0", "-1.5", "-3", "-4.5", "-6", "-inf"}
VALID_MIX_SURROUND   = {"-1.5", "-3", "-4.5", "-6", "-inf"}
VALID_DOWNMIX_MODE   = {"loro", "ltrt", "ltrt-pl2", "not_indicated"}
VALID_SURROUND_MODE  = {"yes", "no", "not_indicated"}
VALID_SURROUND_EX    = {"yes", "no", "not_indicated"}
VALID_TC_RATE        = {"not_indicated", "23.976", "24", "25", "29.97",
                        "30", "48", "50", "59.94", "60"}
VALID_TC_RATE_OUT    = {"auto", "23.976", "24", "25", "29.97",
                        "30", "50", "59.94", "60"}
VALID_LOUDNESS_PRESET = {
    "manual", "atsc_a85", "atsc_a85_agile",
    "ebu_r128", "freetv_op59", "arib_b32",
}
VALID_CORRECTION_MODE = {"pcm_scale", "md_update"}
VALID_METER           = {"1770-3", "1770-2", "1770-1", "LeqA"}
VALID_PEAK_REF        = {"true_peak", "sample_peak"}
VALID_OUTPUT_FORMAT   = {"ac3", "eac3", "ec3", "mp4"}

# ตาราง data_rate ที่ DEE รองรับ (kbps)
VALID_DATA_RATES = {
    32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 128, 144, 160,
    176, 192, 200, 208, 216, 224, 232, 240, 248, 256, 272, 288, 304,
    320, 336, 352, 368, 384, 400, 448, 512, 576, 640, 704, 768, 832,
    896, 960, 1008, 1024,
}

# ค่า data_rate สูงสุดของแต่ละ format
MAX_DATA_RATE = {"dd": 448, "ddp": 1024, "ddp71": 1024}

# wav_list channel tags ของแต่ละ channel_configuration
WAV_LIST_TAGS = {
    "mono":   ["C"],
    "stereo": ["L", "R"],
    "5.1":    ["L", "R", "C", "LFE", "LS", "RS"],
    "7.1":    ["L", "R", "C", "LFE", "LS", "RS", "LRS", "RRS"],
    "auto":   [],   # DEE จะตรวจเอง
}


# ==============================================================================
# 2. ข้อยกเว้นและ Dataclass พารามิเตอร์
# ==============================================================================
class DdpEncodeError(Exception):
    """ข้อผิดพลาดที่คาดการณ์ได้ พร้อม exit_code"""
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


@dataclass
class LoudnessParams:
    """
    พารามิเตอร์ <loudness> ใน pcm_to_ddp filter
    DEE รองรับสองโหมด: measure_only (แค่วัด) หรือ measure_and_correct (วัดแล้วแก้)
    """
    # "measure_only" | "measure_and_correct"
    mode: str = "measure_and_correct"

    # ─── measure_and_correct ──────────────────────────────────────────────
    # One of: manual, atsc_a85, atsc_a85_agile, ebu_r128, freetv_op59, arib_b32
    preset: str = "atsc_a85"

    # ─── manual เท่านั้น ─────────────────────────────────────────────────
    # One of: 1770-3, 1770-2, 1770-1, LeqA
    metering_mode: str = "1770-3"
    # boolean: true or false
    dialogue_intelligence: bool = True
    # integer: from 0 to 100
    speech_threshold: int = 15
    # One of: pcm_scale, md_update
    correction_mode: str = "pcm_scale"
    # integer: from -31 to -8
    loudness_target: int = -24
    # float: from -12 to -0.1
    peak_limit_db: float = -2.0
    # One of: true_peak, sample_peak
    peak_reference: str = "true_peak"
    # boolean: true or false (optional)
    measure_corrected: bool = False


@dataclass
class DownmixParams:
    """
    พารามิเตอร์ <downmix> ใน pcm_to_ddp filter
    ควบคุม metadata downmix levels ที่ฝังลงใน bitstream
    """
    # One of: +3, +1.5, 0, -1.5, -3, -4.5, -6, -inf
    loro_center_mix_level: str = "-3"
    # One of: -1.5, -3, -4.5, -6, -inf
    loro_surround_mix_level: str = "-3"
    # One of: +3, +1.5, 0, -1.5, -3, -4.5, -6, -inf
    ltrt_center_mix_level: str = "-3"
    # One of: -1.5, -3, -4.5, -6, -inf
    ltrt_surround_mix_level: str = "-3"
    # One of: loro, ltrt, ltrt-pl2, not_indicated
    preferred_downmix_mode: str = "loro"
    # boolean: true or false
    allow_hybrid_downmix: bool = False


@dataclass
class EncodeParams:
    """
    พารามิเตอร์ทั้งหมดสำหรับ DEE pcm_to_ddp job
    ค่าเหล่านี้ตรงกับ element ใน XML template ของ DEE ทุกประการ
    (รวม comment ที่ DEE ระบุค่าที่รองรับ)
    """
    # ─── input ───────────────────────────────────────────────────────────────
    input_path: Path                          # โฟลเดอร์ที่มีไฟล์ต้นทาง
    input_file: str                           # ชื่อไฟล์ WAV (polyfile)
    # One of: not_indicated, 23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60
    input_tc_rate: str = "not_indicated"
    # string — offset (auto หรือ H:M:S.ms)
    input_offset: str = "auto"
    # string — first frame of action
    input_ffoa: str = "auto"

    # ─── wav_list mode (input เป็นไฟล์แยกต่อ channel) ─────────────────────
    # เมื่อ True: input tag จะเป็น <wav_list> แทน <wav>
    wav_list_mode: bool = False
    # dict ของ {role: filename} เช่น {"L": "l.wav", "R": "r.wav", ...}
    wav_list_files: Dict[str, str] = field(default_factory=dict)
    # One of: mono, stereo, 5.1, 7.1, auto
    channel_configuration: str = "5.1"

    # ─── pcm_to_ddp filter ───────────────────────────────────────────────────
    # One of: dd (AC3), ddp (EAC3 5.1), ddp71 (EAC3 7.1)
    encoder_mode: str = "ddp"
    # One of: complete_main, music_and_effects, ...
    bitstream_mode: str = "complete_main"
    # One of: off, mono, stereo, 5.1
    downmix_config: str = "off"
    # One of: 32..1024 kbps (ดู VALID_DATA_RATES)
    data_rate: int = 192
    # One of: not_indicated, 23.976, ... 60
    timecode_frame_rate: str = "not_indicated"
    # string — ตำแหน่งเริ่มต้น
    start: str = "first_frame_of_action"
    # string — ตำแหน่งสิ้นสุด
    end: str = "end_of_file"
    # One of: file_position, embedded_timecode
    time_base: str = "file_position"
    # string — เติม silence ก่อน (วินาที)
    prepend_silence: str = "0.0"
    # string — เติม silence หลัง (วินาที)
    append_silence: str = "0.0"
    # boolean: true or false — เปิด LFE channel
    lfe_on: bool = True
    # One of: yes, no, not_indicated
    dolby_surround_mode: str = "not_indicated"
    # One of: yes, no, not_indicated
    dolby_surround_ex_mode: str = "no"
    # integer: from -1 to 65535 (-1 = auto)
    user_data: int = -1

    # ─── DRC ─────────────────────────────────────────────────────────────────
    # One of: film_standard, film_light, music_standard, music_light, speech
    line_mode_drc: str = "film_light"
    # One of: film_standard, film_light, music_standard, music_light, speech
    rf_mode_drc: str = "film_light"

    # ─── Processing flags ────────────────────────────────────────────────────
    # boolean: true or false — Low-pass filter บน LFE
    lfe_lowpass_filter: bool = True
    # boolean: true or false — หมุนเฟส surround 90°
    surround_90_phase_shift: bool = True
    # One of: true, false — ลด Surround -3 dB ตามมาตรฐาน Dolby
    surround_3db_attenuation: bool = True

    # ─── Loudness ─────────────────────────────────────────────────────────────
    loudness: LoudnessParams = field(default_factory=LoudnessParams)

    # ─── Downmix metadata ────────────────────────────────────────────────────
    downmix: DownmixParams = field(default_factory=DownmixParams)

    # ─── Embedded Timecodes ──────────────────────────────────────────────────
    # 'off' | 'auto' | H:M:S.ms
    starting_timecode: str = "off"
    # One of: auto, 23.976, 24, 25, 29.97, 30, 50, 59.94, 60
    tc_frame_rate_out: str = "auto"

    # ─── output ──────────────────────────────────────────────────────────────
    output_path: Path = field(default_factory=Path)
    output_file: str = ""
    # "ac3" | "eac3"/"ec3" | "mp4"
    output_format: str = "eac3"

    # ─── misc ─────────────────────────────────────────────────────────────────
    temp_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()))
    clean_temp: bool = True


# ==============================================================================
# 3. ค้นหา DEE / ffprobe ในระบบ
# ==============================================================================
def _find_tool(name: str, explicit: Optional[str] = None) -> Optional[str]:
    """ค้นหา executable: ระบุโดยตรง → ENV → PATH → โฟลเดอร์โปรเจกต์"""
    ext = ".exe" if os.name == "nt" else ""

    if explicit:
        p = Path(explicit).expanduser().resolve()
        if p.is_file():
            return str(p)
        c = p / (name + ext)
        if c.is_file():
            return str(c)

    env_key = f"{name.upper().replace('-', '_')}_BIN"
    env_val = os.environ.get(env_key)
    if env_val:
        p = Path(env_val).expanduser().resolve()
        if p.is_file():
            return str(p)

    found = shutil.which(name)
    if found:
        return found

    cur = Path(__file__).resolve().parent
    for sub in [cur, cur.parent, cur.parent / "tools", cur.parent / "bin"]:
        c = sub / (name + ext)
        if c.is_file():
            return str(c)

    return None


def find_dee(explicit: Optional[str] = None) -> str:
    for candidate in ("dee", "dee.exe", "dolby_encoding_engine"):
        result = _find_tool(candidate, explicit)
        if result:
            return result
    raise DdpEncodeError(
        "ไม่พบโปรแกรม DEE (Dolby Encoding Engine) ในระบบ\n"
        "กรุณาติดตั้ง DEE และเพิ่มลงใน PATH หรือระบุพาธด้วย --dee",
        exit_code=4,
    )


def find_ffprobe(explicit: Optional[str] = None) -> Optional[str]:
    return _find_tool("ffprobe", explicit)


# ==============================================================================
# 4. Validation
# ==============================================================================
def _validate_params(p: EncodeParams) -> None:
    """ตรวจสอบความถูกต้องของพารามิเตอร์ก่อนสร้าง XML"""
    if p.encoder_mode not in VALID_ENCODER_MODE:
        raise DdpEncodeError(
            f"--encoder-mode '{p.encoder_mode}' ไม่ถูกต้อง ค่าที่รองรับ: {VALID_ENCODER_MODE}",
            exit_code=2,
        )
    if p.bitstream_mode not in VALID_BSTREAM_MODE:
        raise DdpEncodeError(
            f"--bitstream-mode '{p.bitstream_mode}' ไม่ถูกต้อง",
            exit_code=2,
        )
    if p.downmix_config not in VALID_DOWNMIX_CFG:
        raise DdpEncodeError(
            f"--downmix-config '{p.downmix_config}' ไม่ถูกต้อง",
            exit_code=2,
        )
    if p.data_rate not in VALID_DATA_RATES:
        raise DdpEncodeError(
            f"--data-rate {p.data_rate} ไม่อยู่ในรายการค่าที่ DEE รองรับ\n"
            f"ดูรายการค่าที่ถูกต้องด้วย --list-rates",
            exit_code=2,
        )
    max_rate = MAX_DATA_RATE.get(p.encoder_mode, 1024)
    if p.data_rate > max_rate:
        raise DdpEncodeError(
            f"--data-rate {p.data_rate} kbps เกินขีดสูงสุดของ {p.encoder_mode} ({max_rate} kbps)",
            exit_code=2,
        )
    if p.encoder_mode == "ddp71" and p.downmix_config != "off":
        raise DdpEncodeError(
            "โหมด ddp71 (7.1) บังคับให้ --downmix-config เป็น 'off'",
            exit_code=2,
        )
    if p.line_mode_drc not in VALID_DRC_PROFILE:
        raise DdpEncodeError(
            f"--line-drc '{p.line_mode_drc}' ไม่ถูกต้อง ค่าที่รองรับ: {VALID_DRC_PROFILE}",
            exit_code=2,
        )
    if p.rf_mode_drc not in VALID_DRC_PROFILE:
        raise DdpEncodeError(
            f"--rf-drc '{p.rf_mode_drc}' ไม่ถูกต้อง ค่าที่รองรับ: {VALID_DRC_PROFILE}",
            exit_code=2,
        )
    if p.loudness.preset not in VALID_LOUDNESS_PRESET:
        raise DdpEncodeError(
            f"--loudness-preset '{p.loudness.preset}' ไม่ถูกต้อง ค่าที่รองรับ: {VALID_LOUDNESS_PRESET}",
            exit_code=2,
        )
    if p.downmix.loro_center_mix_level not in VALID_MIX_CENTER:
        raise DdpEncodeError(
            f"--loro-center '{p.downmix.loro_center_mix_level}' ไม่ถูกต้อง",
            exit_code=2,
        )
    if p.downmix.loro_surround_mix_level not in VALID_MIX_SURROUND:
        raise DdpEncodeError(
            f"--loro-surround '{p.downmix.loro_surround_mix_level}' ไม่ถูกต้อง",
            exit_code=2,
        )
    if p.downmix.ltrt_center_mix_level not in VALID_MIX_CENTER:
        raise DdpEncodeError(
            f"--ltrt-center '{p.downmix.ltrt_center_mix_level}' ไม่ถูกต้อง",
            exit_code=2,
        )
    if p.downmix.ltrt_surround_mix_level not in VALID_MIX_SURROUND:
        raise DdpEncodeError(
            f"--ltrt-surround '{p.downmix.ltrt_surround_mix_level}' ไม่ถูกต้อง",
            exit_code=2,
        )
    if p.downmix.preferred_downmix_mode not in VALID_DOWNMIX_MODE:
        raise DdpEncodeError(
            f"--preferred-downmix '{p.downmix.preferred_downmix_mode}' ไม่ถูกต้อง",
            exit_code=2,
        )
    if p.output_format in ("ec3",):
        p.output_format = "eac3"  # normalize
    if p.output_format not in VALID_OUTPUT_FORMAT:
        raise DdpEncodeError(
            f"--output-format '{p.output_format}' ไม่ถูกต้อง ค่าที่รองรับ: {VALID_OUTPUT_FORMAT}",
            exit_code=2,
        )
    # ตรวจสอบความสอดคล้อง encoder_mode กับ output_format
    if p.encoder_mode == "dd" and p.output_format not in ("ac3", "mp4"):
        raise DdpEncodeError(
            "encoder_mode='dd' ต้องใช้ output_format='ac3' หรือ 'mp4'",
            exit_code=2,
        )
    if p.encoder_mode in ("ddp", "ddp71") and p.output_format not in ("eac3", "ec3", "mp4"):
        raise DdpEncodeError(
            f"encoder_mode='{p.encoder_mode}' ต้องใช้ output_format='eac3' หรือ 'mp4'",
            exit_code=2,
        )


# ==============================================================================
# 5. สร้าง XML Job Config
# ==============================================================================
def _add_loudness(parent: ET.Element, lp: LoudnessParams, is_71: bool) -> None:
    """เพิ่ม <loudness> element ลงใน parent ตาม mode และ encoder"""
    lel = ET.SubElement(parent, "loudness")

    if is_71:
        # 7.1 mode: DEE บังคับให้ใช้ measure_only เท่านั้น (auto-correct ไม่รองรับ)
        mo = ET.SubElement(lel, "measure_only")
        ET.SubElement(mo, "metering_mode").text = lp.metering_mode
        ET.SubElement(mo, "dialogue_intelligence").text = (
            "true" if lp.dialogue_intelligence else "false"
        )
        ET.SubElement(mo, "speech_threshold").text = str(lp.speech_threshold)
        return

    if lp.mode == "measure_only":
        mo = ET.SubElement(lel, "measure_only")
        ET.SubElement(mo, "metering_mode").text = lp.metering_mode
        ET.SubElement(mo, "dialogue_intelligence").text = (
            "true" if lp.dialogue_intelligence else "false"
        )
        ET.SubElement(mo, "speech_threshold").text = str(lp.speech_threshold)
    else:
        # measure_and_correct
        mac = ET.SubElement(lel, "measure_and_correct")
        ET.SubElement(mac, "preset").text = lp.preset

        if lp.preset == "manual":
            # manual: ระบุค่าทั้งหมดเอง
            ET.SubElement(mac, "metering_mode").text = lp.metering_mode
            ET.SubElement(mac, "dialogue_intelligence").text = (
                "true" if lp.dialogue_intelligence else "false"
            )
            ET.SubElement(mac, "speech_threshold").text = str(lp.speech_threshold)
            ET.SubElement(mac, "correction_mode").text = lp.correction_mode
            ET.SubElement(mac, "loudness_target").text = str(lp.loudness_target)
            ET.SubElement(mac, "peak_limit_db").text = str(lp.peak_limit_db)
            ET.SubElement(mac, "peak_reference").text = lp.peak_reference
            ET.SubElement(mac, "measure_corrected").text = (
                "true" if lp.measure_corrected else "false"
            )


def _add_downmix(parent: ET.Element, dm: DownmixParams) -> None:
    """เพิ่ม <downmix> + <allow_hybrid_downmix> elements ลงใน parent"""
    dmel = ET.SubElement(parent, "downmix")
    # One of: +3, +1.5, 0, -1.5, -3, -4.5, -6, -inf
    ET.SubElement(dmel, "loro_center_mix_level").text   = dm.loro_center_mix_level
    # One of: -1.5, -3, -4.5, -6, -inf
    ET.SubElement(dmel, "loro_surround_mix_level").text = dm.loro_surround_mix_level
    # One of: +3, +1.5, 0, -1.5, -3, -4.5, -6, -inf
    ET.SubElement(dmel, "ltrt_center_mix_level").text   = dm.ltrt_center_mix_level
    # One of: -1.5, -3, -4.5, -6, -inf
    ET.SubElement(dmel, "ltrt_surround_mix_level").text = dm.ltrt_surround_mix_level
    # One of: loro, ltrt, ltrt-pl2, not_indicated
    ET.SubElement(dmel, "preferred_downmix_mode").text  = dm.preferred_downmix_mode

    ET.SubElement(parent, "allow_hybrid_downmix").text = (
        "true" if dm.allow_hybrid_downmix else "false"
    )


def build_encode_xml(p: EncodeParams) -> str:
    """
    สร้าง XML job config สำหรับ DEE pcm_to_ddp
    พารามิเตอร์ทุกตัวที่ DEE รองรับถูก passthrough มาจาก EncodeParams
    """
    is_71 = (p.encoder_mode == "ddp71")
    root = ET.Element("job_config")

    # ── Input ──────────────────────────────────────────────────────────────
    inp = ET.SubElement(root, "input")
    audio_in = ET.SubElement(inp, "audio")

    if p.wav_list_mode:
        # <wav_list version="1"> — input แยกไฟล์ต่อ channel
        wl = ET.SubElement(audio_in, "wav_list", version="1")
        for role, fname in p.wav_list_files.items():
            ET.SubElement(wl, f"file_name_{role}").text = fname
        # One of: mono, stereo, 5.1, 7.1, auto
        ET.SubElement(wl, "channel_configuration").text = p.channel_configuration
        ET.SubElement(wl, "timecode_frame_rate").text = p.input_tc_rate
        ET.SubElement(wl, "offset").text = p.input_offset
        ET.SubElement(wl, "ffoa").text = p.input_ffoa
        storage = ET.SubElement(wl, "storage")
        ET.SubElement(ET.SubElement(storage, "local"), "path").text = str(p.input_path)
    else:
        # <wav version="1"> — WAV polyfile ปกติ
        wav = ET.SubElement(audio_in, "wav", version="1")
        ET.SubElement(wav, "file_name").text = p.input_file
        # One of: not_indicated, 23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60
        ET.SubElement(wav, "timecode_frame_rate").text = p.input_tc_rate
        # string — offset
        ET.SubElement(wav, "offset").text = p.input_offset
        # string — first frame of action
        ET.SubElement(wav, "ffoa").text = p.input_ffoa
        storage = ET.SubElement(wav, "storage")
        ET.SubElement(ET.SubElement(storage, "local"), "path").text = str(p.input_path)

    # ── Filter: pcm_to_ddp ────────────────────────────────────────────────
    filt = ET.SubElement(root, "filter")
    audio_f = ET.SubElement(filt, "audio")
    enc = ET.SubElement(audio_f, "pcm_to_ddp", version="3")

    # ── Loudness ─────────────────────────────────────────────────────────
    _add_loudness(enc, p.loudness, is_71)

    # One of: dd, ddp, ddp71
    ET.SubElement(enc, "encoder_mode").text = p.encoder_mode
    # One of: complete_main, music_and_effects, ...
    ET.SubElement(enc, "bitstream_mode").text = p.bitstream_mode
    # One of: off, mono, stereo, 5.1  (ddp71 บังคับ off)
    ET.SubElement(enc, "downmix_config").text = "off" if is_71 else p.downmix_config
    # One of: 32..1024 kbps
    ET.SubElement(enc, "data_rate").text = str(p.data_rate)
    # One of: not_indicated, 23.976, ... 60
    ET.SubElement(enc, "timecode_frame_rate").text = p.timecode_frame_rate
    # string — start
    ET.SubElement(enc, "start").text = p.start
    # string — end
    ET.SubElement(enc, "end").text = p.end
    # One of: file_position, embedded_timecode
    ET.SubElement(enc, "time_base").text = p.time_base
    # string — silence ก่อน/หลัง
    ET.SubElement(enc, "prepend_silence_duration").text = p.prepend_silence
    ET.SubElement(enc, "append_silence_duration").text = p.append_silence
    # boolean: true or false
    ET.SubElement(enc, "lfe_on").text = "true" if p.lfe_on else "false"
    # One of: yes, no, not_indicated
    ET.SubElement(enc, "dolby_surround_mode").text = p.dolby_surround_mode
    # One of: yes, no, not_indicated
    ET.SubElement(enc, "dolby_surround_ex_mode").text = p.dolby_surround_ex_mode
    # integer: from -1 to 65535
    ET.SubElement(enc, "user_data").text = str(p.user_data)

    # ── DRC ──────────────────────────────────────────────────────────────
    drc_el = ET.SubElement(enc, "drc")
    # One of: film_standard, film_light, music_standard, music_light, speech
    ET.SubElement(drc_el, "line_mode_drc_profile").text = p.line_mode_drc
    ET.SubElement(drc_el, "rf_mode_drc_profile").text  = p.rf_mode_drc

    # boolean: true or false
    ET.SubElement(enc, "lfe_lowpass_filter").text = (
        "true" if p.lfe_lowpass_filter else "false"
    )
    # boolean: true or false
    ET.SubElement(enc, "surround_90_degree_phase_shift").text = (
        "true" if p.surround_90_phase_shift else "false"
    )
    # One of: true, false
    ET.SubElement(enc, "surround_3db_attenuation").text = (
        "true" if p.surround_3db_attenuation else "false"
    )

    # ── Downmix metadata ─────────────────────────────────────────────────
    _add_downmix(enc, p.downmix)

    # ── Embedded timecodes ───────────────────────────────────────────────
    tc_el = ET.SubElement(enc, "embedded_timecodes")
    # 'off' disables embedded timecodes, 'auto' uses 'start' value
    ET.SubElement(tc_el, "starting_timecode").text = p.starting_timecode
    # One of: auto, 23.976, 24, 25, 29.97, 30, 50, 59.94, 60
    ET.SubElement(tc_el, "frame_rate").text = p.tc_frame_rate_out

    # ── Output ────────────────────────────────────────────────────────────
    out_el = ET.SubElement(root, "output")
    fmt = p.output_format
    # normalize ec3 → eac3 ใน XML tag ด้วย
    out_tag = "ec3" if fmt in ("eac3", "ec3") else fmt   # DEE ใช้ <ec3> สำหรับ EAC3
    out_fmt_el = ET.SubElement(out_el, out_tag, version="1")
    ET.SubElement(out_fmt_el, "file_name").text = p.output_file
    storage_out = ET.SubElement(out_fmt_el, "storage")
    ET.SubElement(ET.SubElement(storage_out, "local"), "path").text = str(p.output_path)

    # ── Misc ──────────────────────────────────────────────────────────────
    misc = ET.SubElement(root, "misc")
    tmp = ET.SubElement(misc, "temp_dir")
    ET.SubElement(tmp, "clean_temp").text = "true" if p.clean_temp else "false"
    ET.SubElement(tmp, "path").text = str(p.temp_dir)

    # Pretty-print
    raw = ET.tostring(root, encoding="unicode", xml_declaration=False)
    dom = minidom.parseString(raw)
    return dom.toprettyxml(indent="  ", encoding=None)


# ==============================================================================
# 6. DEE runner
# ==============================================================================
class DdpEncodeJob:
    """รันงาน DEE encode ครบวงจร: สร้าง XML → เรียก DEE → รายงานผล"""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self._dee_bin: Optional[str] = None

    def _resolve_tools(self) -> None:
        self._dee_bin = find_dee(getattr(self.args, "dee", None))
        if not self.args.quiet:
            print(f"[DEE] {self._dee_bin}", file=sys.stderr)

    def _detect_output_ext(self, encoder_mode: str, output_format: str) -> str:
        if output_format == "mp4":
            return ".mp4"
        if encoder_mode == "dd":
            return ".ac3"
        return ".eac3"

    def _build_params(self) -> EncodeParams:
        args = self.args
        input_p = Path(args.input).expanduser().resolve()
        if not input_p.is_file():
            raise DdpEncodeError(f"ไม่พบไฟล์ input: {input_p}", exit_code=5)

        encoder_mode  = getattr(args, "encoder_mode", "ddp")
        output_format = getattr(args, "output_format", "eac3")

        # output
        if args.output:
            out_full = Path(args.output).expanduser().resolve()
        else:
            ext = self._detect_output_ext(encoder_mode, output_format)
            out_full = input_p.parent / (input_p.stem + ".encoded" + ext)
        out_full.parent.mkdir(parents=True, exist_ok=True)

        tmp_dir = Path(getattr(args, "temp_dir", None) or tempfile.gettempdir())
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # loudness params
        lp = LoudnessParams(
            mode=getattr(args, "loudness_mode", "measure_and_correct"),
            preset=getattr(args, "loudness_preset", "atsc_a85"),
            metering_mode=getattr(args, "metering_mode", "1770-3"),
            dialogue_intelligence=not getattr(args, "no_di", False),
            speech_threshold=getattr(args, "speech_threshold", 15),
            correction_mode=getattr(args, "correction_mode", "pcm_scale"),
            loudness_target=getattr(args, "loudness_target", -24),
            peak_limit_db=getattr(args, "peak_limit_db", -2.0),
            peak_reference=getattr(args, "peak_reference", "true_peak"),
            measure_corrected=getattr(args, "measure_corrected", False),
        )

        # downmix metadata params
        dm = DownmixParams(
            loro_center_mix_level=getattr(args, "loro_center", "-3"),
            loro_surround_mix_level=getattr(args, "loro_surround", "-3"),
            ltrt_center_mix_level=getattr(args, "ltrt_center", "-3"),
            ltrt_surround_mix_level=getattr(args, "ltrt_surround", "-3"),
            preferred_downmix_mode=getattr(args, "preferred_downmix", "loro"),
            allow_hybrid_downmix=getattr(args, "allow_hybrid_downmix", False),
        )

        p = EncodeParams(
            # input
            input_path=input_p.parent,
            input_file=input_p.name,
            input_tc_rate=getattr(args, "input_tc_rate", "not_indicated"),
            input_offset=getattr(args, "input_offset", "auto"),
            input_ffoa=getattr(args, "input_ffoa", "auto"),
            # wav_list
            wav_list_mode=getattr(args, "wav_list", False),
            channel_configuration=getattr(args, "channel_config", "5.1"),
            # encoder
            encoder_mode=encoder_mode,
            bitstream_mode=getattr(args, "bitstream_mode", "complete_main"),
            downmix_config=getattr(args, "downmix_config", "off"),
            data_rate=getattr(args, "data_rate", 192),
            timecode_frame_rate=getattr(args, "tc_rate", "not_indicated"),
            start=getattr(args, "start", "first_frame_of_action"),
            end=getattr(args, "end", "end_of_file"),
            time_base=getattr(args, "time_base", "file_position"),
            prepend_silence=getattr(args, "prepend_silence", "0.0"),
            append_silence=getattr(args, "append_silence", "0.0"),
            lfe_on=not getattr(args, "no_lfe", False),
            dolby_surround_mode=getattr(args, "dolby_surround_mode", "not_indicated"),
            dolby_surround_ex_mode=getattr(args, "dolby_surround_ex_mode", "no"),
            user_data=getattr(args, "user_data", -1),
            line_mode_drc=getattr(args, "line_drc", "film_light"),
            rf_mode_drc=getattr(args, "rf_drc", "film_light"),
            lfe_lowpass_filter=not getattr(args, "no_lfe_lp", False),
            surround_90_phase_shift=not getattr(args, "no_surround_phase", False),
            surround_3db_attenuation=not getattr(args, "no_surround_3db", False),
            loudness=lp,
            downmix=dm,
            starting_timecode=getattr(args, "starting_timecode", "off"),
            tc_frame_rate_out=getattr(args, "tc_frame_rate_out", "auto"),
            # output
            output_path=out_full.parent,
            output_file=out_full.name,
            output_format=output_format,
            # misc
            temp_dir=tmp_dir,
            clean_temp=not getattr(args, "keep_temp", False),
        )
        return p

    def _write_xml(self, p: EncodeParams, xml_dir: Path) -> Path:
        xml_content = build_encode_xml(p)
        xml_path = xml_dir / f"ddp_encode_{p.input_file}.xml"
        xml_path.write_text(xml_content, encoding="utf-8")
        return xml_path

    def _run_dee(self, xml_path: Path) -> int:
        cmd = [self._dee_bin, "-x", str(xml_path)]
        if not self.args.quiet:
            print(f"\n[DEE] เรียก: {' '.join(cmd)}", file=sys.stderr)
            print("[DEE] กำลังทำงาน...", file=sys.stderr)

        stdout_pipe = None if getattr(self.args, "verbose", False) else subprocess.DEVNULL
        try:
            proc = subprocess.run(cmd, stdout=stdout_pipe)
            return proc.returncode
        except FileNotFoundError:
            raise DdpEncodeError(f"ไม่พบ DEE: {self._dee_bin}", exit_code=4)
        except KeyboardInterrupt:
            raise DdpEncodeError("ผู้ใช้ยกเลิกการทำงาน", exit_code=130)

    def _report(self, p: EncodeParams, rc: int) -> None:
        if self.args.quiet:
            return
        out_file = p.output_path / p.output_file
        print("\n" + "=" * 70, file=sys.stderr)
        if rc == 0:
            print("✓  DEE encode สำเร็จ", file=sys.stderr)
            print(f"   ไฟล์ผลลัพธ์   : {out_file}", file=sys.stderr)
            print(f"   encoder_mode   : {p.encoder_mode}", file=sys.stderr)
            print(f"   data_rate      : {p.data_rate} kbps", file=sys.stderr)
            print(f"   loudness preset: {p.loudness.preset}", file=sys.stderr)
        else:
            print(f"✗  DEE ล้มเหลว (exit code {rc})", file=sys.stderr)
        print("=" * 70, file=sys.stderr)

    def run(self) -> int:
        self._resolve_tools()
        p = self._build_params()
        _validate_params(p)

        # dry-run
        if getattr(self.args, "dry_run", False):
            xml_str = build_encode_xml(p)
            if getattr(self.args, "json_out", False):
                import dataclasses
                print(json.dumps({
                    "xml": xml_str,
                }, ensure_ascii=False, indent=2))
            else:
                print("[Dry-run] XML Job Config ที่จะส่งให้ DEE:\n")
                print(xml_str)
            return 0

        # ตรวจ output ซ้ำ
        out_file = p.output_path / p.output_file
        if out_file.exists() and not getattr(self.args, "yes", False):
            try:
                ans = input(f"ไฟล์ '{out_file}' มีอยู่แล้ว เขียนทับหรือไม่? (y/N): ")
            except EOFError:
                ans = "n"
            if ans.strip().lower() not in ("y", "yes"):
                print("ยกเลิก — ไม่เขียนทับ", file=sys.stderr)
                return 0

        with tempfile.TemporaryDirectory(prefix="ddp_encode_") as xml_tmp:
            xml_path = self._write_xml(p, Path(xml_tmp))
            if getattr(self.args, "verbose", False):
                print(f"\n[XML] {xml_path}", file=sys.stderr)
                print(xml_path.read_text(encoding="utf-8"), file=sys.stderr)
            rc = self._run_dee(xml_path)

        self._report(p, rc)
        return 0 if rc == 0 else 1


# ==============================================================================
# 7. Public API
# ==============================================================================
def encode(
    input_file: str,
    output_file: Optional[str] = None,
    *,
    encoder_mode: str = "ddp",
    output_format: str = "eac3",
    data_rate: int = 192,
    bitstream_mode: str = "complete_main",
    downmix_config: str = "off",
    lfe_on: bool = True,
    dolby_surround_mode: str = "not_indicated",
    dolby_surround_ex_mode: str = "no",
    user_data: int = -1,
    line_drc: str = "film_light",
    rf_drc: str = "film_light",
    lfe_lowpass_filter: bool = True,
    surround_90_phase_shift: bool = True,
    surround_3db_attenuation: bool = True,
    loudness_preset: str = "atsc_a85",
    loudness_mode: str = "measure_and_correct",
    loro_center: str = "-3",
    loro_surround: str = "-3",
    ltrt_center: str = "-3",
    ltrt_surround: str = "-3",
    preferred_downmix: str = "loro",
    temp_dir: Optional[str] = None,
    clean_temp: bool = True,
    dee_bin: Optional[str] = None,
    dry_run: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    overwrite: bool = False,
) -> int:
    """
    Public API สำหรับ import ใช้ในโปรเจกต์อื่น

    ตัวอย่าง:
        from lib.ddp_encode import encode
        rc = encode("input.wav", "output.eac3", encoder_mode="ddp", data_rate=192)

    คืนค่า: 0 = สำเร็จ, ≠0 = ล้มเหลว
    """
    class _NS:
        pass

    ns = _NS()
    ns.input = input_file
    ns.output = output_file
    ns.encoder_mode = encoder_mode
    ns.output_format = output_format
    ns.data_rate = data_rate
    ns.bitstream_mode = bitstream_mode
    ns.downmix_config = downmix_config
    ns.no_lfe = not lfe_on
    ns.dolby_surround_mode = dolby_surround_mode
    ns.dolby_surround_ex_mode = dolby_surround_ex_mode
    ns.line_drc = line_drc
    ns.rf_drc = rf_drc
    ns.no_lfe_lp = not lfe_lowpass_filter
    ns.no_surround_phase = not surround_90_phase_shift
    ns.no_surround_3db = not surround_3db_attenuation
    ns.loudness_preset = loudness_preset
    ns.loudness_mode = loudness_mode
    ns.loro_center = loro_center
    ns.loro_surround = loro_surround
    ns.ltrt_center = ltrt_center
    ns.ltrt_surround = ltrt_surround
    ns.preferred_downmix = preferred_downmix
    ns.temp_dir = temp_dir
    ns.keep_temp = not clean_temp
    ns.dee = dee_bin
    ns.dry_run = dry_run
    ns.json_out = False
    ns.verbose = verbose
    ns.quiet = quiet
    ns.yes = overwrite
    # defaults
    ns.input_tc_rate = "not_indicated"
    ns.input_offset = "auto"
    ns.input_ffoa = "auto"
    ns.wav_list = False
    ns.channel_config = "5.1"
    ns.tc_rate = "not_indicated"
    ns.start = "first_frame_of_action"
    ns.end = "end_of_file"
    ns.time_base = "file_position"
    ns.prepend_silence = "0.0"
    ns.append_silence = "0.0"
    ns.user_data = user_data
    ns.starting_timecode = "off"
    ns.tc_frame_rate_out = "auto"
    ns.metering_mode = "1770-3"
    ns.no_di = False
    ns.speech_threshold = 15
    ns.correction_mode = "pcm_scale"
    ns.loudness_target = -24
    ns.peak_limit_db = -2.0
    ns.peak_reference = "true_peak"
    ns.measure_corrected = False
    ns.allow_hybrid_downmix = False

    job = DdpEncodeJob(ns)  # type: ignore[arg-type]
    return job.run()


# ==============================================================================
# 8. CLI Arguments & Thai Help
# ==============================================================================
HELP_TEXT_THAI = """\
เข้ารหัส WAV → AC-3 หรือ E-AC-3 ด้วย Dolby Encoding Engine (DEE)

ใช้ไฟล์นี้เมื่อมี WAV ที่จัด channel มาแล้ว:
  - 5.1 WAV → AC-3/E-AC-3 5.1
  - 7.1 WAV → E-AC-3 7.1
  - stereo WAV → AC-3/E-AC-3 2.0

ถ้าต้องการแปลง 5.1 เป็น Lo/Ro หรือ Lt/Rt ก่อน encode
ให้ใช้ 5.1to2ddp.py แทน ไม่ควรใช้ --downmix-config เพื่อสร้าง DPL2
เพราะค่า downmix/Dolby Surround ใน DEE เป็น metadata ของ bitstream
ไม่ใช่ DSP matrix

โหมดหลัก:
  --encoder-mode dd    = AC-3 (Dolby Digital)
  --encoder-mode ddp   = E-AC-3 5.1 หรือ stereo
  --encoder-mode ddp71 = E-AC-3 7.1

Loudness:
  measure_and_correct = วัดและปรับ loudness
  measure_only        = วัดอย่างเดียว ไม่แก้ PCM
  สำหรับ AC-3 (dd) และ 7.1 ให้ใช้ measure_only ตาม DEE template

Metadata ที่พบบ่อย:
  --dolby-surround-mode yes|no|not_indicated
  --preferred-downmix loro|ltrt
  --user-data -1..65535
  หมายเหตุ: --user-data ไม่ใช่การกำหนด dialnorm โดยตรง

ตัวอย่าง:
  # WAV 5.1 -> E-AC-3 192 kbps
  python lib/ddp_encode.py -i movie_51.wav -o movie.eac3 --encoder-mode ddp --data-rate 192

  # WAV stereo Lt/Rt -> AC-3 192 kbps พร้อม Dolby Surround metadata
  python lib/ddp_encode.py -i movie_ltrt.wav -o movie.ac3 --encoder-mode dd --output-format ac3 --data-rate 192 --no-lfe --loudness-mode measure_only --dolby-surround-mode yes --preferred-downmix ltrt

  # แสดง XML ที่จะส่งให้ DEE โดยไม่ encode
  python lib/ddp_encode.py -i movie.wav --dry-run

รหัสสถานะการสิ้นสุด (Exit Codes):
  0   สำเร็จเรียบร้อย
  1   เกิดข้อผิดพลาดทั่วไป
  2   การกำหนดอาร์กิวเมนต์ไม่ถูกต้อง
  3   ฟอร์แมตไฟล์ไม่รองรับ
  4   ไม่พบ DEE หรือ DEE ทำงานล้มเหลว
  5   เกิดข้อผิดพลาดในการอ่าน/เขียนไฟล์ (I/O Error)
  130 ผู้ใช้ยกเลิกการทำงาน (Ctrl+C)
"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ddp_encode.py",
        description=HELP_TEXT_THAI,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── I/O ───────────────────────────────────────────────────────────────
    parser.add_argument("-i", "--input",  required=True,
                        help="พาธไฟล์ WAV input (polyfile)")
    parser.add_argument("-o", "--output",
                        help="พาธไฟล์ผลลัพธ์ (ค่าเริ่มต้น: <input>.encoded.<ext>)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="เขียนทับไฟล์ผลลัพธ์เดิมโดยไม่ถาม")

    # ── Encoder mode / format ─────────────────────────────────────────────
    parser.add_argument(
        "--encoder-mode", dest="encoder_mode",
        choices=["dd", "ddp", "ddp71"],
        default="ddp",
        help="โหมด encoder: dd=AC3 | ddp=EAC3 5.1 | ddp71=EAC3 7.1 (ค่าเริ่มต้น: ddp)",
    )
    parser.add_argument(
        "--output-format", dest="output_format",
        choices=["ac3", "eac3", "ec3", "mp4"],
        default="eac3",
        help="ฟอร์แมต output: ac3 | eac3 | mp4 (ค่าเริ่มต้น: eac3)",
    )

    # ── DEE filter passthrough (pcm_to_ddp) ──────────────────────────────
    parser.add_argument(
        "--data-rate", dest="data_rate",
        type=int, default=192, metavar="KBPS",
        help="Bitrate kbps (ค่าเริ่มต้น: 192 | สูงสุด: dd=448, ddp/ddp71=1024)",
    )
    parser.add_argument(
        "--bitstream-mode", dest="bitstream_mode",
        choices=sorted(VALID_BSTREAM_MODE),
        default="complete_main",
        help="Bitstream mode (ค่าเริ่มต้น: complete_main)",
    )
    parser.add_argument(
        "--downmix-config", dest="downmix_config",
        choices=["off", "mono", "stereo", "5.1"],
        default="off",
        help="Downmix config: off | mono | stereo | 5.1 (ค่าเริ่มต้น: off)",
    )
    parser.add_argument(
        "--no-lfe", dest="no_lfe",
        action="store_true",
        help="ปิด LFE channel (ค่าเริ่มต้น: เปิด)",
    )
    parser.add_argument(
        "--tc-rate", dest="tc_rate",
        choices=sorted(VALID_TC_RATE),
        default="not_indicated",
        help="Timecode frame rate (ค่าเริ่มต้น: not_indicated)",
    )
    parser.add_argument("--start", default="first_frame_of_action",
                        help="ตำแหน่งเริ่มต้น (ค่าเริ่มต้น: first_frame_of_action)")
    parser.add_argument("--end", default="end_of_file",
                        help="ตำแหน่งสิ้นสุด (ค่าเริ่มต้น: end_of_file)")
    parser.add_argument(
        "--time-base", dest="time_base",
        choices=["file_position", "embedded_timecode"],
        default="file_position",
        help="Time base (ค่าเริ่มต้น: file_position)",
    )
    parser.add_argument(
        "--prepend-silence", dest="prepend_silence",
        default="0.0", metavar="SEC",
        help="เติม silence ก่อน input (วินาที, ค่าเริ่มต้น: 0.0)",
    )
    parser.add_argument(
        "--append-silence", dest="append_silence",
        default="0.0", metavar="SEC",
        help="เติม silence หลัง input (วินาที, ค่าเริ่มต้น: 0.0)",
    )
    parser.add_argument(
        "--dolby-surround-mode", dest="dolby_surround_mode",
        choices=["yes", "no", "not_indicated"],
        default="not_indicated",
        help="Dolby Surround mode flag (ค่าเริ่มต้น: not_indicated)",
    )
    parser.add_argument(
        "--dolby-surround-ex-mode", dest="dolby_surround_ex_mode",
        choices=["yes", "no", "not_indicated"],
        default="no",
        help="Dolby Surround EX mode flag (ค่าเริ่มต้น: no)",
    )
    parser.add_argument(
        "--user-data", dest="user_data",
        type=int, default=-1, metavar="-1..65535",
        help="User data field (integer -1 ถึง 65535, -1=auto, ค่าเริ่มต้น: -1)",
    )

    # ── DRC ───────────────────────────────────────────────────────────────
    drc_choices = sorted(VALID_DRC_PROFILE)
    parser.add_argument(
        "--line-drc", dest="line_drc",
        choices=drc_choices, default="film_light",
        help="Line mode DRC profile (ค่าเริ่มต้น: film_light)",
    )
    parser.add_argument(
        "--rf-drc", dest="rf_drc",
        choices=drc_choices, default="film_light",
        help="RF mode DRC profile (ค่าเริ่มต้น: film_light)",
    )

    # ── Processing flags ──────────────────────────────────────────────────
    parser.add_argument(
        "--no-lfe-lp", dest="no_lfe_lp",
        action="store_true",
        help="ปิด LFE Low-pass filter (ค่าเริ่มต้น: เปิด)",
    )
    parser.add_argument(
        "--no-surround-phase", dest="no_surround_phase",
        action="store_true",
        help="ปิด Surround 90° phase shift (ค่าเริ่มต้น: เปิด)",
    )
    parser.add_argument(
        "--no-surround-3db", dest="no_surround_3db",
        action="store_true",
        help="ปิด Surround -3 dB attenuation (ค่าเริ่มต้น: เปิด)",
    )

    # ── Loudness ──────────────────────────────────────────────────────────
    parser.add_argument(
        "--loudness-mode", dest="loudness_mode",
        choices=["measure_and_correct", "measure_only"],
        default="measure_and_correct",
        help="โหมด loudness: measure_and_correct (ค่าเริ่มต้น) | measure_only",
    )
    parser.add_argument(
        "--loudness-preset", dest="loudness_preset",
        choices=sorted(VALID_LOUDNESS_PRESET),
        default="atsc_a85",
        help="Loudness preset (ค่าเริ่มต้น: atsc_a85) — ใช้กับ measure_and_correct",
    )
    # manual loudness params
    parser.add_argument(
        "--metering-mode", dest="metering_mode",
        choices=["1770-3", "1770-2", "1770-1", "LeqA"],
        default="1770-3",
        help="[manual] Metering mode (ค่าเริ่มต้น: 1770-3)",
    )
    parser.add_argument("--no-di", dest="no_di", action="store_true",
                        help="[manual] ปิด Dialogue Intelligence")
    parser.add_argument(
        "--speech-threshold", dest="speech_threshold",
        type=int, default=15, metavar="0-100",
        help="[manual] Speech threshold 0–100 (ค่าเริ่มต้น: 15)",
    )
    parser.add_argument(
        "--correction-mode", dest="correction_mode",
        choices=["pcm_scale", "md_update"], default="pcm_scale",
        help="[manual] Correction mode (ค่าเริ่มต้น: pcm_scale)",
    )
    parser.add_argument(
        "--loudness-target", dest="loudness_target",
        type=int, default=-24, metavar="-31..-8",
        help="[manual] Loudness target LKFS (ค่าเริ่มต้น: -24)",
    )
    parser.add_argument(
        "--peak-limit-db", dest="peak_limit_db",
        type=float, default=-2.0, metavar="-12..-0.1",
        help="[manual] True peak limit dBTP (ค่าเริ่มต้น: -2.0)",
    )
    parser.add_argument(
        "--peak-reference", dest="peak_reference",
        choices=["true_peak", "sample_peak"], default="true_peak",
        help="[manual] Peak reference (ค่าเริ่มต้น: true_peak)",
    )
    parser.add_argument(
        "--measure-corrected", dest="measure_corrected",
        action="store_true",
        help="[manual] วัด loudness หลัง correction ด้วย",
    )

    # ── Downmix metadata ─────────────────────────────────────────────────
    parser.add_argument(
        "--loro-center", dest="loro_center",
        choices=sorted(VALID_MIX_CENTER), default="-3",
        help="LoRo Center mix level (ค่าเริ่มต้น: -3 dB)",
    )
    parser.add_argument(
        "--loro-surround", dest="loro_surround",
        choices=sorted(VALID_MIX_SURROUND), default="-3",
        help="LoRo Surround mix level (ค่าเริ่มต้น: -3 dB)",
    )
    parser.add_argument(
        "--ltrt-center", dest="ltrt_center",
        choices=sorted(VALID_MIX_CENTER), default="-3",
        help="LtRt Center mix level (ค่าเริ่มต้น: -3 dB)",
    )
    parser.add_argument(
        "--ltrt-surround", dest="ltrt_surround",
        choices=sorted(VALID_MIX_SURROUND), default="-3",
        help="LtRt Surround mix level (ค่าเริ่มต้น: -3 dB)",
    )
    parser.add_argument(
        "--preferred-downmix", dest="preferred_downmix",
        choices=sorted(VALID_DOWNMIX_MODE), default="loro",
        help="Preferred downmix mode (ค่าเริ่มต้น: loro)",
    )
    parser.add_argument(
        "--allow-hybrid-downmix", dest="allow_hybrid_downmix",
        action="store_true",
        help="อนุญาต hybrid downmix (ค่าเริ่มต้น: false)",
    )

    # ── Timecodes ─────────────────────────────────────────────────────────
    parser.add_argument(
        "--starting-timecode", dest="starting_timecode",
        default="off",
        help="Embedded timecode เริ่มต้น: 'off' | 'auto' | H:M:S.ms (ค่าเริ่มต้น: off)",
    )
    parser.add_argument(
        "--tc-frame-rate-out", dest="tc_frame_rate_out",
        choices=sorted(VALID_TC_RATE_OUT), default="auto",
        help="Frame rate ของ embedded timecode (ค่าเริ่มต้น: auto)",
    )

    # ── Paths ─────────────────────────────────────────────────────────────
    parser.add_argument("--dee", default=None,
                        help="ระบุพาธไปยังโปรแกรม dee โดยตรง")
    parser.add_argument(
        "--temp-dir", dest="temp_dir", default=None, metavar="PATH",
        help="โฟลเดอร์ temp สำหรับ DEE (ค่าเริ่มต้น: %%TEMP%%)",
    )
    parser.add_argument(
        "--keep-temp", dest="keep_temp", action="store_true",
        help="ไม่ลบไฟล์ temp หลังจาก DEE ทำงานเสร็จ",
    )

    # ── Misc ──────────────────────────────────────────────────────────────
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="แสดง XML ที่จะส่งให้ DEE โดยไม่เรียก DEE จริง")
    parser.add_argument("--json", dest="json_out", action="store_true",
                        help="ส่งออก dry-run ในรูปแบบ JSON")
    parser.add_argument(
        "--list-rates", dest="list_rates", action="store_true",
        help="แสดงรายการ data_rate ทั้งหมดที่ DEE รองรับแล้วออก",
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="แสดงข้อความการทำงานอย่างละเอียด")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="ปิดการแสดงข้อความ (ยกเว้น error)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")

    return parser


# ==============================================================================
# 9. Entry Point
# ==============================================================================
def main() -> None:
    parser = build_arg_parser()
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(2)

    if "--list-rates" in sys.argv:
        rates = sorted(VALID_DATA_RATES)
        print(f"Data rates ที่ DEE รองรับ ({len(rates)} ค่า):")
        print("  " + ", ".join(str(r) for r in rates) + " kbps")
        print("\nขีดสูงสุดตาม encoder mode:")
        for mode, maxr in MAX_DATA_RATE.items():
            print(f"  {mode}: สูงสุด {maxr} kbps")
        sys.exit(0)

    args = parser.parse_args()

    try:
        job = DdpEncodeJob(args)
        rc = job.run()
        sys.exit(rc)
    except DdpEncodeError as e:
        print(f"\n[ข้อผิดพลาด] {e.message}", file=sys.stderr)
        sys.exit(e.exit_code)
    except KeyboardInterrupt:
        print("\n[ABORT] ผู้ใช้ยกเลิกการทำงาน", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"\n[ข้อผิดพลาดร้ายแรง] {e}", file=sys.stderr)
        if getattr(args, "verbose", False):
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
