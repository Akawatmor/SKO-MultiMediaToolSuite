#!/usr/bin/env python3
# pipeline_51_to_ac3_ltrt.py
#
# Full Automated Pipeline with 18.5 kHz Low-Pass Filter:
# 1. Any Audio Input -> FFmpeg check -> 5.1 Float32 PCM WAV (48 kHz)
# 2. 5.1 Float32 WAV -> Custom DPL II (Phase 90) Matrix -> 18.5kHz Low-Pass -> 2.0 Lt/Rt Float32 WAV
# 3. 2.0 Lt/Rt Float32 WAV -> Dolby DEE -> AC-3 192 kbps Stereo (Dolby Surround Encoded)

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import numpy as np
    import scipy.io.wavfile as wavfile
    from scipy.signal import hilbert, butter, sosfiltfilt
except ImportError:
    print("ERROR: กรุณาติดตั้ง numpy และ scipy: pip install numpy scipy", file=sys.stderr)
    sys.exit(1)


# สัมประสิทธิ์ Linear Coefficients (Float32 Precision)
G_CENTER = np.float32(10 ** (-3.0 / 20.0))   # -3.0 dB  (~0.70794578)
G_MAJOR  = np.float32(10 ** (-1.2 / 20.0))   # -1.2 dB  (~0.87096359)
G_MINOR  = np.float32(10 ** (-6.2 / 20.0))   # -6.2 dB  (~0.48977882)

# กำหนดความถี่ Cutoff สำหรับ Low-Pass Filter
CUTOFF_HZ = 18500.0


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
    die(f"ไม่พบเครื่องมือ '{name}' ใน PATH กรุณาตรวจสอบการติดตั้ง")


def run_cmd(cmd: list[str]) -> None:
    shown = subprocess.list2cmdline(cmd) if os.name == "nt" else " ".join(cmd)
    print("\n>", shown)
    subprocess.run(cmd, check=True)


def ensure_float32_51_wav(ffmpeg: str, input_file: Path, temp_dir: Path) -> Path:
    """
    ตรวจสอบและแปลงไฟล์อินพุตให้กลายเป็น 5.1 Float32 PCM WAV (48 kHz) ด้วย FFmpeg
    """
    output_wav = temp_dir / "input_51_float32.wav"
    
    if input_file.suffix.lower() == ".wav":
        try:
            sr, data = wavfile.read(input_file)
            if sr == 48000 and data.ndim == 2 and data.shape[1] == 6 and data.dtype == np.float32:
                print(f"[STAGE 1] ไฟล์อินพุตเป็น 5.1 Float32 WAV เรียบร้อยแล้ว: {input_file.name}")
                return input_file
        except Exception:
            pass

    print(f"[STAGE 1] กำลังแปลงไฟล์อินพุตเป็น 5.1 Float32 WAV (48kHz) ด้วย FFmpeg...")
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", str(input_file),
        "-ar", "48000",
        "-ac", "6",
        "-c:a", "pcm_f32le",
        "-y", str(output_wav)
    ]
    run_cmd(cmd)
    return output_wav


def apply_phase_shift_90(signal: np.ndarray) -> np.ndarray:
    """หมุนเฟสสัญญาณ 90 องศาด้วย Hilbert Transform"""
    analytic_signal = hilbert(signal)
    return np.imag(analytic_signal).astype(np.float32)


def apply_lowpass_18p5khz(stereo_data: np.ndarray, sr: int = 48000, cutoff: float = CUTOFF_HZ) -> np.ndarray:
    """
    Zero-Phase Butterworth Low-Pass Filter (~18.5 kHz)
    ใช้ sosfiltfilt เพื่อป้องกัน Phase Distortion ไม่ให้ความต่างเฟสของ DPL II เพี้ยน
    """
    sos = butter(N=8, Wn=cutoff, btype='low', fs=sr, output='sos')
    filtered = sosfiltfilt(sos, stereo_data, axis=0)
    return filtered.astype(np.float32)


