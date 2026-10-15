import os
import queue
import shutil
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET
from xml.dom import minidom

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# ถ้า DEE รุ่นของคุณไม่รับ "--xml" ให้ลองเปลี่ยนเป็น "-xml" (ดู dee.exe --help)
DEE_XML_FLAG = "--xml"

# DEE transcode_to_ddp รับอินพุตแบบ ac3 / ec3 / wav_s337m / dde เท่านั้น (ไม่รับ PCM WAV)
# ไฟล์อื่นจึงถูกแปลงเป็น AC3 ด้วย FFmpeg ก่อน (640 kbps = เพดานของ AC-3 เพื่อลด generation loss)
INTERMEDIATE_AC3_BITRATE = "640k"

BITRATES = ["auto", "32", "40", "48", "56", "64", "72", "80", "88", "96", "104",
            "112", "120", "128", "144", "160", "176", "192", "200", "208", "216",
            "224", "232", "240", "248", "256", "272", "288", "304", "320", "336",
            "352", "368", "384", "400", "448", "512", "576", "640", "704", "768",
            "832", "896", "960", "1008", "1024"]
DOWNMIX_MODES = ["auto", "loro", "ltrt", "ltrt-pl2", "not_indicated"]
CENTER_LEVELS = ["auto", "+3", "+1.5", "0", "-1.5", "-3", "-4.5", "-6", "-inf"]
SURROUND_LEVELS = ["auto", "-1.5", "-3", "-4.5", "-6", "-inf"]
SURROUND_MODES = ["auto", "yes", "no", "not_indicated"]
LOUDNESS_PRESETS = ["atsc_a85", "atsc_a85_agile", "ebu_r128", "freetv_op59", "arib_b32"]
DRC_PROFILES = ["auto", "film_standard", "film_light", "music_standard", "music_light", "speech"]

# FFmpeg ac3 encoder รองรับสูงสุด 5.1 ; aformat จะ up/down-mix ให้เอง
CHANNEL_FILTERS = {
    "5.1_standard": "aformat=channel_layouts=5.1",
    "stereo": "aformat=channel_layouts=stereo",
    "keep_original": "aformat=channel_layouts=mono|stereo|3.0|4.0|quad|5.0|5.1",
}

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# Helpers (ไม่ผูกกับ Tkinter เพื่อให้ทดสอบแยกได้)
# ---------------------------------------------------------------------------
def resolve_exe(path):
    """คืน path เต็มของ exe (รองรับทั้ง path ตรง ๆ และชื่อที่อยู่ใน PATH) ไม่เจอคืน None"""
    if not path:
        return None
    if os.path.isfile(path):
        return path
    return shutil.which(path)


