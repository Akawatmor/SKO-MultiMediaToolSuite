#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib/ddp_decode.py
===============================================================================
Wrapper สำหรับ Dolby Encoding Engine (DEE) — ถอดรหัสไฟล์ AC3 / EAC3 → WAV
รองรับการนำเข้าเป็นโมดูล (Importable Module) และการรันเป็น CLI โดยตรง

ฟอร์แมตที่ DEE รองรับผ่าน Wrapper นี้:
  - AC3  (Dolby Digital 5.1)   ← ถอดด้วย ddp_decode filter ของ DEE
  - EAC3 (Dolby Digital Plus 5.1 / non_atmos, atmos)  ← เช่นเดียวกัน

ฟอร์แมตอื่น ๆ (TrueHD, DTS, PCM ฯลฯ):
  → ใช้ ffmpeg wrapper โดยตรง (ไม่อยู่ในโมดูลนี้)

กระบวนการทำงาน:
  1. สร้างไฟล์ XML Job Config ชั่วคราวในโฟลเดอร์ temp
  2. เรียก dee <job.xml> รอจนเสร็จ
  3. ลบไฟล์ temp ทิ้ง (เว้นแต่ --keep-temp)
  4. รายงานผลลัพธ์ (และ Manifest XML ถ้ามี)

ข้อมูลพารามิเตอร์ XML ทั้งหมด (ที่มี comment ใน template ของ DEE):
  ดู build_decode_xml() สำหรับรายละเอียดครบถ้วน
===============================================================================
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import xml.dom.minidom as minidom
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

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

# ฟอร์แมตที่ wrapper นี้รองรับผ่าน DEE
DEE_SUPPORTED_FORMATS = {"ac3", "eac3", "ec3"}

# ชุดค่า passthrough ที่ DEE รองรับ (สำหรับ validation)
VALID_DOWNMIX    = {"off", "mono", "stereo", "5.1"}
VALID_STEREO     = {"auto", "loro", "ltrt"}
VALID_DRC        = {"none", "line", "rf"}
VALID_TC_RATE    = {"not_indicated", "23.976", "24", "25", "29.97",
                    "30", "48", "50", "59.94", "60"}
VALID_METER      = {"1770-3", "1770-2", "1770-1", "LeqA"}
VALID_STREAM_TYPE = {"non_atmos", "atmos"}


# ==============================================================================
# 2. ข้อยกเว้นและ Dataclass พารามิเตอร์
# ==============================================================================
class DdpDecodeError(Exception):
    """ข้อผิดพลาดที่คาดการณ์ได้ พร้อม exit_code"""
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


@dataclass
class DecodeParams:
    """
    พารามิเตอร์ทั้งหมดสำหรับ DEE ddp_decode job
    ค่าเหล่านี้ตรงกับ element ใน XML template ของ DEE ทุกประการ
    (รวม comment ที่ DEE ระบุค่าที่รองรับ)
    """
    # ─── input ───────────────────────────────────────────────────────────────
    input_path: Path                         # พาธโฟลเดอร์ที่มีไฟล์ต้นทาง
    input_file: str                          # ชื่อไฟล์ต้นทาง (ไม่รวมพาธ)
    input_format: str = "auto"               # "ac3" | "eac3" | "auto" (ตรวจจากนามสกุล)

    # ─── EAC3-specific ───────────────────────────────────────────────────────
    # One of: non_atmos, atmos  (ใช้เฉพาะเมื่อ input_format == "eac3")
    stream_type: str = "non_atmos"

    # ─── ddp_decode filter ───────────────────────────────────────────────────
    # integer: from 0 to 3
    threads: int = 3
    # One of: off, mono, stereo, 5.1
    downmix_config: str = "off"
    # One of: auto, loro, ltrt
    stereo_mode: str = "auto"
    # One of: none, line, rf
    drc: str = "none"
    # One of: not_indicated, 23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60
    timecode_frame_rate: str = "not_indicated"
    # string — ตำแหน่งเริ่มต้น (รูปแบบ H:M:S.ms)
    start: str = "0:0:0.0"
    # string — ตำแหน่งสิ้นสุด ("end_of_file" หรือ H:M:S.ms)
    end: str = "end_of_file"

    # ─── loudness (measure_only ใน decode — ไม่ auto-correct) ───────────────
    # One of: 1770-3, 1770-2, 1770-1, LeqA
    metering_mode: str = "1770-3"
    # boolean: true or false
    dialogue_intelligence: bool = True
    # integer: from 0 to 100
    speech_threshold: int = 15

    # ─── output ──────────────────────────────────────────────────────────────
    output_path: Path = field(default_factory=Path)   # โฟลเดอร์ผลลัพธ์
    output_file: str = ""                             # ชื่อไฟล์ WAV ผลลัพธ์
    # สร้าง manifest XML ควบคู่กับ WAV (ค่าเริ่มต้น: True)
    output_manifest: bool = True
    manifest_file: str = ""  # ชื่อไฟล์ manifest (ว่าง = ใช้ชื่อ output_file + .xml)

    # ─── misc ─────────────────────────────────────────────────────────────────
    temp_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()))
    # ลบ temp ทิ้งหลังเสร็จ
    clean_temp: bool = True


