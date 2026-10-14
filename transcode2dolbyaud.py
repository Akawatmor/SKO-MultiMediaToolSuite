#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Universal audio transcoder with optional custom DPL-II Lt/Rt downmix.

Input decoding/downmix preparation: FFmpeg + ffprobe
AC-3 and E-AC-3 output: DEE pcm_to_ddp version 3, encoding directly from the stereo PCM WAV
AAC output: qaac (Apple CoreAudio AAC encoder), TVBR/CVBR/ABR

The custom DPL-II matrix follows the supplied wav2DPL2 implementation.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

try:
    import numpy as np
    from scipy.io import wavfile
    from scipy.signal import butter, hilbert, sosfiltfilt
except ImportError as exc:  # pragma: no cover - environment dependent
    print(
        "[ERROR] DPL-II DSP dependencies are missing. Install them with:\n"
        "        python -m pip install numpy scipy\n"
        f"Details: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(2)


VERSION = "1.3.0"
DEE_XML_FLAG_DEFAULT = "--xml"
DEFAULT_DEE_WINDOWS = r"C:\Encoder dll\dolby\dee\dee.exe"

# Linear coefficients copied from the user's supplied wav2DPL2 implementation.
G_CENTER = np.float32(10 ** (-3.0 / 20.0))
G_MAJOR = np.float32(10 ** (-1.2 / 20.0))
G_MINOR = np.float32(10 ** (-6.2 / 20.0))
DEFAULT_LPF_HZ = 18500.0

LOUDNESS_PRESETS = [
    "atsc_a85", "atsc_a85_agile", "ebu_r128", "freetv_op59", "arib_b32"
]
# Exact DRC profile values from the supplied pcm_to_ddp v3 schema.
DRC_PROFILES = ["film_standard", "film_light", "music_standard", "music_light", "speech"]
# Values documented in the supplied pcm_to_ddp v3 schema.
DEE_DATA_RATES = [
    32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 128, 144, 160,
    176, 192, 200, 208, 216, 224, 232, 240, 248, 256, 272, 288, 304,
    320, 336, 352, 368, 384, 400, 448, 512, 576, 640, 704, 768, 832,
    896, 960, 1008, 1024,
]
OUTPUT_BY_SUFFIX = {
    ".ac3": "ac3",
    ".ec3": "eac3",
    ".eac3": "eac3",
    ".m4a": "aac",
    ".aac": "aac",
}


class PipelineError(RuntimeError):
    """Expected user-facing pipeline failure."""


def resolve_exe(candidate: str | None) -> str | None:
    """Resolve an executable from a direct path or PATH."""
    if not candidate:
        return None
    candidate = os.path.expandvars(os.path.expanduser(candidate.strip()))
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)
    found = shutil.which(candidate)
    if found:
        return found
    if os.name == "nt" and not candidate.lower().endswith(".exe"):
        found = shutil.which(candidate + ".exe")
        if found:
            return found
    return None


