#!/usr/bin/env python3
# verify_ltrt_pipeline.py
#
# Verification Test:
# 1. Generate 5.1 Float32 Test WAV (FL, FR, C, LFE, Ls, Rs tones)
# 2. Downmix 5.1 -> 2.0 Lt/Rt Float32 PCM WAV (DPL II + Phase 90°)
# 3. Dolby DEE -> AC-3 192 kbps Stereo (acmod 2_0, dolby_surround_mode=yes)
# 4. FFmpeg decodes AC-3 2.0 -> 2.0 PCM WAV
# 5. Measure Lt/Rt dB, Phase, and classify matrix

import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import numpy as np
    import scipy.io.wavfile as wavfile
    from scipy.signal import hilbert
except ImportError:
    print("ERROR: กรุณาติดตั้ง numpy และ scipy: pip install numpy scipy", file=sys.stderr)
    sys.exit(1)


SR = 48_000
TONE_HZ = 1_000.0
AMP = 0.25
LEAD_S = 0.50
SEG_S = 2.00
TAIL_S = 0.50
FADE_S = 0.10

# ลำดับช่องสัญญาณมาตรฐาน 5.1 WAV: FL, FR, C, LFE, Ls, Rs
SEGMENTS = [
    ("FL",  "FL",  TONE_HZ),
    ("FR",  "FR",  TONE_HZ),
    ("C",   "C",   TONE_HZ),
    ("LFE", "LFE",   60.0),
    ("Ls",  "Ls",  TONE_HZ),
    ("Rs",  "Rs",  TONE_HZ),
]

# Coefficients จากสูตร DPL II
G_CENTER = np.float32(10 ** (-3.0 / 20.0))   # -3.0 dB
G_MAJOR  = np.float32(10 ** (-1.2 / 20.0))   # -1.2 dB
G_MINOR  = np.float32(10 ** (-6.2 / 20.0))   # -6.2 dB


def die(msg: str, rc: int = 1) -> None:
    print(f"\n[ERROR] {msg}", file=sys.stderr)
    sys.exit(rc)


def find_tool(name: str) -> str:
    p = shutil.which(name)
    if p:
        return p
    if os.name == "nt" and not name.lower().endswith(".exe"):
        p = shutil.which(name + ".exe")
        if p:
            return p
    die(f"ไม่พบเครื่องมือ '{name}' ใน PATH")


def run_cmd(cmd: list[str]) -> None:
    shown = subprocess.list2cmdline(cmd) if os.name == "nt" else " ".join(cmd)
    print("\n>", shown)
    subprocess.run(cmd, check=True)


def write_test_51_f32_wav(path: Path) -> None:
    """สร้าง 5.1 Float32 WAV สัญญาณทดสอบทีละช่อง"""
    lead = int(LEAD_S * SR)
    seg = int(SEG_S * SR)
    tail = int(TAIL_S * SR)
    fade = int(FADE_S * SR)
    frames_total = lead + len(SEGMENTS) * seg + tail

    data = np.zeros((frames_total, 6), dtype=np.float32)
    ch_idx = {"FL": 0, "FR": 1, "C": 2, "LFE": 3, "Ls": 4, "Rs": 5}

    for i, (_, ch_name, hz) in enumerate(SEGMENTS):
        idx = ch_idx[ch_name]
        start_f = lead + i * seg
        end_f = start_f + seg
        n_samples = seg

        t = np.arange(n_samples, dtype=np.float32) / SR
        tone = AMP * np.sin(2.0 * np.pi * hz * t)

        # Apply Fade In/Out
        fade_arr = np.ones(n_samples, dtype=np.float32)
        if fade > 0:
            fade_in = 0.5 - 0.5 * np.cos(np.pi * np.arange(fade) / fade)
            fade_out = 0.5 - 0.5 * np.cos(np.pi * np.arange(fade) / fade)[::-1]
            fade_arr[:fade] = fade_in
            fade_arr[-fade:] = fade_out

        data[start_f:end_f, idx] = tone * fade_arr

    wavfile.write(path, SR, data)


def apply_phase_shift_90(signal: np.ndarray) -> np.ndarray:
    """หมุนเฟสสัญญาณ 90 องศาด้วย Hilbert Transform"""
    analytic_signal = hilbert(signal)
    return np.imag(analytic_signal).astype(np.float32)


def dpl2_ltrt_downmix_dsp(wav_51_in: Path, wav_20_out: Path) -> None:
    """ทำ DPL II + Phase 90 Matrix Downmix"""
    sr, data = wavfile.read(wav_51_in)

    fl = data[:, 0]
    fr = data[:, 1]
    c  = data[:, 2]
    ls = data[:, 4]
    rs = data[:, 5]

    ls_90 = apply_phase_shift_90(ls)
    rs_90 = apply_phase_shift_90(rs)

    lt = fl + (G_CENTER * c) - (G_MAJOR * ls_90) - (G_MINOR * rs_90)
    rt = fr + (G_CENTER * c) + (G_MINOR * ls_90) + (G_MAJOR * rs_90)

    stereo = np.column_stack((lt, rt)).astype(np.float32)

    # Static Peak Normalization
    max_peak = np.max(np.abs(stereo))
    if max_peak > 1.0:
        stereo = stereo / max_peak

    wavfile.write(wav_20_out, sr, stereo)