# ==============================================================================
# 3. ค้นหา DEE / ffmpeg ในระบบ
# ==============================================================================
def _find_tool(name: str, explicit: Optional[str] = None) -> Optional[str]:
    """ค้นหา executable ตามลำดับ: ระบุโดยตรง → ENV → PATH → โฟลเดอร์โปรเจกต์"""
    ext = ".exe" if os.name == "nt" else ""

    if explicit:
        p = Path(explicit).expanduser().resolve()
        if p.is_file():
            return str(p)
        c = p / (name + ext)
        if c.is_file():
            return str(c)

    env_key = f"{name.upper()}_BIN"
    env_val = os.environ.get(env_key)
    if env_val:
        p = Path(env_val).expanduser().resolve()
        if p.is_file():
            return str(p)

    # ค้นหาใน PATH
    found = shutil.which(name)
    if found:
        return found

    # ค้นหาในโฟลเดอร์โปรเจกต์
    cur = Path(__file__).resolve().parent
    for sub in [cur, cur.parent, cur.parent / "tools", cur.parent / "bin"]:
        c = sub / (name + ext)
        if c.is_file():
            return str(c)

    return None


def find_dee(explicit: Optional[str] = None) -> str:
    """ค้นหา dee หรือ dee.exe แล้วคืนพาธ ถ้าไม่พบ raise DdpDecodeError"""
    # DEE อาจมีชื่อต่างกัน
    for candidate in ("dee", "dee.exe", "dolby_encoding_engine"):
        result = _find_tool(candidate, explicit)
        if result:
            return result
    raise DdpDecodeError(
        "ไม่พบโปรแกรม DEE (Dolby Encoding Engine) ในระบบ\n"
        "กรุณาติดตั้ง DEE และเพิ่มลงใน PATH หรือระบุพาธด้วย --dee",
        exit_code=4,
    )


def find_ffmpeg(explicit: Optional[str] = None) -> Optional[str]:
    """ค้นหา ffmpeg — คืน None ถ้าไม่พบ (ไม่บังคับ)"""
    return _find_tool("ffmpeg", explicit)


def find_ffprobe(explicit: Optional[str] = None) -> Optional[str]:
    """ค้นหา ffprobe"""
    return _find_tool("ffprobe", explicit)


# ==============================================================================
# 4. ตรวจสอบ format ไฟล์ input
# ==============================================================================
def _detect_format(file_path: Path) -> str:
    """ตรวจจาก extension → 'ac3' | 'eac3'"""
    ext = file_path.suffix.lower().lstrip(".")
    if ext in ("ac3",):
        return "ac3"
    if ext in ("eac3", "ec3"):
        return "eac3"
    return ext  # ส่งคืนตามที่ตรวจได้