def command_string(cmd: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return shlex.join(cmd)


def run_command(cmd: list[str], *, label: str, cwd: Path | None = None) -> None:
    print(f"\n[{label}] > {command_string(cmd)}", flush=True)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise PipelineError(f"Cannot start {cmd[0]!r}: {exc}") from exc

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            print(f"[{label}] {line}", flush=True)
    proc.stdout.close()
    rc = proc.wait()
    if rc != 0:
        raise PipelineError(f"{label} failed with exit code {rc}.")


def require_exe(candidate: str | None, name: str) -> str:
    resolved = resolve_exe(candidate)
    if not resolved:
        raise PipelineError(
            f"Cannot find {name}: {candidate!r}. Install it or pass its path with the relevant option."
        )
    return resolved


def _windows_short_path(path: Path) -> Path | None:
    """Return a Windows 8.3 path if one is available, otherwise None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        get_short_path = ctypes.windll.kernel32.GetShortPathNameW
        get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        get_short_path.restype = ctypes.c_uint
        count = get_short_path(str(path), buf, len(buf))
        if count and count < len(buf):
            result = Path(buf.value)
            if not any(ch.isspace() for ch in str(result)):
                return result
    except Exception:
        pass
    return None


def choose_work_root(output_parent: Path, requested_root: Path | None = None) -> Path:
    """Choose a temp root whose DEE-visible path contains no whitespace on Windows."""
    if requested_root is not None:
        candidates = [requested_root.expanduser()]
    else:
        candidates = [Path(tempfile.gettempdir())]
        # Useful fallback when the system temp directory has spaces and no 8.3 alias.
        if not any(ch.isspace() for ch in str(output_parent)):
            candidates.append(output_parent)
        drive = os.environ.get("SystemDrive", "C:")
        candidates.append(Path(drive + r"\t2da_temp"))

    errors: list[str] = []
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            absolute = Path(os.path.abspath(str(candidate)))
            if os.name != "nt" or not any(ch.isspace() for ch in str(absolute)):
                return absolute
            short = _windows_short_path(absolute)
            if short is not None:
                return short
            errors.append(f"{absolute}: path contains spaces and no short path is available")
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")

    if requested_root is not None:
        raise PipelineError(
            "The requested --temp-root cannot be used by DEE because its path contains spaces "
            "and Windows could not provide an 8.3 short path. Choose a temporary directory without spaces."
        )
    details = "; ".join(errors)
    raise PipelineError(
        "Could not find a temporary directory with a DEE-safe path (no spaces). " + details
    )


def probe_audio(ffprobe: str, input_file: Path, audio_stream: int) -> dict[str, Any]:
    cmd = [
        ffprobe, "-v", "error", "-select_streams", f"a:{audio_stream}",
        "-show_entries", "stream=channels,channel_layout,sample_rate,codec_name",
        "-of", "json", str(input_file),
    ]
    try:
        result = subprocess.run(
            cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        raise PipelineError(f"ffprobe could not inspect input audio. {detail}") from exc
    except OSError as exc:
        raise PipelineError(f"Cannot run ffprobe: {exc}") from exc
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError as exc:
        raise PipelineError("ffprobe returned invalid JSON.") from exc
    if not streams:
        raise PipelineError(f"No audio stream a:{audio_stream} found in {input_file}.")
    return streams[0]


def decode_to_float_wav(
    ffmpeg: str,
    input_file: Path,
    output_wav: Path,
    *,
    audio_stream: int,
    channels: int,
    sample_rate: int,
) -> None:
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(input_file),
        "-map", f"0:a:{audio_stream}", "-vn", "-sn", "-dn",
        "-ac", str(channels), "-ar", str(sample_rate),
        "-c:a", "pcm_f32le", "-rf64", "auto", str(output_wav),
    ]
    run_command(cmd, label="FFmpeg decode")
    if not output_wav.is_file() or output_wav.stat().st_size <= 44:
        raise PipelineError(f"FFmpeg did not create a usable PCM WAV: {output_wav}")


def apply_phase_shift_90(signal: np.ndarray) -> np.ndarray:
    """Shift one surround channel by 90 degrees using a Hilbert transform."""
    analytic_signal = hilbert(signal)
    return np.imag(analytic_signal).astype(np.float32, copy=False)


def apply_lowpass(stereo: np.ndarray, sample_rate: int, cutoff: float) -> np.ndarray:
    if cutoff <= 0 or cutoff >= sample_rate / 2:
        raise PipelineError(
            f"Low-pass cutoff must be above 0 and below Nyquist ({sample_rate / 2:.1f} Hz); got {cutoff}."
        )
    sos = butter(N=8, Wn=cutoff, btype="low", fs=sample_rate, output="sos")
    try:
        filtered = sosfiltfilt(sos, stereo, axis=0)
    except ValueError as exc:
        raise PipelineError(
            "Audio is too short for the zero-phase low-pass filter. Use --no-lpf for very short clips."
        ) from exc
    return filtered.astype(np.float32, copy=False)


def _close_memmap(array: Any) -> None:
    """Flush and close a NumPy memmap (important on Windows before cleanup)."""
    if isinstance(array, np.memmap):
        try:
            array.flush()
        except Exception:
            pass
        mmap_obj = getattr(array, "_mmap", None)
        if mmap_obj is not None:
            try:
                mmap_obj.close()
            except Exception:
                pass


def _filter_lowpass_chunked(
    source: np.memmap,
    target: np.memmap,
    *,
    frame_count: int,
    sample_rate: int,
    cutoff: float,
) -> None:
    """Apply zero-phase LPF in overlapped blocks to bound working RAM.

    The overlap is deliberately much longer than this high-cutoff filter's settling
    time; overlapping samples are discarded so block boundaries are not audible.
    """
    if cutoff <= 0 or cutoff >= sample_rate / 2:
        raise PipelineError(
            f"Low-pass cutoff must be above 0 and below Nyquist ({sample_rate / 2:.1f} Hz); got {cutoff}."
        )
    sos = butter(N=8, Wn=cutoff, btype="low", fs=sample_rate, output="sos")
    core_size = max(sample_rate * 30, 262144)  # about 30 seconds per block
    overlap = min(max(sample_rate // 6, 8192), max(frame_count // 4, 8192))

    for core_start in range(0, frame_count, core_size):
        core_end = min(frame_count, core_start + core_size)
        ext_start = max(0, core_start - overlap)
        ext_end = min(frame_count, core_end + overlap)
        block = np.asarray(source[ext_start:ext_end, :], dtype=np.float32)
        try:
            filtered = sosfiltfilt(sos, block, axis=0)
        except ValueError as exc:
            raise PipelineError(
                "Audio is too short for the zero-phase low-pass filter. Use --no-lpf for very short clips."
            ) from exc
        left = core_start - ext_start
        right = left + (core_end - core_start)
        target[core_start:core_end, :] = filtered[left:right, :]
        del block, filtered
    target.flush()


def dpl2_ltrt_downmix(
    wav_51_in: Path,
    wav_20_out: Path,
    *,
    sample_rate_expected: int,
    lpf_hz: float | None,
    prevent_clipping: bool,
    ffmpeg: str,
) -> None:
    """Run the supplied 5.1 -> Lt/Rt matrix, using disk-backed scratch arrays.

    Hilbert transforms remain full-length per surround channel, matching the supplied
    formula. PCM buffers and the matrix/filter pass are disk-backed/block-processed
    to avoid keeping the entire 5.1 and stereo programme in RAM simultaneously.
    """
    try:
        sr, data = wavfile.read(str(wav_51_in), mmap=True)
    except Exception as exc:
        raise PipelineError(f"Could not read 5.1 PCM WAV (including RF64): {exc}") from exc
    if sr != sample_rate_expected:
        _close_memmap(data)
        raise PipelineError(f"Expected {sample_rate_expected} Hz 5.1 WAV, got {sr} Hz.")
    if data.ndim != 2 or data.shape[1] != 6:
        shape = getattr(data, "shape", None)
        _close_memmap(data)
        raise PipelineError(f"Expected 6-channel 5.1 WAV; got shape {shape}.")
    if data.dtype != np.float32:
        _close_memmap(data)
        raise PipelineError(f"Expected float32 PCM from FFmpeg; got {data.dtype}.")

    frame_count = int(data.shape[0])
    scratch_dir = wav_20_out.parent
    ls90_path = scratch_dir / "dpl2_surround_l_90.raw"
    rs90_path = scratch_dir / "dpl2_surround_r_90.raw"
    pre_filter_path = scratch_dir / "dpl2_stereo_pre_lpf.raw"
    filtered_path = scratch_dir / "dpl2_stereo_filtered.raw"
    scratch_maps: list[np.memmap] = []

    try:
        print(
            "[DPL-II] Calculating full-length 90-degree Hilbert phase shifts and the supplied matrix...",
            flush=True,
        )
        ls90 = np.memmap(ls90_path, dtype=np.float32, mode="w+", shape=(frame_count,))
        scratch_maps.append(ls90)
        shifted_l = apply_phase_shift_90(data[:, 4])
        ls90[:] = shifted_l
        del shifted_l
        ls90.flush()

        rs90 = np.memmap(rs90_path, dtype=np.float32, mode="w+", shape=(frame_count,))
        scratch_maps.append(rs90)
        shifted_r = apply_phase_shift_90(data[:, 5])
        rs90[:] = shifted_r
        del shifted_r
        rs90.flush()

        pre_filter = np.memmap(
            pre_filter_path, dtype=np.float32, mode="w+", shape=(frame_count, 2)
        )
        scratch_maps.append(pre_filter)
        mix_chunk = max(sr * 30, 262144)
        for start in range(0, frame_count, mix_chunk):
            end = min(frame_count, start + mix_chunk)
            # Matrix is the one supplied by the user; LFE channel 3 is intentionally omitted.
            fl = data[start:end, 0]
            fr = data[start:end, 1]
            center = data[start:end, 2]
            surround_l_90 = ls90[start:end]
            surround_r_90 = rs90[start:end]
            pre_filter[start:end, 0] = (
                fl + (G_CENTER * center) - (G_MAJOR * surround_l_90) - (G_MINOR * surround_r_90)
            )
            pre_filter[start:end, 1] = (
                fr + (G_CENTER * center) + (G_MINOR * surround_l_90) + (G_MAJOR * surround_r_90)
            )
        pre_filter.flush()
        del ls90, rs90

        if lpf_hz is not None:
            print(f"[DPL-II] Applying zero-phase 8th-order Butterworth LPF at {lpf_hz:g} Hz...")
            result = np.memmap(
                filtered_path, dtype=np.float32, mode="w+", shape=(frame_count, 2)
            )
            scratch_maps.append(result)
            _filter_lowpass_chunked(
                pre_filter, result,
                frame_count=frame_count, sample_rate=sr, cutoff=lpf_hz,
            )
            del pre_filter
            result_path = filtered_path
        else:
            result = pre_filter
            result_path = pre_filter_path

        if prevent_clipping:
            max_peak = 0.0
            for start in range(0, frame_count, mix_chunk):
                end = min(frame_count, start + mix_chunk)
                if end > start:
                    max_peak = max(max_peak, float(np.max(np.abs(result[start:end, :]))))
            if max_peak > 1.0:
                peak_db = 20.0 * math.log10(max_peak)
                print(
                    f"[DPL-II] Peak exceeded 0 dBFS by {peak_db:.2f} dB; "
                    "applying static whole-file peak scaling."
                )
                for start in range(0, frame_count, mix_chunk):
                    end = min(frame_count, start + mix_chunk)
                    result[start:end, :] = result[start:end, :] / max_peak
                result.flush()

        output_bytes = frame_count * 2 * np.dtype(np.float32).itemsize
        # scipy writes classic RIFF/WAV. For very long stereo outputs, let FFmpeg
        # write RF64 when needed instead of producing a WAV with a wrapped size field.
        _close_memmap(data)
        for mm in scratch_maps:
            if mm is not result:
                _close_memmap(mm)
        result.flush()
        if output_bytes + 44 < 0xFFFFFFFF:
            wavfile.write(str(wav_20_out), sr, result)
        else:
            _close_memmap(result)
            raw_cmd = [
                ffmpeg, "-hide_banner", "-nostdin", "-y",
                "-f", "f32le", "-ar", str(sr), "-ac", "2", "-i", str(result_path),
                "-c:a", "pcm_f32le", "-rf64", "auto", str(wav_20_out),
            ]
            run_command(raw_cmd, label="FFmpeg RF64 wrap")
        _close_memmap(result)
        print(f"[DPL-II] Wrote stereo float WAV: {wav_20_out}")
    finally:
        _close_memmap(data)
        # Close every map before deleting scratch files (Windows keeps mapped files locked).
        for mm in scratch_maps:
            _close_memmap(mm)
        for scratch in (ls90_path, rs90_path, pre_filter_path, filtered_path):
            try:
                scratch.unlink(missing_ok=True)
            except OSError:
                pass


def make_ffmpeg_stereo(
    ffmpeg: str,
    input_file: Path,
    stereo_wav: Path,
    *,
    audio_stream: int,
    sample_rate: int,
) -> None:
    decode_to_float_wav(
        ffmpeg, input_file, stereo_wav, audio_stream=audio_stream,
        channels=2, sample_rate=sample_rate,
    )
    print("[DOWNMIX] Using FFmpeg's standard channel downmix (no custom DPL-II matrix).")


def write_xml(root: ET.Element, xml_path: Path) -> None:
    tree = ET.ElementTree(root)
    try:
        ET.indent(tree, space="  ")
    except AttributeError:  # Python < 3.9 compatibility
        pass
    tree.write(str(xml_path), encoding="utf-8", xml_declaration=True)


def add_local_path(parent: ET.Element, path: Path) -> None:
    storage = ET.SubElement(ET.SubElement(parent, "storage"), "local")
    # Do not call Path.resolve() here: it can expand a Windows 8.3 path back to
    # a long path containing spaces, which breaks some DEE builds.
    ET.SubElement(storage, "path").text = os.path.abspath(str(path))


def build_pcm_to_ddp_job(
    input_wav: Path,
    output_file: Path,
    xml_path: Path,
    work_dir: Path,
    args: argparse.Namespace,
    surround_mode: str,
    *,
    output_kind: str,
) -> None:
    """Build the user's supplied DEE pcm_to_ddp v3 schema for DD or DDP."""
    if output_kind not in ("ac3", "eac3"):
        raise PipelineError(f"Unsupported DEE output kind: {output_kind}")

    job = ET.Element("job_config")

    # <input><audio><wav version="1"> -- DEE receives the already-mixed stereo PCM.
    audio_in = ET.SubElement(ET.SubElement(job, "input"), "audio")
    wav = ET.SubElement(audio_in, "wav", version="1")
    ET.SubElement(wav, "file_name").text = input_wav.name
    ET.SubElement(wav, "timecode_frame_rate").text = "not_indicated"
    ET.SubElement(wav, "offset").text = "auto"
    ET.SubElement(wav, "ffoa").text = "auto"
    add_local_path(wav, input_wav.parent)

    audio_filter = ET.SubElement(ET.SubElement(job, "filter"), "audio")
    enc = ET.SubElement(audio_filter, "pcm_to_ddp", version="3")

    loudness = ET.SubElement(enc, "loudness")
    if output_kind == "eac3":
        # The supplied DDP XML uses measure_and_correct with a correction preset.
        measure = ET.SubElement(loudness, "measure_and_correct")
        ET.SubElement(measure, "preset").text = args.loudness_preset
        encoder_mode = "ddp"
        output_tag = "ec3"
    else:
        # The supplied DD XML explicitly requires measure_only in DD mode.
        measure = ET.SubElement(loudness, "measure_only")
        ET.SubElement(measure, "metering_mode").text = "1770-3"
        ET.SubElement(measure, "dialogue_intelligence").text = "true"
        ET.SubElement(measure, "speech_threshold").text = "15"
        encoder_mode = "dd"
        output_tag = "ac3"

    ET.SubElement(enc, "encoder_mode").text = encoder_mode
    ET.SubElement(enc, "bitstream_mode").text = "complete_main"
    # The PCM WAV is already stereo; do not ask DEE to downmix it a second time.
    ET.SubElement(enc, "downmix_config").text = "off"
    ET.SubElement(enc, "data_rate").text = str(args.bitrate)
    ET.SubElement(enc, "timecode_frame_rate").text = "not_indicated"
    ET.SubElement(enc, "start").text = "first_frame_of_action"
    ET.SubElement(enc, "end").text = "end_of_file"
    ET.SubElement(enc, "time_base").text = "file_position"
    ET.SubElement(enc, "prepend_silence_duration").text = "0.0"
    ET.SubElement(enc, "append_silence_duration").text = "0.0"
    # The input is 2.0 stereo with no discrete LFE channel.
    ET.SubElement(enc, "lfe_on").text = "false"
    ET.SubElement(enc, "dolby_surround_mode").text = surround_mode
    ET.SubElement(enc, "dolby_surround_ex_mode").text = "no"
    ET.SubElement(enc, "user_data").text = "-1"

    drc = ET.SubElement(enc, "drc")
    ET.SubElement(drc, "line_mode_drc_profile").text = args.drc_line
    ET.SubElement(drc, "rf_mode_drc_profile").text = args.drc_rf
    ET.SubElement(enc, "lfe_lowpass_filter").text = "true"
    # DPL-II phase encoding was already performed in the DSP stage. Avoid a second shift.
    ET.SubElement(enc, "surround_90_degree_phase_shift").text = "false"
    ET.SubElement(enc, "surround_3db_attenuation").text = "false"

    dm = ET.SubElement(enc, "downmix")
    ET.SubElement(dm, "loro_center_mix_level").text = "-3"
    ET.SubElement(dm, "loro_surround_mix_level").text = "-3"
    ET.SubElement(dm, "ltrt_center_mix_level").text = "-3"
    ET.SubElement(dm, "ltrt_surround_mix_level").text = "-3"
    ET.SubElement(dm, "preferred_downmix_mode").text = "ltrt"
    ET.SubElement(enc, "allow_hybrid_downmix").text = "false"

    embedded = ET.SubElement(enc, "embedded_timecodes")
    ET.SubElement(embedded, "starting_timecode").text = "off"
    ET.SubElement(embedded, "frame_rate").text = "auto"

    output = ET.SubElement(ET.SubElement(job, "output"), output_tag, version="1")
    ET.SubElement(output, "file_name").text = output_file.name
    add_local_path(output, output_file.parent)

    temp = ET.SubElement(ET.SubElement(job, "misc"), "temp_dir")
    ET.SubElement(temp, "clean_temp").text = "true"
    ET.SubElement(temp, "path").text = os.path.abspath(str(work_dir))
    write_xml(job, xml_path)


def choose_surround_mode(requested: str, dpl2_applied: bool) -> str:
    if requested == "auto":
        return "yes" if dpl2_applied else "no"
    return requested


def encode_dolby(
    dee: str,
    stereo_wav: Path,
    staging_output: Path,
    work_dir: Path,
    args: argparse.Namespace,
    surround_mode: str,
    *,
    output_kind: str,
) -> None:
    """Encode the preprocessed stereo PCM directly with DEE pcm_to_ddp v3."""
    xml_path = work_dir / f"dee_{output_kind}_job.xml"
    build_pcm_to_ddp_job(
        stereo_wav, staging_output, xml_path, work_dir, args, surround_mode,
        output_kind=output_kind,
    )
    mode = "ddp" if output_kind == "eac3" else "dd"
    loudness_summary = (
        f"measure_and_correct/{args.loudness_preset}"
        if output_kind == "eac3" else "measure_only/1770-3"
    )
    print(
        f"[DEE CONFIG] pcm_to_ddp v3; encoder_mode={mode}; bitrate={args.bitrate}; "
        f"loudness={loudness_summary}; line DRC={args.drc_line}; RF DRC={args.drc_rf}; "
        f"downmix_config=off",
        flush=True,
    )
    run_command([dee, args.dee_xml_flag, str(xml_path)], label=f"DEE {output_kind.upper()}")
    if not staging_output.is_file() or staging_output.stat().st_size == 0:
        raise PipelineError(f"DEE returned success but output is missing: {staging_output}")


def encode_aac(
    ffmpeg: str,
    qaac: str,
    stereo_wav: Path,
    staging_output: Path,
    work_dir: Path,
    args: argparse.Namespace,
    output_suffix: str,
) -> None:
    # Give qaac a conventional integer-PCM WAV rather than depending on float-WAV support.
    qaac_wav = work_dir / "qaac_input_pcm24.wav"
    cmd_pcm = [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(stereo_wav),
        "-vn", "-sn", "-dn", "-ac", "2", "-ar", str(args.sample_rate),
        "-c:a", "pcm_s24le", str(qaac_wav),
    ]
    run_command(cmd_pcm, label="FFmpeg QAAC PCM prep")
    if not qaac_wav.is_file() or qaac_wav.stat().st_size == 0:
        raise PipelineError("FFmpeg did not create the 24-bit PCM WAV for qaac.")

    if args.qaac_mode == "tvbr":
        quality_arg = ["--tvbr", str(args.qaac_quality)]
    elif args.qaac_mode == "cvbr":
        quality_arg = ["--cvbr", str(args.qaac_bitrate)]
    else:
        quality_arg = ["--abr", str(args.qaac_bitrate)]

    qaac_cmd = [qaac, *quality_arg, "-o", str(staging_output)]
    if output_suffix.lower() == ".aac":
        qaac_cmd.insert(1, "--adts")
    qaac_cmd.append(str(qaac_wav))
    run_command(qaac_cmd, label=f"QAAC AAC ({args.qaac_mode.upper()})")
    if not staging_output.is_file() or staging_output.stat().st_size == 0:
        raise PipelineError(f"qaac returned success but AAC output is missing: {staging_output}")


class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    """Keep command examples as individual lines while showing option defaults."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcode2dolbyaud",
        description=(
            "Universal audio transcode pipeline. Decode audio with FFmpeg; optionally calculate a custom "
            "5.1 -> 2.0 Dolby Pro Logic II Lt/Rt matrix; encode to AC-3, E-AC-3, or AAC."
        ),
        formatter_class=HelpFormatter,
        epilog=(
            "Examples:\n"
            "  python transcode2dolbyaud.py input.mkv output.ac3\n"
            "  python transcode2dolbyaud.py input.wav output.eac3 --bitrate 192\n"
            "  python transcode2dolbyaud.py input.wav output.eac3 --drc-line music_light --drc-rf music_light\n"
            "  python transcode2dolbyaud.py input.flac output.m4a --qaac-mode tvbr --qaac-quality 91\n"
            "  python transcode2dolbyaud.py input.wav output.m4a --qaac-mode cvbr --qaac-bitrate 192\n"
            "  python transcode2dolbyaud.py input.opus output.aac --qaac-mode abr --qaac-bitrate 256\n"
            "  python transcode2dolbyaud.py input.mkv output.ac3 --downmix ffmpeg\n\n"
            "DPL-II DSP dependencies: python -m pip install numpy scipy\n"
            "Temporary files use a DEE-safe path without spaces and are deleted at the end.\n"
            "Use --keep-temp only for debugging; --temp-root can select a safe temporary root.\n"
            "External tools: ffmpeg + ffprobe; DEE for .ac3/.ec3/.eac3; qaac for .m4a/.aac."
        ),
    )
    parser.add_argument("input", type=Path, help="Input audio/video file (first audio stream by default)")
    parser.add_argument(
        "output", type=Path,
        help="Output file: .ac3, .ec3/.eac3, .m4a, or .aac (format inferred from extension)",
    )
    parser.add_argument("--audio-stream", type=int, default=0, help="Zero-based audio stream index (a:0, a:1, ...)")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="Path/name of ffmpeg executable")
    parser.add_argument("--ffprobe", default="ffprobe", help="Path/name of ffprobe executable")
    parser.add_argument(
        "--dee", default=(DEFAULT_DEE_WINDOWS if os.name == "nt" and os.path.isfile(DEFAULT_DEE_WINDOWS) else "dee"),
        help="Path/name of Dolby Encoding Engine executable",
    )
    parser.add_argument("--qaac", default="qaac64", help="Path/name of qaac executable (try qaac if named differently)")
    parser.add_argument("--dee-xml-flag", choices=["--xml", "-xml"], default=DEE_XML_FLAG_DEFAULT,
                        help="XML option supported by the installed DEE version")
    parser.add_argument("--downmix", choices=["dpl2", "ffmpeg"], default="dpl2",
                        help="DPL-II DSP for multichannel inputs, or FFmpeg's standard stereo downmix")
    parser.add_argument("--sample-rate", type=int, choices=[44100, 48000], default=48000,
                        help="Output PCM / encoded audio sample rate")
    parser.add_argument("--lpf-hz", type=float, default=DEFAULT_LPF_HZ,
                        help="Zero-phase low-pass cutoff used by custom DPL-II DSP")
    parser.add_argument("--no-lpf", action="store_true", help="Disable the DPL-II low-pass filter")
    parser.add_argument("--no-prevent-clipping", action="store_true",
                        help="Disable the static whole-file peak safeguard (> 0 dBFS scaling)")

    parser.add_argument("--bitrate", type=int, choices=DEE_DATA_RATES, default=192,
                        help="DEE PCM-to-DD/DDP target data rate in kbps (must be a schema-supported value)")
    parser.add_argument("--dolby-surround-mode", choices=["auto", "yes", "no", "not_indicated"], default="auto",
                        help="Surround flag; auto=yes only when custom DPL-II matrix was applied")
    parser.add_argument("--loudness-preset", choices=LOUDNESS_PRESETS, default="atsc_a85",
                        help="DEE E-AC-3 pcm_to_ddp correction preset; AC-3 uses measure_only as required by schema")
    parser.add_argument("--drc-line", choices=DRC_PROFILES, default="music_light",
                        help="Explicit DEE line-mode DRC profile; only concrete profile values are allowed (never auto)")
    parser.add_argument("--drc-rf", choices=DRC_PROFILES, default="music_light",
                        help="Explicit DEE RF-mode DRC profile; only concrete profile values are allowed (never auto)")

    parser.add_argument("--qaac-mode", choices=["tvbr", "cvbr", "abr"], default="tvbr",
                        help="QAAC AAC mode: true VBR quality, constrained VBR bitrate, or average bitrate")
    parser.add_argument("--qaac-quality", type=int, default=91, metavar="0..127",
                        help="QAAC TVBR quality, 0-127 (used only with --qaac-mode tvbr)")
    parser.add_argument("--qaac-bitrate", type=int, default=192, metavar="0..512",
                        help="QAAC CVBR/ABR bitrate in kbps; 0 requests the encoder's highest rate")

    parser.add_argument("--temp-root", type=Path, default=None,
                        help="Optional temporary root; on Windows it must have a DEE-safe path without spaces")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output after successful encoding")
    parser.add_argument("--keep-temp", action="store_true", help="Keep work directory for debugging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def run_pipeline(args: argparse.Namespace) -> Path:
    input_file = args.input.expanduser().resolve()
    output_file = args.output.expanduser().resolve()

    if not input_file.is_file():
        raise PipelineError(f"Input file does not exist: {input_file}")
    if input_file == output_file:
        raise PipelineError("Input and output paths must be different.")
    if args.audio_stream < 0:
        raise PipelineError("--audio-stream must be 0 or a positive integer.")
    if args.bitrate <= 0:
        raise PipelineError("--bitrate must be greater than zero.")
    if args.qaac_quality not in range(0, 128):
        raise PipelineError("--qaac-quality must be in the range 0-127.")
    if args.qaac_bitrate not in range(0, 513):
        raise PipelineError("--qaac-bitrate must be in the range 0-512 kbps.")
    if args.downmix == "dpl2" and not args.no_lpf and args.lpf_hz >= args.sample_rate / 2:
        raise PipelineError(
            f"--lpf-hz must be lower than Nyquist ({args.sample_rate / 2:g} Hz); use --no-lpf or a lower cutoff."
        )
    output_kind = OUTPUT_BY_SUFFIX.get(output_file.suffix.lower())
    if not output_kind:
        allowed = ", ".join(OUTPUT_BY_SUFFIX.keys())
        raise PipelineError(f"Unsupported output suffix {output_file.suffix!r}. Supported suffixes: {allowed}.")
    if output_file.exists() and not args.overwrite:
        raise PipelineError(f"Output already exists: {output_file}. Pass --overwrite to replace it.")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = require_exe(args.ffmpeg, "FFmpeg")

    # ffprobe is needed to avoid accidentally upmixing an already-stereo source to 5.1.
    ffprobe = require_exe(args.ffprobe, "ffprobe")
    probe = probe_audio(ffprobe, input_file, args.audio_stream)
    source_channels = int(probe.get("channels") or 0)
    source_layout = probe.get("channel_layout", "unknown")
    source_codec = probe.get("codec_name", "unknown")
    print(f"[INPUT] {input_file}")
    print(f"[INPUT] codec={source_codec}, channels={source_channels}, layout={source_layout}, audio stream=a:{args.audio_stream}")
    print(f"[OUTPUT] {output_file} ({output_kind})")

    need_dpl2 = args.downmix == "dpl2" and source_channels >= 6
    dpl2_applied = False
    dee: str | None = None
    qaac: str | None = None
    if output_kind in ("ac3", "eac3"):
        dee = require_exe(args.dee, "Dolby Encoding Engine (DEE)")
    elif output_kind == "aac":
        qaac = require_exe(args.qaac, "qaac")

    work_root = choose_work_root(output_file.parent, args.temp_root)
    work_dir = Path(tempfile.mkdtemp(prefix="transcode2dolbyaud_", dir=str(work_root)))
    # All files referenced by DEE, including the output target in its XML, stay
    # inside this whitespace-free temporary directory. Publish to the user path
    # only after DEE/QAAC exits, so DEE never has to parse a spaced output path.
    staging_output = work_dir / ("encoded_output" + output_file.suffix.lower())
    publish_stage: Path | None = None

    try:
        print(f"[TEMP] DEE-safe working directory: {work_dir}")
        stereo_wav = work_dir / "stereo_float32.wav"
        if need_dpl2:
            wav_51 = work_dir / "input_51_float32.wav"
            print("[STAGE 1] Multichannel input detected; decoding to 6-channel float32 PCM WAV.")
            decode_to_float_wav(
                ffmpeg, input_file, wav_51, audio_stream=args.audio_stream,
                channels=6, sample_rate=args.sample_rate,
            )
            dpl2_ltrt_downmix(
                wav_51, stereo_wav,
                sample_rate_expected=args.sample_rate,
                lpf_hz=None if args.no_lpf else args.lpf_hz,
                prevent_clipping=not args.no_prevent_clipping,
                ffmpeg=ffmpeg,
            )
            dpl2_applied = True
        else:
            if args.downmix == "dpl2" and source_channels < 6:
                print(
                    "[WARN] Input has fewer than 6 channels; a 5.1 DPL-II matrix cannot be applied. "
                    "Falling back to FFmpeg stereo conversion for this input."
                )
            make_ffmpeg_stereo(
                ffmpeg, input_file, stereo_wav,
                audio_stream=args.audio_stream, sample_rate=args.sample_rate,
            )

        surround_mode = choose_surround_mode(args.dolby_surround_mode, dpl2_applied)
        if output_kind in ("ac3", "eac3"):
            assert dee is not None
            encode_dolby(
                dee, stereo_wav, staging_output, work_dir, args, surround_mode,
                output_kind=output_kind,
            )
        else:
            assert qaac is not None
            encode_aac(
                ffmpeg, qaac, stereo_wav, staging_output, work_dir, args,
                output_file.suffix,
            )

        if output_file.exists() and not args.overwrite:
            raise PipelineError(f"Output appeared during processing; refusing to replace it: {output_file}")

        # Copy the finished product to a sibling staging file so the final replace
        # stays atomic even when the temp root and destination are on different drives.
        publish_fd, publish_name = tempfile.mkstemp(
            prefix=f".{output_file.stem}.partial_", suffix=output_file.suffix,
            dir=str(output_file.parent),
        )
        os.close(publish_fd)
        publish_stage = Path(publish_name)
        publish_stage.unlink(missing_ok=True)
        shutil.copy2(staging_output, publish_stage)
        os.replace(publish_stage, output_file)
        publish_stage = None
        print("\n" + "=" * 72)
        print("[SUCCESS] Transcoding completed.")
        print(f"  Input : {input_file}")
        print(f"  Output: {output_file}")
        print(f"  Codec : {output_kind}; sample rate {args.sample_rate} Hz; 2.0 stereo")
        print(f"  Matrix: {'custom DPL-II Lt/Rt' if dpl2_applied else 'FFmpeg stereo downmix/conversion'}")
        if output_kind in ("ac3", "eac3"):
            print(f"  Dolby Surround bitstream flag: {surround_mode}")
        if output_kind == "aac":
            if args.qaac_mode == "tvbr":
                print(f"  QAAC : TVBR quality {args.qaac_quality}")
            else:
                print(f"  QAAC : {args.qaac_mode.upper()} {args.qaac_bitrate} kbps")
        print("=" * 72)
        return output_file
    finally:
        if publish_stage is not None and publish_stage.exists():
            try:
                publish_stage.unlink()
            except OSError:
                pass
        if args.keep_temp:
            print(f"[INFO] Kept temporary work directory: {work_dir}")
        else:
            shutil.rmtree(work_dir, ignore_errors=True)
            print("[TEMP] Temporary work directory removed.")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    print(
        f"[SCRIPT] transcode2dolbyaud.py v{VERSION} | DEE filter=pcm_to_ddp v3 | "
        f"DRC line={args.drc_line}, RF={args.drc_rf}", flush=True
    )
    try:
        run_pipeline(args)
        return 0
    except PipelineError as exc:
        print(f"\n[ERROR] {exc}", file=sys.stderr)
        if getattr(args, "keep_temp", False):
            print("[INFO] --keep-temp was enabled; intermediate files are retained where available.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[ABORTED] Interrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\n[UNEXPECTED ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        if getattr(args, "keep_temp", False):
            print("[INFO] --keep-temp was enabled; inspect remaining intermediate files.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