def detect_dee_input_type(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".ac3":
        return "ac3"
    if ext in (".ec3", ".eac3"):
        return "ec3"
    return None


def build_manifest(input_file, output_file, xml_path, temp_dir, s):
    """สร้าง DEE job XML ให้ตรง schema ของ transcode_to_ddp (ac3/ec3 input -> ec3 output)"""
    input_type = detect_dee_input_type(input_file)
    if input_type is None:
        raise ValueError(f"DEE input must be .ac3/.ec3/.eac3, got: {input_file}")

    in_dir, in_name = os.path.split(os.path.abspath(input_file))
    out_dir, out_name = os.path.split(os.path.abspath(output_file))

    job = ET.Element("job_config")

    # <input>
    audio_in = ET.SubElement(ET.SubElement(job, "input"), "audio")
    t = ET.SubElement(audio_in, input_type, version="1")
    ET.SubElement(t, "file_name").text = in_name
    ET.SubElement(ET.SubElement(ET.SubElement(t, "storage"), "local"), "path").text = in_dir

    # <filter>
    audio_f = ET.SubElement(ET.SubElement(job, "filter"), "audio")
    tc = ET.SubElement(audio_f, "transcode_to_ddp", version="1")

    loud = ET.SubElement(ET.SubElement(tc, "loudness"), "measure_and_correct")
    ET.SubElement(loud, "preset").text = s["loudness_preset"]

    ET.SubElement(tc, "data_rate").text = s["bitrate"]
    ET.SubElement(tc, "lfe_on").text = "auto"
    ET.SubElement(tc, "dolby_surround_mode").text = s["surround_mode"]
    ET.SubElement(tc, "dolby_surround_ex_mode").text = "auto"

    drc = ET.SubElement(tc, "drc")
    ET.SubElement(drc, "line_mode_drc_profile").text = s["drc_line"]
    ET.SubElement(drc, "rf_mode_drc_profile").text = s["drc_rf"]

    ET.SubElement(tc, "lfe_lowpass_filter").text = "auto"
    ET.SubElement(tc, "surround_90_degree_phase_shift").text = "auto"
    ET.SubElement(tc, "surround_3db_attenuation").text = "auto"

    dm = ET.SubElement(tc, "downmix")
    ET.SubElement(dm, "loro_center_mix_level").text = s["center_mix"]
    ET.SubElement(dm, "loro_surround_mix_level").text = s["surround_mix"]
    ET.SubElement(dm, "ltrt_center_mix_level").text = s["center_mix"]
    ET.SubElement(dm, "ltrt_surround_mix_level").text = s["surround_mix"]
    ET.SubElement(dm, "preferred_downmix_mode").text = s["downmix_mode"]

    # <output>
    ec3 = ET.SubElement(ET.SubElement(job, "output"), "ec3", version="1")
    ET.SubElement(ec3, "file_name").text = out_name
    ET.SubElement(ET.SubElement(ET.SubElement(ec3, "storage"), "local"), "path").text = out_dir

    # <misc>
    tmp = ET.SubElement(ET.SubElement(job, "misc"), "temp_dir")
    ET.SubElement(tmp, "clean_temp").text = "true"
    ET.SubElement(tmp, "path").text = temp_dir

    pretty = minidom.parseString(ET.tostring(job, encoding="utf-8")).toprettyxml(indent="  ")
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(pretty)


def run_process(cmd, log, prefix=""):
    """รันโปรเซสและส่งทุกบรรทัดของ output เข้า log() คืน return code"""
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=CREATE_NO_WINDOW,
    )
    for line in process.stdout:
        line = line.rstrip()
        if line:
            log(prefix + line)
    process.stdout.close()
    return process.wait()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext


class DEEConverterGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Dolby Encoding Engine (DEE) - Universal Converter")
        self.root.geometry("820x820")
        self.root.minsize(750, 750)

        self.log_queue = queue.Queue()

        # Executables
        self.dee_path_var = tk.StringVar(value=self.find_default_dee())
        self.ffmpeg_path_var = tk.StringVar(value=self.find_default_ffmpeg())

        # Files
        self.input_file_var = tk.StringVar()
        self.output_file_var = tk.StringVar()
        self.output_ext_var = tk.StringVar(value=".ec3")
        self.force_ffmpeg_var = tk.BooleanVar(value=False)

        # FFmpeg pre-processing
        self.channel_map_var = tk.StringVar(value="5.1_standard")
        self.pcm_sample_rate_var = tk.StringVar(value="48000")

        # Dolby settings
        self.bitrate_var = tk.StringVar(value="192")
        self.downmix_mode_var = tk.StringVar(value="auto")
        self.center_mix_var = tk.StringVar(value="auto")
        self.surround_mix_var = tk.StringVar(value="auto")
        self.surround_mode_var = tk.StringVar(value="auto")
        self.drc_line_var = tk.StringVar(value="auto")
        self.drc_rf_var = tk.StringVar(value="auto")
        self.loudness_preset_var = tk.StringVar(value="atsc_a85")

        self.create_widgets()
        self.root.after(100, self.drain_log_queue)

    # ----- defaults -----
    def find_default_dee(self):
        default_path = r"C:\Encoder dll\dolby\dee\dee.exe"
        if os.path.isfile(default_path):
            return default_path
        return shutil.which("dee") or "dee.exe"

    def find_default_ffmpeg(self):
        return shutil.which("ffmpeg") or "ffmpeg.exe"

    # ----- UI -----
    def create_widgets(self):
        padding = {"padx": 8, "pady": 5}

        # 1. Executables
        exe_frame = ttk.LabelFrame(self.root, text=" 1. Executables Location ")
        exe_frame.pack(fill="x", **padding)
        exe_frame.columnconfigure(1, weight=1)

        ttk.Label(exe_frame, text="DEE Path:").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(exe_frame, textvariable=self.dee_path_var).grid(row=0, column=1, sticky="ew", padx=5, pady=3)
        ttk.Button(exe_frame, text="Browse...", command=self.browse_dee).grid(row=0, column=2, padx=5, pady=3)

        ttk.Label(exe_frame, text="FFmpeg Path:").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(exe_frame, textvariable=self.ffmpeg_path_var).grid(row=1, column=1, sticky="ew", padx=5, pady=3)
        ttk.Button(exe_frame, text="Browse...", command=self.browse_ffmpeg).grid(row=1, column=2, padx=5, pady=3)

        # 2. Input / Output
        io_frame = ttk.LabelFrame(self.root, text=" 2. Input / Output Files ")
        io_frame.pack(fill="x", **padding)
        io_frame.columnconfigure(1, weight=1)

        ttk.Label(io_frame, text="Input File:").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(io_frame, textvariable=self.input_file_var).grid(row=0, column=1, sticky="ew", padx=5, pady=3)
        ttk.Button(io_frame, text="Browse...", command=self.browse_input).grid(row=0, column=2, padx=5, pady=3)

        ttk.Label(io_frame, text="Output Format:").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ext_frame = ttk.Frame(io_frame)
        ext_frame.grid(row=1, column=1, sticky="w", padx=5, pady=3)
        ttk.Radiobutton(ext_frame, text=".ec3", variable=self.output_ext_var, value=".ec3",
                        command=self.update_output_path).pack(side="left", padx=10)
        ttk.Radiobutton(ext_frame, text=".eac3", variable=self.output_ext_var, value=".eac3",
                        command=self.update_output_path).pack(side="left", padx=10)

        ttk.Label(io_frame, text="Output File:").grid(row=2, column=0, sticky="w", padx=5, pady=3)
        ttk.Entry(io_frame, textvariable=self.output_file_var).grid(row=2, column=1, sticky="ew", padx=5, pady=3)
        ttk.Button(io_frame, text="Save As...", command=self.browse_output).grid(row=2, column=2, padx=5, pady=3)

        ttk.Checkbutton(
            io_frame,
            text="Force FFmpeg re-encode via AC3 first (ไม่จำเป็นสำหรับ .ac3/.ec3 — ใช้เมื่ออยากปรับ channel/sample rate)",
            variable=self.force_ffmpeg_var,
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=5, pady=3)

        # 3. FFmpeg mapping
        mapping_frame = ttk.LabelFrame(self.root, text=" 3. FFmpeg Pre-Processing & Channel Mapping ")
        mapping_frame.pack(fill="x", **padding)

        ttk.Label(mapping_frame, text="Channel Layout Option:").grid(row=0, column=0, sticky="w", padx=5, pady=3)
        map_options = [
            ("5.1 (up/down-mix อัตโนมัติ)", "5.1_standard"),
            ("Downmix to Stereo (L, R)", "stereo"),
            ("Keep Original (สูงสุด 5.1)", "keep_original"),
        ]
        for col, (text, val) in enumerate(map_options, start=1):
            ttk.Radiobutton(mapping_frame, text=text, variable=self.channel_map_var,
                            value=val).grid(row=0, column=col, sticky="w", padx=5, pady=3)

        ttk.Label(mapping_frame, text="Sample Rate:").grid(row=1, column=0, sticky="w", padx=5, pady=3)
        ttk.Combobox(mapping_frame, textvariable=self.pcm_sample_rate_var,
                     values=["48000", "44100", "32000"], state="readonly",
                     width=12).grid(row=1, column=1, sticky="w", padx=5, pady=3)

        # 4. Dolby params
        cfg = ttk.LabelFrame(self.root, text=" 4. Dolby Encoding Parameters ")
        cfg.pack(fill="x", **padding)

        def combo(label, var, values, row, col):
            ttk.Label(cfg, text=label).grid(row=row, column=col, sticky="w", padx=5, pady=4)
            ttk.Combobox(cfg, textvariable=var, values=values, state="readonly",
                         width=14).grid(row=row, column=col + 1, sticky="w", padx=5, pady=4)

        combo("Bitrate (kbps):", self.bitrate_var, BITRATES, 0, 0)
        combo("Preferred Downmix Mode:", self.downmix_mode_var, DOWNMIX_MODES, 0, 2)
        combo("Center Mix Level:", self.center_mix_var, CENTER_LEVELS, 1, 0)
        combo("Surround Mix Level:", self.surround_mix_var, SURROUND_LEVELS, 1, 2)
        combo("Dolby Surround Mode:", self.surround_mode_var, SURROUND_MODES, 2, 0)
        combo("Loudness Preset:", self.loudness_preset_var, LOUDNESS_PRESETS, 2, 2)
        combo("Line DRC Profile:", self.drc_line_var, DRC_PROFILES, 3, 0)
        combo("RF DRC Profile:", self.drc_rf_var, DRC_PROFILES, 3, 2)

        # 5. Button & log
        self.btn_convert = ttk.Button(self.root, text="START CONVERSION PROCESS",
                                      command=self.start_encoding_thread)
        self.btn_convert.pack(fill="x", padx=10, pady=8)

        log_frame = ttk.LabelFrame(self.root, text=" Real-Time Process Console ")
        log_frame.pack(fill="both", expand=True, **padding)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap="word", height=12,
            background="#121212", foreground="#00FF66", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)

    # ----- browse -----
    def browse_dee(self):
        f = filedialog.askopenfilename(title="Select DEE Executable",
                                       filetypes=[("Executable", "*.exe"), ("All Files", "*.*")])
        if f:
            self.dee_path_var.set(f)

    def browse_ffmpeg(self):
        f = filedialog.askopenfilename(title="Select FFmpeg Executable",
                                       filetypes=[("Executable", "*.exe"), ("All Files", "*.*")])
        if f:
            self.ffmpeg_path_var.set(f)

    def browse_input(self):
        f = filedialog.askopenfilename(
            title="Select Any Audio/Video File",
            filetypes=[("All Supported Files",
                        "*.ac3 *.ec3 *.eac3 *.wav *.mkv *.mp4 *.avi *.m4a *.flac *.aac *.dts *.ts *.m2ts"),
                       ("All Files", "*.*")])
        if f:
            self.input_file_var.set(f)
            self.update_output_path()

    def update_output_path(self):
        input_path = self.input_file_var.get()
        if input_path:
            base = os.path.splitext(input_path)[0]
            self.output_file_var.set(f"{base}_dee{self.output_ext_var.get()}")

    def browse_output(self):
        ext = self.output_ext_var.get()
        f = filedialog.asksaveasfilename(
            title="Save Output File", defaultextension=ext,
            filetypes=[(f"Dolby Digital Plus ({ext})", f"*{ext}"), ("All Files", "*.*")])
        if f:
            self.output_file_var.set(f)

    # ----- thread-safe logging -----
    def log(self, message):
        """เรียกได้จากทุก thread"""
        self.log_queue.put(message)

    def drain_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.insert(tk.END, msg + "\n")
                self.log_text.see(tk.END)
        except queue.Empty:
            pass
        self.root.after(100, self.drain_log_queue)

    # ----- pipeline -----
    def collect_settings(self):
        return {
            "bitrate": self.bitrate_var.get(),
            "downmix_mode": self.downmix_mode_var.get(),
            "center_mix": self.center_mix_var.get(),
            "surround_mix": self.surround_mix_var.get(),
            "surround_mode": self.surround_mode_var.get(),
            "drc_line": self.drc_line_var.get(),
            "drc_rf": self.drc_rf_var.get(),
            "loudness_preset": self.loudness_preset_var.get(),
            "channel_map": self.channel_map_var.get(),
            "sample_rate": self.pcm_sample_rate_var.get(),
            "force_ffmpeg": self.force_ffmpeg_var.get(),
        }

    def start_encoding_thread(self):
        dee_path = resolve_exe(self.dee_path_var.get().strip())
        input_file = self.input_file_var.get().strip()
        output_file = self.output_file_var.get().strip()

        if not dee_path:
            messagebox.showerror("Error", f"DEE executable not found:\n{self.dee_path_var.get()}")
            return
        if not os.path.isfile(input_file):
            messagebox.showerror("Error", f"Input file not found:\n{input_file}")
            return
        if not output_file:
            messagebox.showerror("Error", "Please specify output path.")
            return
        if os.path.abspath(input_file).lower() == os.path.abspath(output_file).lower():
            messagebox.showerror("Error", "Output file must be different from input file.")
            return

        settings = self.collect_settings()
        needs_ffmpeg = settings["force_ffmpeg"] or detect_dee_input_type(input_file) is None
        ffmpeg_path = None
        if needs_ffmpeg:
            ffmpeg_path = resolve_exe(self.ffmpeg_path_var.get().strip())
            if not ffmpeg_path:
                messagebox.showerror(
                    "Error", f"FFmpeg is required for this input but was not found:\n{self.ffmpeg_path_var.get()}")
                return

        self.btn_convert.config(state="disabled")
        self.log_text.delete(1.0, tk.END)
        self.log("=== STARTING PROCESSING PIPELINE ===")

        threading.Thread(
            target=self.run_pipeline,
            args=(dee_path, ffmpeg_path, input_file, output_file, settings),
            daemon=True,
        ).start()

    def ffmpeg_to_ac3(self, ffmpeg_path, input_file, out_ac3, s):
        cmd = [ffmpeg_path, "-y", "-hide_banner", "-i", input_file,
               "-vn", "-sn", "-dn",
               "-af", CHANNEL_FILTERS[s["channel_map"]],
               "-ar", s["sample_rate"],
               "-c:a", "ac3", "-b:a", INTERMEDIATE_AC3_BITRATE,
               out_ac3]
        self.log("[+] Running FFmpeg pre-conversion (-> AC3 intermediate):")
        self.log("    " + subprocess.list2cmdline(cmd))
        rc = run_process(cmd, self.log, "    [FFmpeg] ")
        if rc != 0:
            raise RuntimeError(f"FFmpeg conversion failed with exit code {rc}")
        self.log("[+] FFmpeg conversion completed.\n")

    def run_pipeline(self, dee_path, ffmpeg_path, input_file, output_file, s):
        work_dir = None
        error_msg = None
        success = False
        try:
            out_dir = os.path.dirname(os.path.abspath(output_file))
            os.makedirs(out_dir, exist_ok=True)

            # โฟลเดอร์ชั่วคราวเฉพาะงานนี้ (อยู่ไดรฟ์เดียวกับ output) ลบทิ้งทั้งก้อนตอนจบ
            work_dir = tempfile.mkdtemp(prefix="dee_work_", dir=out_dir)

            dee_input = input_file
            if ffmpeg_path:
                dee_input = os.path.join(work_dir, "interm_input.ac3")
                self.ffmpeg_to_ac3(ffmpeg_path, input_file, dee_input, s)

            xml_path = os.path.join(work_dir, "dee_job.xml")
            self.log(f"[+] Generating Dolby Manifest XML: {xml_path}")
            build_manifest(dee_input, output_file, xml_path, work_dir, s)

            cmd = [dee_path, DEE_XML_FLAG, xml_path]
            self.log("[+] Executing DEE: " + subprocess.list2cmdline(cmd) + "\n")
            rc = run_process(cmd, self.log)

            if rc == 0:
                success = True
                self.log("\n[SUCCESS] Pipeline finished successfully!")
            else:
                error_msg = f"DEE failed with exit code {rc}."
                self.log(f"\n[ERROR] {error_msg}")

        except Exception as e:
            error_msg = str(e)
            self.log(f"\n[EXCEPTION] {error_msg}")
        finally:
            if work_dir and os.path.isdir(work_dir):
                shutil.rmtree(work_dir, ignore_errors=True)
                self.log("[+] Cleaned temporary files.")

            if success:
                self.root.after(0, lambda: messagebox.showinfo(
                    "Done", f"Encoding finished successfully!\n\nSaved to: {output_file}"))
            else:
                msg = error_msg or "Unknown error"
                self.root.after(0, lambda: messagebox.showerror("Error", msg))
            self.root.after(0, lambda: self.btn_convert.config(state="normal"))


if __name__ == "__main__":
    root = tk.Tk()
    app = DEEConverterGUI(root)
    root.mainloop()