def _validate_params(p: DecodeParams) -> None:
    """ตรวจสอบความถูกต้องของพารามิเตอร์ก่อนสร้าง XML"""
    if p.input_format not in DEE_SUPPORTED_FORMATS:
        raise DdpDecodeError(
            f"ฟอร์แมต '{p.input_format}' ไม่ได้รับการรองรับโดย DEE\n"
            f"ฟอร์แมตที่รองรับ: {', '.join(sorted(DEE_SUPPORTED_FORMATS))}\n"
            f"สำหรับฟอร์แมตอื่น (TrueHD, DTS, PCM ฯลฯ) ใช้ ffmpeg wrapper โดยตรง",
            exit_code=3,
        )
    if p.downmix_config not in VALID_DOWNMIX:
        raise DdpDecodeError(
            f"--downmix '{p.downmix_config}' ไม่ถูกต้อง ค่าที่รองรับ: {VALID_DOWNMIX}",
            exit_code=2,
        )
    if p.stereo_mode not in VALID_STEREO:
        raise DdpDecodeError(
            f"--stereo-mode '{p.stereo_mode}' ไม่ถูกต้อง ค่าที่รองรับ: {VALID_STEREO}",
            exit_code=2,
        )
    if p.drc not in VALID_DRC:
        raise DdpDecodeError(
            f"--drc '{p.drc}' ไม่ถูกต้อง ค่าที่รองรับ: {VALID_DRC}",
            exit_code=2,
        )
    if p.timecode_frame_rate not in VALID_TC_RATE:
        raise DdpDecodeError(
            f"--tc-rate '{p.timecode_frame_rate}' ไม่ถูกต้อง ค่าที่รองรับ: {VALID_TC_RATE}",
            exit_code=2,
        )
    if p.metering_mode not in VALID_METER:
        raise DdpDecodeError(
            f"--metering-mode '{p.metering_mode}' ไม่ถูกต้อง ค่าที่รองรับ: {VALID_METER}",
            exit_code=2,
        )
    if p.stream_type not in VALID_STREAM_TYPE:
        raise DdpDecodeError(
            f"--stream-type '{p.stream_type}' ไม่ถูกต้อง ค่าที่รองรับ: {VALID_STREAM_TYPE}",
            exit_code=2,
        )
    if not (0 <= p.threads <= 3):
        raise DdpDecodeError("--threads ต้องอยู่ระหว่าง 0 ถึง 3", exit_code=2)
    if not (0 <= p.speech_threshold <= 100):
        raise DdpDecodeError("--speech-threshold ต้องอยู่ระหว่าง 0 ถึง 100", exit_code=2)