def dpl2_ltrt_downmix_dsp(wav_51_in: Path, wav_20_out: Path) -> None:
    """
    คำนวณ Downmix 5.1 -> 2.0 Lt/Rt Float32 WAV ด้วยสูตร Dolby Pro Logic II (Phase 90) + 18.5kHz Low-Pass
    """
    print(f"[STAGE 2] กำลังประมวลผล Lt/Rt Downmix (DPL II + Phase 90° + 18.5kHz Low-Pass)...")
    sr, data = wavfile.read(wav_51_in)

    if data.dtype != np.float32:
        data = data.astype(np.float32)

    # ช่องสัญญาณ: 0:FL, 1:FR, 2:C, 3:LFE, 4:Ls, 5:Rs
    fl = data[:, 0]
    fr = data[:, 1]
    c  = data[:, 2]
    ls = data[:, 4]
    rs = data[:, 5]

    # 1. หมุนเฟส Surround Ls/Rs 90 องศา
    ls_90 = apply_phase_shift_90(ls)
    rs_90 = apply_phase_shift_90(rs)

    # 2. สมการ Matrix DPL II (Phase 90)
    lt = fl + (G_CENTER * c) - (G_MAJOR * ls_90) - (G_MINOR * rs_90)
    rt = fr + (G_CENTER * c) + (G_MINOR * ls_90) + (G_MAJOR * rs_90)

    stereo = np.column_stack((lt, rt)).astype(np.float32)

    # 3. ประมวลผล Low-Pass Filter 18.5 kHz (Zero-Phase)
    stereo = apply_lowpass_18p5khz(stereo, sr=sr, cutoff=CUTOFF_HZ)

    # 4. Static Peak Normalization ป้องกัน Peak Clip เกิน 0 dBFS โดยไม่ใช้ Dynamic Compression
    max_peak = np.max(np.abs(stereo))
    if max_peak > 1.0:
        peak_db = 20 * np.log10(max_peak)
        print(f"         [INFO] สัญญาณ Peak เกิน 0 dBFS (+{peak_db:.2f} dB) ทำการย่อขนาดแบบ Static Normalization ทั้งไฟล์")
        stereo = stereo / max_peak

    wavfile.write(wav_20_out, sr, stereo)
    print(f"         [SUCCESS] สร้างไฟล์ 2.0 Lt/Rt Float32 WAV (Filtered 18.5kHz) สำเร็จ: {wav_20_out.name}")


def generate_dee_xml(input_20_wav: Path, output_ac3: Path, temp_dir: Path) -> Path:
    """
    สร้างไฟล์ XML Config สำหรับ DEE เพื่อแปลง 2.0 Lt/Rt WAV -> AC-3 192 kbps Stereo
    """
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


def main() -> None:
    if len(sys.argv) < 3:
        print("วิธีใช้งาน: python pipeline_51_to_ac3_ltrt.py <input_audio_file> <output_ac3_file>")
        sys.exit(1)

    input_file = Path(sys.argv[1]).expanduser().resolve()
    output_ac3 = Path(sys.argv[2]).expanduser().resolve()

    if not input_file.exists():
        die(f"ไม่พบไฟล์อินพุต: {input_file}")

    ffmpeg = find_tool("ffmpeg")
    dee = find_tool("dee")

    work_root = Path("C:/dee_task/pipeline_run") if os.name == "nt" else Path("/tmp/dee_task/pipeline_run")
    work_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ltrt_pipe_", dir=str(work_root)) as td:
        temp_dir = Path(td)
        wav_20_ltrt = temp_dir / "interm_20_ltrt.wav"

        # Stage 1: ตรวจสอบและแปลงเป็น 5.1 Float32 WAV
        wav_51_f32 = ensure_float32_51_wav(ffmpeg, input_file, temp_dir)

        # Stage 2: คำนวณ Lt/Rt Matrix Downmix + 18.5kHz Low-Pass (Float32)
        dpl2_ltrt_downmix_dsp(wav_51_f32, wav_20_ltrt)

        # Stage 3: เข้ารหัสด้วย DEE เป็น AC-3 192 kbps
        print(f"\n[STAGE 3] กำลังเข้ารหัส AC-3 192 kbps Stereo (Dolby Surround Encoded) ด้วย Dolby DEE...")
        job_xml = generate_dee_xml(wav_20_ltrt, output_ac3, temp_dir)
        run_cmd([dee, "--xml", str(job_xml), "--progress", "--stdout"])

    if not output_ac3.exists():
        die(f"การแปลงไฟล์เสร็จสิ้นแต่ไม่พบไฟล์ AC-3 ผลลัพธ์: {output_ac3}")

    print("\n" + "=" * 70)
    print("[COMPLETED] กระบวนการทั้งหมดเสร็จสิ้นเรียบร้อยแล้ว!")
    print(f"  -> Input Source : {input_file}")
    print(f"  -> Output AC-3  : {output_ac3} (192 kbps / 2.0 Stereo / Lt-Rt Encoded / LPF 18.5kHz)")
    print("=" * 70)


if __name__ == "__main__":
    main()