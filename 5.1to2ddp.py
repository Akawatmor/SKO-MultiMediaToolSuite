#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convert multichannel audio to stereo Dolby AC-3/E-AC-3.

This is an orchestration layer around the existing library modules. DEE's
preferred downmix value is metadata only; it does not create a DPL/DPL-II
matrix, so Lt/Rt is generated before the final Dolby encode.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parent
LIB = ROOT / "lib"
DEE_INPUTS = {".ac3", ".eac3", ".ec3"}
MATRIX_ALIASES = {
    "dolby_surround": "ds",
    "ds": "ds",
    "dpl": "pl",
    "pl": "pl",
    "dpl2": "pl2",
    "pl2": "pl2",
    "dpl2s": "pl2s",
    "pl2s": "pl2s",
}


class PipelineError(RuntimeError):
    """User-facing pipeline error."""


def load_library(name: str) -> ModuleType:
    path = LIB / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_sko_{name}", path)
    if spec is None or spec.loader is None:
        raise PipelineError(f"โหลด library ไม่สำเร็จ: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_executable(name: str, explicit: str | None = None) -> str:
    candidates: list[Path] = []
    if explicit:
        direct = Path(os.path.expandvars(os.path.expanduser(explicit)))
        candidates.append(direct)
        if direct.suffix.lower() != ".exe":
            candidates.append(direct.with_suffix(".exe"))
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    if os.name == "nt":
        found = shutil.which(f"{name}.exe")
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise PipelineError(f"ไม่พบ {name}: {explicit or 'PATH'}")


def run_ffmpeg_stereo(
    ffmpeg: str,
    source: Path,
    destination: Path,
    *,
    stream: int,
) -> None:
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(source), "-map", f"0:a:{stream}", "-vn", "-sn", "-dn",
        "-ac", "2", "-ar", "48000", "-channel_layout", "stereo",
        "-c:a", "pcm_f32le", "-rf64", "auto", str(destination),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or "").strip()
        raise PipelineError(f"FFmpeg Lo/Ro downmix ล้มเหลว: {detail}")
    if not destination.is_file() or destination.stat().st_size <= 44:
        raise PipelineError(f"FFmpeg ไม่ได้สร้าง WAV ที่ใช้งานได้: {destination}")


def run_ffmpeg_wav32(
    ffmpeg: str,
    source: Path,
    destination: Path,
    *,
    stream: int,
) -> None:
    """Normalize an otherwise unsupported audio container to 32-bit float WAV."""
    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(source), "-map", f"0:a:{stream}", "-vn", "-sn", "-dn",
        "-ar", "48000", "-c:a", "pcm_f32le", "-rf64", "auto",
        str(destination),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or "").strip()
        raise PipelineError(f"FFmpeg แปลง input เป็น WAV 32-bit ล้มเหลว: {detail}")
    if not destination.is_file() or destination.stat().st_size <= 44:
        raise PipelineError(f"FFmpeg ไม่ได้สร้าง WAV 32-bit ที่ใช้งานได้: {destination}")


def normalize_output(path: Path, output_format: str) -> Path:
    suffix = ".ac3" if output_format == "ac3" else ".eac3"
    if path.suffix.lower() not in {".ac3", ".eac3", ".ec3"}:
        return path.with_suffix(suffix)
    return path