# ==============================================================================
# 5. สร้าง XML Job Config
# ==============================================================================
def build_decode_xml(p: DecodeParams) -> str:
    """
    สร้าง XML job config สำหรับ DEE ddp_decode
    พารามิเตอร์ทุกตัวที่ DEE รองรับถูก passthrough มาจาก DecodeParams
    """
    root = ET.Element("job_config")

    # ── Input ──────────────────────────────────────────────────────────────
    inp = ET.SubElement(root, "input")
    audio_in = ET.SubElement(inp, "audio")

    fmt = p.input_format  # "ac3" หรือ "eac3"/"ec3"
    is_eac3 = fmt in ("eac3", "ec3")

    if is_eac3:
        # EAC3: <ec3 version="1">
        # One of: non_atmos, atmos
        ac_el = ET.SubElement(audio_in, "ec3", version="1")
        ET.SubElement(ac_el, "stream_type").text = p.stream_type
    else:
        # AC3: <ac3 version="1">
        ac_el = ET.SubElement(audio_in, "ac3", version="1")

    ET.SubElement(ac_el, "file_name").text = p.input_file
    storage_in = ET.SubElement(ac_el, "storage")
    local_in = ET.SubElement(storage_in, "local")
    ET.SubElement(local_in, "path").text = str(p.input_path)

    # ── Filter: ddp_decode ────────────────────────────────────────────────
    filt = ET.SubElement(root, "filter")
    audio_f = ET.SubElement(filt, "audio")
    dec = ET.SubElement(audio_f, "ddp_decode", version="1")

    # integer: from 0 to 3
    ET.SubElement(dec, "threads").text = str(p.threads)
    # One of: off, mono, stereo, 5.1
    ET.SubElement(dec, "downmix_config").text = p.downmix_config
    # One of: auto, loro, ltrt
    ET.SubElement(dec, "stereo_mode").text = p.stereo_mode
    # One of: none, line, rf
    ET.SubElement(dec, "drc").text = p.drc
    # One of: not_indicated, 23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60
    ET.SubElement(dec, "timecode_frame_rate").text = p.timecode_frame_rate
    # string — ตำแหน่งเริ่มต้น
    ET.SubElement(dec, "start").text = p.start
    # string — ตำแหน่งสิ้นสุด
    ET.SubElement(dec, "end").text = p.end

    # ── Loudness (measure_only — decode ไม่ auto-correct) ─────────────────
    loudness_el = ET.SubElement(dec, "loudness")
    mo = ET.SubElement(loudness_el, "measure_only")
    # One of: 1770-3, 1770-2, 1770-1, LeqA
    ET.SubElement(mo, "metering_mode").text = p.metering_mode
    # boolean: true or false
    ET.SubElement(mo, "dialogue_intelligence").text = (
        "true" if p.dialogue_intelligence else "false"
    )
    # integer: from 0 to 100
    ET.SubElement(mo, "speech_threshold").text = str(p.speech_threshold)

    # ── Output ────────────────────────────────────────────────────────────
    out = ET.SubElement(root, "output")

    wav_el = ET.SubElement(out, "wav", version="1")
    ET.SubElement(wav_el, "file_name").text = p.output_file
    storage_wav = ET.SubElement(wav_el, "storage")
    local_wav = ET.SubElement(storage_wav, "local")
    ET.SubElement(local_wav, "path").text = str(p.output_path)

    if p.output_manifest:
        man_el = ET.SubElement(out, "manifest", version="1")
        mf_name = p.manifest_file or (Path(p.output_file).stem + "_manifest.xml")
        ET.SubElement(man_el, "file_name").text = mf_name
        storage_man = ET.SubElement(man_el, "storage")
        local_man = ET.SubElement(storage_man, "local")
        ET.SubElement(local_man, "path").text = str(p.output_path)

    # ── Misc ──────────────────────────────────────────────────────────────
    misc = ET.SubElement(root, "misc")
    tmp = ET.SubElement(misc, "temp_dir")
    ET.SubElement(tmp, "clean_temp").text = "true" if p.clean_temp else "false"
    ET.SubElement(tmp, "path").text = str(p.temp_dir)

    # Pretty-print ด้วย minidom
    raw = ET.tostring(root, encoding="unicode", xml_declaration=False)
    dom = minidom.parseString(raw)
    pretty = dom.toprettyxml(indent="  ", encoding=None)
    # toprettyxml เพิ่ม <?xml?> declaration ให้อัตโนมัติ
    return pretty