def generate_dee_xml(input_20_wav: Path, output_ac3: Path, temp_dir: Path) -> Path:
    xml_content = f"""<?xml version="1.0"?>
<job_config>
  <input>
    <audio>
      <wav version="1">
        <file_name>{input_20_wav.name}</file_name>
        <timecode_frame_rate>not_indicated</timecode_frame_rate>
        <offset>auto</offset>
        <ffoa>auto</ffoa>
        <storage><local><path>{input_20_wav.parent.as_posix()}</path></local></storage>
      </wav>
    </audio>
  </input>
  <filter>
    <audio>
      <encode_to_dd_single_pass version="2">
        <threads>4</threads>
        <timecode_frame_rate>not_indicated</timecode_frame_rate>
        <start>first_frame_of_action</start>
        <end>end_of_file</end>
        <time_base>file_position</time_base>
        <loudness>
          <correction_disabled>
            <dialnorm>0</dialnorm>
          </correction_disabled>
        </loudness>
        <bitstream_mode>complete_main</bitstream_mode>
        <acmod>2_0</acmod>
        <data_rate>192</data_rate>
        <lfe_on>false</lfe_on>
        <dolby_surround_mode>yes</dolby_surround_mode>
        <dolby_surround_ex_mode>no</dolby_surround_ex_mode>
        <drc>
          <line_mode_drc_profile>film_light</line_mode_drc_profile>
          <rf_mode_drc_profile>film_light</rf_mode_drc_profile>
        </drc>
        <lfe_lowpass_filter>true</lfe_lowpass_filter>
        <surround_90_degree_phase_shift>false</surround_90_degree_phase_shift>
        <surround_3db_attenuation>false</surround_3db_attenuation>
        <downmix>
          <loro_center_mix_level>-3</loro_center_mix_level>
          <loro_surround_mix_level>-3</loro_surround_mix_level>
          <ltrt_center_mix_level>-3</ltrt_center_mix_level>
          <ltrt_surround_mix_level>-3</ltrt_surround_mix_level>
          <preferred_downmix_mode>ltrt</preferred_downmix_mode>
        </downmix>
      </encode_to_dd_single_pass>
    </audio>
  </filter>
  <output>
    <ac3 version="1">
      <file_name>{output_ac3.name}</file_name>
      <storage><local><path>{output_ac3.parent.as_posix()}</path></local></storage>
    </ac3>
  </output>
  <misc>
    <temp_dir>
      <clean_temp>true</clean_temp>
      <path>{temp_dir.as_posix()}</path>
    </temp_dir>
  </misc>
</job_config>
"""
    xml_path = temp_dir / "dee_job.xml"
    xml_path.write_text(xml_content, encoding="utf-8")
    return xml_path


def decode_ac3_to_pcm(ffmpeg: str, ac3_file: Path, out_wav: Path) -> None:
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", str(ac3_file),
        "-map", "0:a:0",
        "-ar", str(SR),
        "-ac", "2",
        "-c:a", "pcm_s16le",
        "-y", str(out_wav)
    ]
    run_cmd(cmd)


def read_stereo_pcm(path: Path) -> tuple[list[float], list[float]]:
    sr, data = wavfile.read(path)
    if data.dtype == np.int16:
        data = data.astype(np.float64) / 32768.0
    return data[:, 0].tolist(), data[:, 1].tolist()


def dft_tone(x: list[float], start: int, length: int, hz: float) -> complex:
    start = max(0, start)
    end = min(len(x), start + length)
    n = end - start
    if n < SR // 4:
        return 0j

    re, im, wsum = 0.0, 0.0, 0.0
    for i in range(n):
        win = 0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1))
        a = 2.0 * math.pi * hz * i / SR
        s = x[start + i] * win
        re += s * math.cos(a)
        im -= s * math.sin(a)
        wsum += win

    scale = 2.0 / wsum
    return complex(re * scale, im * scale)


def mag_db(z: complex, ref: float) -> float:
    if ref <= 1e-12 or abs(z) <= 1e-12:
        return -120.0
    return 20.0 * math.log10(abs(z) / ref)


def phase_deg(z: complex) -> float:
    return math.degrees(math.atan2(z.imag, z.real))


def wrap_deg(x: float) -> float:
    while x <= -180.0:
        x += 360.0
    while x > 180.0:
        x -= 360.0
    return x


def measure(left: list[float], right: list[float], index: int, hz: float) -> tuple[complex, complex]:
    start = int((LEAD_S + index * SEG_S + 0.50) * SR)
    length = int(1.00 * SR)
    return dft_tone(left, start, length, hz), dft_tone(right, start, length, hz)