def build_parser() -> argparse.ArgumentParser:
    help_text = """\
แปลงเสียงหลายแชนเนลเป็น AC-3 / E-AC-3 แบบสเตอริโอ 2.0

ลำดับการทำงาน:
  1) input AC-3/E-AC-3 จะถูก DEE ถอดเป็น WAV ก่อน
  2) input นามสกุลอื่นที่ไม่ใช่ WAV จะถูก FFmpeg แปลงเป็น WAV 32-bit float 48 kHz
  3) เลือกวิธีทำ stereo:
       loro = Lo/Ro ปกติด้วย FFmpeg
       ltrt = Lt/Rt matrix ด้วย 6to2matrixencoder
  4) ส่ง WAV stereo เข้า DEE เพื่อ encode เป็น AC-3 หรือ E-AC-3

ความหมายของ --matrix-mode (ใช้เมื่อ --mixdown ltrt):
  dpl2 / pl2          Dolby Pro Logic II (ค่าเริ่มต้น)
  dpl / pl             Dolby Pro Logic
  dolby_surround / ds Dolby Surround รุ่นดั้งเดิม
  dpl2s / pl2s        PLII Simple / matrix แบบกลับเฟส

ข้อควรรู้:
  - DPL/DPL2 เป็นการสร้างสัญญาณ Lt/Rt จริงในขั้น matrix ก่อน encode
  - preferred_downmix และ dolby_surround_mode ของ DEE เป็น metadata เท่านั้น
    ไม่ได้สร้าง DPL2 ให้เอง
  - --mixdown loro ไม่ใช่ Dolby Surround และไม่ควรใช้ metadata ltrt
  - --bitrate เป็น kbps ต้องเป็นค่าที่ DEE รองรับ
  - --stream เริ่มนับจาก 0

ตัวอย่าง:
  # 5.1 WAV -> E-AC-3 2.0 Lt/Rt แบบ DPL2 ที่ 192 kbps
  py 5.1to2ddp.py -i movie.wav -o movie.eac3 --mixdown ltrt --matrix-mode dpl2 --bitrate 192 -y

  # AC-3/E-AC-3 -> WAV -> AC-3 2.0 Dolby Surround ที่ 192 kbps
  py 5.1to2ddp.py -i source.eac3 -o output.ac3 --mixdown ltrt --matrix-mode ds --output-format ac3 --bitrate 192 -y

  # FLAC/MP3/M4A หรือไฟล์เสียงอื่น -> normalize เป็น WAV 32-bit -> E-AC-3 2.0 Lo/Ro
  py 5.1to2ddp.py -i source.flac -o output.eac3 --mixdown loro --bitrate 192 -y

ถ้า ffmpeg/dee ไม่อยู่ใน PATH ให้ระบุ --ffmpeg และ --dee
"""
    parser = argparse.ArgumentParser(
        prog="5.1to2ddp.py",
        description=help_text,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-i", "--input", required=True, help="ไฟล์เสียงต้นทาง")
    parser.add_argument("-o", "--output", help="ไฟล์ AC-3/E-AC-3 ปลายทาง")
    parser.add_argument(
        "--mixdown", choices=["loro", "ltrt"], default="ltrt",
        help="เลือกวิธีทำ stereo: loro=ปกติ, ltrt=Lt/Rt matrix (ค่าเริ่มต้น: ltrt)",
    )
    parser.add_argument(
        "--matrix-mode", choices=sorted(MATRIX_ALIASES), default="dpl2",
        help="เลือกสูตร Lt/Rt: dpl2, dpl, dolby_surround หรือ dpl2s (ค่าเริ่มต้น: dpl2)",
    )
    parser.add_argument(
        "--output-format", choices=["ac3", "eac3"], default="eac3",
        help="ฟอร์แมตปลายทาง",
    )
    parser.add_argument(
        "--bitrate", "--data-rate", dest="bitrate", type=int, default=192,
        help="bitrate kbps ของ output เช่น 128, 192, 256, 384, 448",
    )
    parser.add_argument("--stream", type=int, default=0, help="ดัชนี audio stream")
    parser.add_argument(
        "--stream-type", choices=["non_atmos", "atmos"], default="non_atmos",
    )
    parser.add_argument("--dee", help="พาธ dee.exe")
    parser.add_argument("--ffmpeg", help="พาธ ffmpeg.exe")
    parser.add_argument(
        "--loudness-mode",
        choices=["measure_only", "measure_and_correct"],
        default="measure_only",
        help="โหมด loudness ของ DEE",
    )
    parser.add_argument(
        "--loudness-preset",
        choices=["atsc_a85", "atsc_a85_agile", "ebu_r128", "freetv_op59", "arib_b32", "manual"],
        default="atsc_a85",
    )
    parser.add_argument("--preferred-downmix", choices=["loro", "ltrt"])
    parser.add_argument(
        "--dolby-surround-mode", choices=["yes", "no", "not_indicated"],
    )
    parser.add_argument(
        "--user-data", type=int, default=-1,
        help="DEE user_data (-1 = auto); ไม่ใช่ dialnorm override",
    )
    parser.add_argument("--temp-dir", help="โฟลเดอร์ temporary")
    parser.add_argument("-y", "--yes", action="store_true", help="เขียนทับไฟล์ปลายทาง")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def run_pipeline(args: argparse.Namespace) -> int:
    source = Path(args.input).expanduser().resolve()
    if not source.is_file():
        raise PipelineError(f"ไม่พบไฟล์ input: {source}")
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else source.with_suffix(".ac3" if args.output_format == "ac3" else ".eac3")
    )
    output = normalize_output(output, args.output_format)
    if output == source:
        raise PipelineError("ไฟล์ output ต้องไม่ซ้ำกับ input")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not args.yes:
        raise PipelineError(f"ไฟล์ output มีอยู่แล้ว ใช้ -y เพื่อเขียนทับ: {output}")

    ffmpeg = find_executable("ffmpeg", args.ffmpeg)
    dee = find_executable("dee", args.dee)
    decoder = load_library("ddp_decode")
    encoder = load_library("ddp_encode")
    matrix = load_library("6to2matrixencoder")

    temp_root = Path(args.temp_dir).expanduser().resolve() if args.temp_dir else None
    with tempfile.TemporaryDirectory(
        prefix="sko_51to2_",
        dir=str(temp_root) if temp_root else None,
    ) as tmp:
        work = Path(tmp)
        decoded = work / "decoded.wav"
        normalized = work / "normalized_32f.wav"
        matrix_wav = work / "matrix.wav"
        source_for_downmix = source
        source_stream = args.stream

        if source.suffix.lower() in DEE_INPUTS:
            print("[1/3] DEE decode AC-3/E-AC-3 -> WAV")
            rc = decoder.decode(
                str(source), str(decoded),
                input_format="auto",
                stream_type=args.stream_type,
                output_manifest=False,
                temp_dir=str(work),
                dee_bin=dee,
                quiet=not args.verbose,
                overwrite=True,
            )
            if rc != 0:
                raise PipelineError(f"DEE decode ล้มเหลว (exit code {rc})")
            source_for_downmix = decoded
            source_stream = 0
        elif source.suffix.lower() != ".wav":
            print("[1/3] FFmpeg normalize input -> WAV 32-bit float 48 kHz")
            run_ffmpeg_wav32(
                ffmpeg, source, normalized, stream=args.stream,
            )
            source_for_downmix = normalized
            source_stream = 0

        preferred = args.preferred_downmix or args.mixdown
        surround_mode = args.dolby_surround_mode
        if surround_mode is None:
            surround_mode = "yes" if args.mixdown == "ltrt" else "no"

        if args.mixdown == "loro":
            print("[2/3] FFmpeg Lo/Ro stereo downmix")
            run_ffmpeg_stereo(
                ffmpeg, source_for_downmix, matrix_wav,
                stream=source_stream,
            )
        else:
            mode = MATRIX_ALIASES[args.matrix_mode]
            print(f"[2/3] Matrix Lt/Rt ({mode})")
            matrix_args = [
                "-i", str(source_for_downmix), "-o", str(matrix_wav),
                "--mode", mode, "--layout", "auto",
                "--stream", str(source_stream),
                "--headroom", "peak", "--ffmpeg", ffmpeg, "-y",
            ]
            if args.verbose:
                matrix_args.append("--verbose")
            matrix_args_ns = matrix.build_arg_parser().parse_args(matrix_args)
            rc = matrix.EncoderPipeline(matrix_args_ns).run()
            if rc != 0:
                raise PipelineError(f"Matrix encoder ล้มเหลว (exit code {rc})")

        print(f"[3/3] DEE encode stereo -> {output.name}")
        encoder_mode = "dd" if args.output_format == "ac3" else "ddp"
        rc = encoder.encode(
            str(matrix_wav), str(output),
            encoder_mode=encoder_mode,
            output_format=args.output_format,
            data_rate=args.bitrate,
            downmix_config="off",
            lfe_on=False,
            dolby_surround_mode=surround_mode,
            dolby_surround_ex_mode="no",
            loudness_mode=args.loudness_mode,
            loudness_preset=args.loudness_preset,
            preferred_downmix=preferred,
            user_data=args.user_data,
            temp_dir=str(work),
            dee_bin=dee,
            quiet=not args.verbose,
            overwrite=True,
        )
        if rc != 0:
            raise PipelineError(f"DEE encode ล้มเหลว (exit code {rc})")

    print(f"สำเร็จ: {output}")
    print(f"Metadata: preferred_downmix={preferred}, dolby_surround_mode={surround_mode}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_pipeline(args)
    except KeyboardInterrupt:
        print("ยกเลิกโดยผู้ใช้", file=sys.stderr)
        return 130
    except (PipelineError, OSError, ValueError) as exc:
        print(f"ข้อผิดพลาด: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())