# ==============================================================================
# 6. DEE runner
# ==============================================================================
class DdpDecodeJob:
    """รันงาน DEE decode ครบวงจร: สร้าง XML → เรียก DEE → รายงานผล"""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self._dee_bin: Optional[str] = None
        self._ffprobe_bin: Optional[str] = None

    # ── ค้นหาเครื่องมือ ────────────────────────────────────────────────────
    def _resolve_tools(self) -> None:
        self._dee_bin = find_dee(getattr(self.args, "dee", None))
        self._ffprobe_bin = find_ffprobe(getattr(self.args, "ffprobe", None))
        if not self.args.quiet:
            print(f"[DEE]     {self._dee_bin}", file=sys.stderr)
            if self._ffprobe_bin:
                print(f"[ffprobe] {self._ffprobe_bin}", file=sys.stderr)

    # ── ตรวจสอบ input ──────────────────────────────────────────────────────
    def _probe_input(self, input_path: Path) -> dict:
        """ใช้ ffprobe ตรวจสอบ format/codec ของไฟล์ input (ถ้ามี ffprobe)"""
        if not self._ffprobe_bin:
            return {}
        cmd = [
            self._ffprobe_bin, "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            str(input_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return json.loads(result.stdout) if result.returncode == 0 else {}
        except Exception:
            return {}

    # ── สร้าง DecodeParams จาก args ────────────────────────────────────────
    def _build_params(self) -> DecodeParams:
        args = self.args
        input_p = Path(args.input).expanduser().resolve()
        if not input_p.is_file():
            raise DdpDecodeError(f"ไม่พบไฟล์ input: {input_p}", exit_code=5)

        # ตรวจ format
        fmt = getattr(args, "format", "auto")
        if fmt == "auto":
            fmt = _detect_format(input_p)
        if fmt in ("ec3",):
            fmt = "eac3"

        # output path/file
        if args.output:
            out_full = Path(args.output).expanduser().resolve()
        else:
            out_full = input_p.parent / (input_p.stem + ".decoded.wav")
        out_full.parent.mkdir(parents=True, exist_ok=True)

        # temp dir
        tmp_dir = Path(getattr(args, "temp_dir", None) or tempfile.gettempdir())
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # manifest file name
        man_file = getattr(args, "manifest_file", "") or ""

        p = DecodeParams(
            # input
            input_path=input_p.parent,
            input_file=input_p.name,
            input_format=fmt,
            # eac3-specific
            stream_type=getattr(args, "stream_type", "non_atmos"),
            # ddp_decode filter
            threads=getattr(args, "threads", 3),
            downmix_config=getattr(args, "downmix", "off"),
            stereo_mode=getattr(args, "stereo_mode", "auto"),
            drc=getattr(args, "drc", "none"),
            timecode_frame_rate=getattr(args, "tc_rate", "not_indicated"),
            start=getattr(args, "start", "0:0:0.0"),
            end=getattr(args, "end", "end_of_file"),
            # loudness
            metering_mode=getattr(args, "metering_mode", "1770-3"),
            dialogue_intelligence=not getattr(args, "no_di", False),
            speech_threshold=getattr(args, "speech_threshold", 15),
            # output
            output_path=out_full.parent,
            output_file=out_full.name,
            output_manifest=not getattr(args, "no_manifest", False),
            manifest_file=man_file,
            # misc
            temp_dir=tmp_dir,
            clean_temp=not getattr(args, "keep_temp", False),
        )
        return p

    # ── เขียนไฟล์ XML ชั่วคราว ─────────────────────────────────────────────
    def _write_xml(self, p: DecodeParams, xml_dir: Path) -> Path:
        xml_content = build_decode_xml(p)
        xml_path = xml_dir / f"ddp_decode_{p.input_file}.xml"
        xml_path.write_text(xml_content, encoding="utf-8")
        return xml_path

    # ── เรียก DEE ──────────────────────────────────────────────────────────
    def _run_dee(self, xml_path: Path, verbose: bool) -> int:
        cmd = [self._dee_bin, "-x", str(xml_path)]
        if not self.args.quiet:
            print(f"\n[DEE] เรียก: {' '.join(cmd)}", file=sys.stderr)
            print("[DEE] กำลังทำงาน...", file=sys.stderr)

        stdout_pipe = None if verbose else subprocess.DEVNULL
        stderr_pipe = None  # ให้ DEE แสดงข้อความเสมอ (มี progress)

        try:
            proc = subprocess.run(
                cmd,
                stdout=stdout_pipe,
                stderr=stderr_pipe,
            )
            return proc.returncode
        except FileNotFoundError:
            raise DdpDecodeError(f"ไม่พบ DEE: {self._dee_bin}", exit_code=4)
        except KeyboardInterrupt:
            raise DdpDecodeError("ผู้ใช้ยกเลิกการทำงาน", exit_code=130)

    # ── รายงานผลลัพธ์ ──────────────────────────────────────────────────────
    def _report(self, p: DecodeParams, rc: int) -> None:
        if self.args.quiet:
            return
        out_wav = p.output_path / p.output_file
        print("\n" + "=" * 70, file=sys.stderr)
        if rc == 0:
            print("✓  DEE decode สำเร็จ", file=sys.stderr)
            print(f"   ไฟล์ WAV ผลลัพธ์  : {out_wav}", file=sys.stderr)
            if p.output_manifest:
                man = p.output_path / (p.manifest_file or (Path(p.output_file).stem + "_manifest.xml"))
                print(f"   ไฟล์ Manifest     : {man}", file=sys.stderr)
        else:
            print(f"✗  DEE ล้มเหลว (exit code {rc})", file=sys.stderr)
        print("=" * 70, file=sys.stderr)

    # ── main runner ────────────────────────────────────────────────────────
    def run(self) -> int:
        self._resolve_tools()
        p = self._build_params()
        _validate_params(p)

        # dry-run: แสดง XML แล้วออก
        if getattr(self.args, "dry_run", False):
            xml_str = build_decode_xml(p)
            if getattr(self.args, "json_out", False):
                import dataclasses
                print(json.dumps({
                    "xml": xml_str,
                    "params": {
                        k: str(v) for k, v in dataclasses.asdict(p).items()
                    },
                }, ensure_ascii=False, indent=2))
            else:
                print("[Dry-run] XML Job Config ที่จะส่งให้ DEE:\n")
                print(xml_str)
            return 0

        # ตรวจ output ซ้ำ
        out_wav = p.output_path / p.output_file
        if out_wav.exists() and not getattr(self.args, "yes", False):
            try:
                ans = input(f"ไฟล์ '{out_wav}' มีอยู่แล้ว เขียนทับหรือไม่? (y/N): ")
            except EOFError:
                ans = "n"
            if ans.strip().lower() not in ("y", "yes"):
                print("ยกเลิก — ไม่เขียนทับ", file=sys.stderr)
                return 0

        # สร้าง XML และเรียก DEE
        with tempfile.TemporaryDirectory(prefix="ddp_decode_") as xml_tmp:
            xml_path = self._write_xml(p, Path(xml_tmp))
            if getattr(self.args, "verbose", False):
                print(f"\n[XML] {xml_path}", file=sys.stderr)
                print(xml_path.read_text(encoding="utf-8"), file=sys.stderr)
            rc = self._run_dee(xml_path, getattr(self.args, "verbose", False))

        self._report(p, rc)
        return 0 if rc == 0 else 1


# ==============================================================================
# 7. Public API (เพื่อใช้ในฐานะ module)
# ==============================================================================
def decode(
    input_file: str,
    output_file: Optional[str] = None,
    *,
    input_format: str = "auto",
    stream_type: str = "non_atmos",
    threads: int = 3,
    downmix_config: str = "off",
    stereo_mode: str = "auto",
    drc: str = "none",
    timecode_frame_rate: str = "not_indicated",
    start: str = "0:0:0.0",
    end: str = "end_of_file",
    metering_mode: str = "1770-3",
    dialogue_intelligence: bool = True,
    speech_threshold: int = 15,
    output_manifest: bool = True,
    manifest_file: str = "",
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
        from lib.ddp_decode import decode
        rc = decode("input.ac3", "output.wav", downmix_config="off", drc="none")

    คืนค่า: 0 = สำเร็จ, ≠0 = ล้มเหลว
    """
    # สร้าง namespace ปลอมเพื่อใช้กับ DdpDecodeJob
    class _NS:
        pass

    ns = _NS()
    ns.input = input_file
    ns.output = output_file
    ns.format = input_format
    ns.stream_type = stream_type
    ns.threads = threads
    ns.downmix = downmix_config
    ns.stereo_mode = stereo_mode
    ns.drc = drc
    ns.tc_rate = timecode_frame_rate
    ns.start = start
    ns.end = end
    ns.metering_mode = metering_mode
    ns.no_di = not dialogue_intelligence
    ns.speech_threshold = speech_threshold
    ns.no_manifest = not output_manifest
    ns.manifest_file = manifest_file
    ns.temp_dir = temp_dir
    ns.keep_temp = not clean_temp
    ns.dee = dee_bin
    ns.ffprobe = None
    ns.dry_run = dry_run
    ns.json_out = False
    ns.verbose = verbose
    ns.quiet = quiet
    ns.yes = overwrite

    job = DdpDecodeJob(ns)  # type: ignore[arg-type]
    return job.run()


# ==============================================================================
# 8. CLI Arguments & Thai Help
# ==============================================================================
HELP_TEXT_THAI = """\
ถอดรหัสไฟล์ AC3 (Dolby Digital) หรือ EAC3 (Dolby Digital Plus) → WAV
ผ่าน Dolby Encoding Engine (DEE) ด้วย ddp_decode filter

รองรับการ passthrough ค่า parameter ทุกตัวที่ DEE รองรับใน job XML:

  threads           : จำนวน thread DEE (0–3, ค่าเริ่มต้น 3)
  downmix           : การ downmix ผลลัพธ์ (off/mono/stereo/5.1)
  stereo-mode       : โหมด stereo downmix (auto/loro/ltrt)
  drc               : Dynamic Range Control (none/line/rf)
  tc-rate           : Timecode frame rate
  start / end       : ช่วงเวลาที่ต้องการถอดรหัส
  metering-mode     : มาตรฐานวัด loudness (1770-3/1770-2/1770-1/LeqA)
  speech-threshold  : เกณฑ์ตรวจ dialogue intelligence (0–100)
  stream-type       : ประเภท EAC3 stream (non_atmos/atmos)

สำหรับฟอร์แมตอื่น (TrueHD, DTS-MA, PCM ฯลฯ) ใช้ ffmpeg โดยตรง:
  ffmpeg -i input.mkv -map 0:a:0 output.wav

ตัวอย่างการใช้งาน:
  1. ถอดรหัส AC3 → WAV ด้วยค่าเริ่มต้น:
     python lib/ddp_decode.py -i movie.ac3 -o movie_decoded.wav

  2. ถอดรหัส EAC3 แบบ Atmos พร้อม downmix เป็น 5.1:
     python lib/ddp_decode.py -i movie.ec3 --format eac3 --stream-type atmos --downmix 5.1

  3. ถอดรหัสเฉพาะบางช่วงเวลา:
     python lib/ddp_decode.py -i movie.ac3 -o out.wav --start 0:1:0.0 --end 0:2:0.0

  4. Dry-run แสดง XML ที่จะส่งให้ DEE:
     python lib/ddp_decode.py -i movie.ac3 --dry-run

  5. ไม่ใช้ DRC และวัด loudness แบบ EBU R128:
     python lib/ddp_decode.py -i movie.ac3 --drc none --metering-mode 1770-3

รหัสสถานะการสิ้นสุด (Exit Codes):
  0   สำเร็จเรียบร้อย
  1   เกิดข้อผิดพลาดทั่วไป
  2   การกำหนดอาร์กิวเมนต์ไม่ถูกต้อง
  3   ฟอร์แมตไฟล์ input ไม่รองรับ
  4   ไม่พบ DEE หรือ DEE ทำงานล้มเหลว
  5   เกิดข้อผิดพลาดในการอ่าน/เขียนไฟล์ (I/O Error)
  130 ผู้ใช้ยกเลิกการทำงาน (Ctrl+C)
"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ddp_decode.py",
        description=HELP_TEXT_THAI,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── I/O ──────────────────────────────────────────────────────────────
    parser.add_argument("-i", "--input",  required=True,
                        help="พาธไฟล์ input (.ac3 หรือ .eac3/.ec3)")
    parser.add_argument("-o", "--output",
                        help="พาธไฟล์ WAV ผลลัพธ์ (ค่าเริ่มต้น: <input>.decoded.wav)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="เขียนทับไฟล์ผลลัพธ์เดิมโดยไม่ถาม")

    # ── Format ───────────────────────────────────────────────────────────
    parser.add_argument(
        "--format",
        choices=["auto", "ac3", "eac3", "ec3"],
        default="auto",
        help="ฟอร์แมตไฟล์ input (auto=ตรวจจากนามสกุล)",
    )
    parser.add_argument(
        "--stream-type",
        dest="stream_type",
        choices=["non_atmos", "atmos"],
        default="non_atmos",
        help="[EAC3 เท่านั้น] ประเภท stream: non_atmos หรือ atmos (ค่าเริ่มต้น: non_atmos)",
    )

    # ── DEE filter passthrough ────────────────────────────────────────────
    parser.add_argument(
        "--threads",
        type=int,
        default=3,
        metavar="N",
        help="จำนวน thread DEE (integer: 0–3, ค่าเริ่มต้น 3)",
    )
    parser.add_argument(
        "--downmix",
        choices=["off", "mono", "stereo", "5.1"],
        default="off",
        help="การ downmix ผลลัพธ์: off | mono | stereo | 5.1 (ค่าเริ่มต้น: off)",
    )
    parser.add_argument(
        "--stereo-mode",
        dest="stereo_mode",
        choices=["auto", "loro", "ltrt"],
        default="auto",
        help="โหมด stereo downmix: auto | loro | ltrt (ค่าเริ่มต้น: auto)",
    )
    parser.add_argument(
        "--drc",
        choices=["none", "line", "rf"],
        default="none",
        help="Dynamic Range Control: none | line | rf (ค่าเริ่มต้น: none)",
    )
    parser.add_argument(
        "--tc-rate",
        dest="tc_rate",
        choices=sorted(VALID_TC_RATE),
        default="not_indicated",
        help="Timecode frame rate (ค่าเริ่มต้น: not_indicated)",
    )
    parser.add_argument(
        "--start",
        default="0:0:0.0",
        metavar="H:M:S.ms",
        help="ตำแหน่งเริ่มต้นของการถอดรหัส (ค่าเริ่มต้น: 0:0:0.0)",
    )
    parser.add_argument(
        "--end",
        default="end_of_file",
        metavar="H:M:S.ms|end_of_file",
        help="ตำแหน่งสิ้นสุด (ค่าเริ่มต้น: end_of_file)",
    )

    # ── Loudness passthrough ──────────────────────────────────────────────
    parser.add_argument(
        "--metering-mode",
        dest="metering_mode",
        choices=["1770-3", "1770-2", "1770-1", "LeqA"],
        default="1770-3",
        help="มาตรฐานวัด loudness (ค่าเริ่มต้น: 1770-3)",
    )
    parser.add_argument(
        "--no-di",
        dest="no_di",
        action="store_true",
        help="ปิด Dialogue Intelligence (ค่าเริ่มต้น: เปิด)",
    )
    parser.add_argument(
        "--speech-threshold",
        dest="speech_threshold",
        type=int,
        default=15,
        metavar="0-100",
        help="เกณฑ์ตรวจ dialogue (integer 0–100, ค่าเริ่มต้น 15)",
    )

    # ── Manifest ─────────────────────────────────────────────────────────
    parser.add_argument(
        "--no-manifest",
        dest="no_manifest",
        action="store_true",
        help="ไม่สร้างไฟล์ Manifest XML คู่กับ WAV",
    )
    parser.add_argument(
        "--manifest-file",
        dest="manifest_file",
        default="",
        metavar="FILENAME",
        help="ชื่อไฟล์ manifest (ค่าเริ่มต้น: <output>.xml)",
    )

    # ── Paths ─────────────────────────────────────────────────────────────
    parser.add_argument("--dee",     default=None, help="ระบุพาธไปยังโปรแกรม dee โดยตรง")
    parser.add_argument("--ffprobe", default=None, help="ระบุพาธไปยัง ffprobe สำหรับตรวจสอบ input")
    parser.add_argument(
        "--temp-dir",
        dest="temp_dir",
        default=None,
        metavar="PATH",
        help="โฟลเดอร์ temp สำหรับ DEE (ค่าเริ่มต้น: %TEMP%)",
    )
    parser.add_argument(
        "--keep-temp",
        dest="keep_temp",
        action="store_true",
        help="ไม่ลบไฟล์ temp หลังจาก DEE ทำงานเสร็จ",
    )

    # ── Misc ─────────────────────────────────────────────────────────────
    parser.add_argument("--dry-run",  dest="dry_run",  action="store_true",
                        help="แสดง XML ที่จะส่งให้ DEE โดยไม่เรียก DEE จริง")
    parser.add_argument("--json",     dest="json_out", action="store_true",
                        help="ส่งออกข้อมูล dry-run ในรูปแบบ JSON")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="แสดงข้อความการทำงานอย่างละเอียด")
    parser.add_argument("-q", "--quiet",   action="store_true",
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

    args = parser.parse_args()

    try:
        job = DdpDecodeJob(args)
        rc = job.run()
        sys.exit(rc)
    except DdpDecodeError as e:
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