def classify(rows: dict[str, tuple[complex, complex]]) -> tuple[str, float, dict[str, float]]:
    ref = max(abs(rows["FL"][0]), 1e-12)

    def db(ch: str, side: int) -> float:
        return mag_db(rows[ch][side], ref)

    candidates = {
        "Lo/Ro": [
            (db("Ls", 0), -3.0), (db("Ls", 1), -60.0),
            (db("Rs", 0), -60.0), (db("Rs", 1), -3.0),
            (db("C", 0), -3.0), (db("C", 1), -3.0),
        ],
        "Dolby Surround / legacy Pro Logic": [
            (db("Ls", 0), -3.0), (db("Ls", 1), -3.0),
            (db("Rs", 0), -3.0), (db("Rs", 1), -3.0),
            (db("C", 0), -3.0), (db("C", 1), -3.0),
        ],
        "Dolby Pro Logic II": [
            (db("Ls", 0), -1.2), (db("Ls", 1), -6.2),
            (db("Rs", 0), -6.2), (db("Rs", 1), -1.2),
            (db("C", 0), -3.0), (db("C", 1), -3.0),
        ],
    }

    scores: dict[str, float] = {}
    for name, pairs in candidates.items():
        err = 0.0
        for measured, expected in pairs:
            if expected <= -50:
                err += max(0.0, measured + 50.0) ** 2
            else:
                err += (measured - expected) ** 2
        scores[name] = math.sqrt(err / len(pairs))

    best = min(scores, key=scores.get)
    return best, scores[best], scores


def print_results(rows: dict[str, tuple[complex, complex]]) -> None:
    ref = max(abs(rows["FL"][0]), 1e-12)
    ref_phase = phase_deg(rows["FL"][0])

    print("\n" + "=" * 82)
    print("DECODED AC-3 (2.0 STEREO) MATRIX VERIFICATION TEST")
    print("=" * 82)
    print(f"Reference: FL -> Lt = 0 dB, common phase = {ref_phase:+.2f} deg\n")
    print(f"{'Input':<5} {'Lt dB':>9} {'Rt dB':>9} {'Lt phase':>11} {'Rt phase':>11} {'Lt-Rt':>9}")
    print("-" * 82)

    for ch in ("FL", "FR", "C", "LFE", "Ls", "Rs"):
        l, r = rows[ch]
        ldb = mag_db(l, ref)
        rdb = mag_db(r, ref)
        lp = wrap_deg(phase_deg(l) - ref_phase)
        rp = wrap_deg(phase_deg(r) - ref_phase)
        lr = wrap_deg(phase_deg(l) - phase_deg(r)) if abs(l) > 1e-10 and abs(r) > 1e-10 else float("nan")
        lr_s = f"{lr:+.2f}" if math.isfinite(lr) else "n/a"
        print(f"{ch:<5} {ldb:>9.2f} {rdb:>9.2f} {lp:>11.2f} {rp:>11.2f} {lr_s:>9}")

    best, score, scores = classify(rows)

    print("\nClassification:")
    print(f"  -> {best}")
    print(f"  fit error: {score:.2f} dB RMS")

    print("\nCandidate fit (lower is better):")
    for name, s in sorted(scores.items(), key=lambda kv: kv[1]):
        print(f"  {name:<34} {s:.2f} dB")
    print("=" * 82)


def main() -> None:
    ffmpeg = find_tool("ffmpeg")
    dee = find_tool("dee")

    work_root = Path("C:/dee_task/verify_run") if os.name == "nt" else Path("/tmp/dee_task/verify_run")
    work_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="verify_pipe_", dir=str(work_root)) as td:
        temp_dir = Path(td)

        wav_51_in = temp_dir / "test_51_source.wav"
        wav_20_ltrt = temp_dir / "test_20_ltrt.wav"
        ac3_out = temp_dir / "test_20_ltrt.ac3"
        wav_decoded = temp_dir / "test_20_decoded.wav"

        print("1. Generating 5.1 Test WAV (Float32)...")
        write_test_51_f32_wav(wav_51_in)

        print("2. Performing Custom DSP DPL II (Phase 90°) Downmix...")
        dpl2_ltrt_downmix_dsp(wav_51_in, wav_20_ltrt)

        print("3. Encoding 2.0 Lt/Rt WAV to AC-3 192 kbps with DEE...")
        job_xml = generate_dee_xml(wav_20_ltrt, ac3_out, temp_dir)
        run_cmd([dee, "--xml", str(job_xml), "--progress", "--stdout"])

        print("4. Decoding AC-3 2.0 back to PCM Stereo WAV with FFmpeg...")
        decode_ac3_to_pcm(ffmpeg, ac3_out, wav_decoded)

        print("5. Analyzing decoded PCM stereo matrix...")
        left, right = read_stereo_pcm(wav_decoded)
        rows = {label: measure(left, right, i, hz) for i, (label, _, hz) in enumerate(SEGMENTS)}

        print_results(rows)


if __name__ == "__main__":
    main()