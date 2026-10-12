import os
import re
import json
import glob
import shutil
import threading
import subprocess
from collections import defaultdict

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# ================= Configuration =================
QAAC_EXEC = "qaac64"        # หรือระบุ path เช่น r"C:\tools\qaac.exe"
MP4BOX_EXEC = "mp4box"    # หรือระบุ path เช่น r"C:\tools\mp4box.exe"
QAAC_QUALITY = "-V 109"   # True VBR

DEFAULT_ARTIST = "Digital Juice"
DEFAULT_ALBUM_ARTIST = "Digital Juice"

# --- ค่ากำหนด DSP ความดัง ---
DSP_CONFIG = {
    "compressor": {
        "enabled": True,
        "threshold": -12.0,  # dBFS
        "ratio": 2.0,        # 2:1
        "attack": 0.03,      # 30 ms
        "release": 0.20,     # 200 ms
        "makeup": 2.0,       # +2.0 dB
        "hp_cutoff": 60.0    # Hz
    },
    "limiter": {
        "enabled": True,
        "threshold": -0.5,   # dBFS
        "ceiling": -0.5,     # dBFS
        "release": 0.05      # 50 ms
    }
}

STEM_COLORS = [
    "#7088f0",  # 1. Cornflower Blue   (ปรับสว่างจาก Indigo ไม่กลืนกับพื้นดำ)
    "#3ba3ec",  # 2. Bright Azure       (ฟ้าสด สว่างปานกลาง)
    "#66ccee",  # 3. Ice Cyan           (ฟ้าอมเขียวสว่างสูง)
    "#44bb99",  # 4. Mint Green         (เขียวมิ้นต์สว่างปานกลาง)
    "#22cc88",  # 5. Vivid Teal         (เขียวสดอมฟ้า)
    "#f0e442",  # 6. Electric Lemon     (เหลืองสว่างสูงสุด เด่นชัดที่สุด)
    "#eedd88",  # 7. Light Amber-Sand   (เหลืองครีม สว่างนวล)
    "#f3a033",  # 8. Tangerine Gold     (ส้มทองสว่าง)
    "#ee6644",  # 9. Vivid Vermilion    (ส้มอิฐสด)
    "#d8557c",  # 10. Cranberry Rose    (ปรับสว่างจาก Wine Red)
    "#cc79a7",  # 11. Soft Orchid       (ชมพูมาเจนต้าตุ่น)
    "#aa44bb",  # 12. Bright Purple     (ม่วงสว่าง)
]

FILENAME_PATTERN = re.compile(
    r"^(?P<song>.+?)_(?P<version>[^_]+)_(?P<layer_no>\d+)_(?P<layer_name>.+?)\.wav$",
    re.IGNORECASE
)
# =================================================


def format_song_title(song_raw: str) -> str:
    """
    แปลงชื่อเพลง: แยก CamelCase และขีดล่าง เช่น 'CandyStickJam' หรือ 'Candy_Stick_Jam' -> 'Candy Stick Jam'
    """
    s = re.sub(r'(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])', ' ', song_raw)
    s = s.replace('_', ' ')
    return re.sub(r'\s+', ' ', s).strip()


def natural_sort_key(s: str):
    """จัดเรียงลำดับชื่อเพลง 1, 2, ..., 9, 10 ตามลำดับตัวเลขจริง"""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


class StemConverterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("NI Stem & Multi-track Creator (With Mastering DSP)")
        self.root.geometry("820x760")
        self.root.minsize(720, 600)

        self.is_running = False

        self._setup_style()
        self._build_ui()

    def _setup_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=5, font=("Segoe UI", 9))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
        style.configure("TLabelframe.Label", font=("Segoe UI", 9, "bold"))

    def _build_ui(self):
        # 1. เลือกโฟลเดอร์
        folder_frame = ttk.LabelFrame(self.root, text=" 1. เลือกโฟลเดอร์ต้นทาง (Source Directory) ", padding=10)
        folder_frame.pack(fill=tk.X, padx=12, pady=5)

        self.path_var = tk.StringVar()
        entry_path = ttk.Entry(folder_frame, textvariable=self.path_var)
        entry_path.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        btn_browse = ttk.Button(folder_frame, text="เลือกโฟลเดอร์...", command=self.browse_folder)
        btn_browse.pack(side=tk.RIGHT)

        # 2. Metadata (Album, Artist, Cover)
        meta_frame = ttk.LabelFrame(self.root, text=" 2. ข้อมูลแท็กเพลง (Album & Artist Metadata) ", padding=10)
        meta_frame.pack(fill=tk.X, padx=12, pady=5)

        meta_frame.columnconfigure(1, weight=1)

        # Album
        ttk.Label(meta_frame, text="ชื่ออัลบั้ม (Album):").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.album_var = tk.StringVar(value="")
        ttk.Entry(meta_frame, textvariable=self.album_var).grid(row=0, column=1, columnspan=2, sticky=tk.EW, pady=3)

        # Artist
        ttk.Label(meta_frame, text="ศิลปิน (Artist):").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.artist_var = tk.StringVar(value=DEFAULT_ARTIST)
        ttk.Entry(meta_frame, textvariable=self.artist_var).grid(row=1, column=1, columnspan=2, sticky=tk.EW, pady=3)

        # Album Artist
        ttk.Label(meta_frame, text="ศิลปินอัลบั้ม (Album Artist):").grid(row=2, column=0, sticky=tk.W, pady=3)
        self.album_artist_var = tk.StringVar(value=DEFAULT_ALBUM_ARTIST)
        ttk.Entry(meta_frame, textvariable=self.album_artist_var).grid(row=2, column=1, columnspan=2, sticky=tk.EW, pady=3)

        # Cover Art
        ttk.Label(meta_frame, text="รูปภาพปก (Cover Art):").grid(row=3, column=0, sticky=tk.W, pady=3)
        self.cover_var = tk.StringVar(value="")
        ttk.Entry(meta_frame, textvariable=self.cover_var).grid(row=3, column=1, sticky=tk.EW, pady=3, padx=(0, 6))
        btn_browse_cover = ttk.Button(meta_frame, text="เลือกรูป...", command=self.browse_cover)
        btn_browse_cover.grid(row=3, column=2, sticky=tk.E, pady=3)

        # 3. ตัวเลือกการประมวลผล
        opt_frame = ttk.LabelFrame(self.root, text=" 3. โหมดการสแกนและผลลัพธ์ ", padding=10)
        opt_frame.pack(fill=tk.X, padx=12, pady=5)

        self.scan_mode_var = tk.StringVar(value="batch")
        rb_batch = ttk.Radiobutton(
            opt_frame,
            text="หลายเพลง / สแกนโฟลเดอร์ย่อย (Batch: กวาดหาเพลงในทุก Subfolder อัตโนมัติ)",
            value="batch",
            variable=self.scan_mode_var
        )
        rb_single = ttk.Radiobutton(
            opt_frame,
            text="โฟลเดอร์เดียว (Single: สแกนเฉพาะไฟล์ในโฟลเดอร์นี้ตรงๆ เท่านั้น)",
            value="single",
            variable=self.scan_mode_var
        )
        rb_batch.pack(anchor=tk.W, pady=1)
        rb_single.pack(anchor=tk.W, pady=1)

        sep = ttk.Separator(opt_frame, orient=tk.HORIZONTAL)
        sep.pack(fill=tk.X, pady=6)

        self.make_mka_var = tk.BooleanVar(value=True)
        self.make_stem_var = tk.BooleanVar(value=True)

        chk_mka = ttk.Checkbutton(opt_frame, text="สร้างไฟล์ FLAC Multi-track (.mka)", variable=self.make_mka_var)
        chk_stem = ttk.Checkbutton(opt_frame, text="สร้างไฟล์ NI Stems (.stem.m4a) + Mastering DSP", variable=self.make_stem_var)
        chk_mka.pack(anchor=tk.W)
        chk_stem.pack(anchor=tk.W)

        # 4. ปุ่มสั่งการ
        action_frame = ttk.Frame(self.root, padding=4)
        action_frame.pack(fill=tk.X, padx=12)

        self.btn_start = ttk.Button(
            action_frame, 
            text="▶ เริ่มแปลงไฟล์ (Start Processing)", 
            style="Accent.TButton", 
            command=self.start_thread
        )
        self.btn_start.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        btn_clear = ttk.Button(action_frame, text="ล้าง Log", command=self.clear_log)
        btn_clear.pack(side=tk.RIGHT)

        # 5. Log Output
        log_frame = ttk.LabelFrame(self.root, text=" บันทึกการทำงาน (Log Output) ", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=5)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, bg="#1e1e1e", fg="#e0e0e0", 
            insertbackground="white", font=("Consolas", 9)
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # 6. Status bar
        self.status_var = tk.StringVar(value="พร้อมทำงาน")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=4)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    def log(self, text: str):
        def _append():
            self.log_text.insert(tk.END, text + "\n")
            self.log_text.see(tk.END)
        self.root.after(0, _append)

    def clear_log(self):
        self.log_text.delete("1.0", tk.END)

    def browse_folder(self):
        selected = filedialog.askdirectory(title="เลือกโฟลเดอร์ต้นทางที่มีไฟล์ WAV Stems")
        if selected:
            self.path_var.set(selected)
            auto_cover = os.path.join(selected, "cover.jpg")
            if os.path.isfile(auto_cover):
                self.cover_var.set(auto_cover)
            if not self.album_var.get().strip():
                self.album_var.set(os.path.basename(selected))

    def browse_cover(self):
        selected = filedialog.askopenfilename(
            title="เลือกภาพปก (Cover Art)",
            filetypes=[("Image Files", "*.jpg;*.jpeg;*.png")]
        )
        if selected:
            self.cover_var.set(selected)

    def check_executables(self) -> bool:
        missing = []
        for name, cmd in [("FFmpeg", "ffmpeg"), ("QAAC", QAAC_EXEC), ("MP4Box", MP4BOX_EXEC)]:
            if not shutil.which(cmd):
                missing.append(name)
        if missing:
            msg = f"ไม่พบโปรแกรมต่อไปนี้ใน PATH:\n- " + "\n- ".join(missing) + "\n\nโปรดตรวจสอบว่าได้ติดตั้งและเพิ่มใน System PATH แล้ว"
            messagebox.showerror("Error: Missing Dependencies", msg)
            return False
        return True

    def start_thread(self):
        if self.is_running:
            return

        source_dir = self.path_var.get().strip()
        if not source_dir or not os.path.isdir(source_dir):
            messagebox.showwarning("แจ้งเตือน", "กรุณาระบุโฟลเดอร์ต้นทางที่ถูกต้องก่อนเริ่มทำงาน")
            return

        if not self.make_mka_var.get() and not self.make_stem_var.get():
            messagebox.showwarning("แจ้งเตือน", "กรุณาเลือกรูปแบบผลลัพธ์อย่างน้อย 1 อย่าง (.mka หรือ .stem.m4a)")
            return

        if not self.check_executables():
            return

        self.is_running = True
        self.btn_start.config(state=tk.DISABLED)
        self.status_var.set("กำลังประมวลผล...")

        thread = threading.Thread(target=self.process_batches, args=(source_dir,), daemon=True)
        thread.start()

    def convert_to_qaac_float32(self, input_wav, output_m4a):
        qaac_cmd = [
            QAAC_EXEC, QAAC_QUALITY, "--rate", "44100", "--threading", input_wav, "-o", output_m4a
        ]
        p = subprocess.run(qaac_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if p.returncode != 0:
            self.log(f"[Error QAAC]: {p.stderr.decode(errors='ignore')}")

    def process_batches(self, source_dir):
        try:
            output_dir = os.path.join(source_dir, "_output")
            os.makedirs(output_dir, exist_ok=True)

            cover_file = self.cover_var.get().strip()
            if not cover_file or not os.path.isfile(cover_file):
                fallback_cover = os.path.join(source_dir, "cover.jpg")
                cover_file = fallback_cover if os.path.isfile(fallback_cover) else None

            if cover_file:
                self.log(f"[+] ตรวจพบภาพปก: {cover_file}")
            else:
                self.log("[-] ไม่พบภาพปก cover.jpg (จะข้ามการฝังรูป)")

            album_name = self.album_var.get().strip() or os.path.basename(source_dir)
            artist_name = self.artist_var.get().strip() or DEFAULT_ARTIST
            album_artist_name = self.album_artist_var.get().strip() or DEFAULT_ALBUM_ARTIST

            is_batch = (self.scan_mode_var.get() == "batch")
            self.log(f"[*] เริ่มสแกนไฟล์ WAV ({'Recursive Subfolders' if is_batch else 'Single Folder Only'})...")

            wav_files = []
            if is_batch:
                for root_path, dirs, files in os.walk(source_dir):
                    if "_output" in root_path.split(os.sep):
                        continue
                    for f in files:
                        if f.lower().endswith(".wav"):
                            wav_files.append(os.path.join(root_path, f))
            else:
                for f in os.listdir(source_dir):
                    f_path = os.path.join(source_dir, f)
                    if os.path.isfile(f_path) and f.lower().endswith(".wav"):
                        wav_files.append(f_path)

            groups = defaultdict(list)
            for file_path in wav_files:
                filename = os.path.basename(file_path)
                match = FILENAME_PATTERN.match(filename)
                if match:
                    song = match.group("song")
                    version = match.group("version")
                    layer_no = int(match.group("layer_no"))
                    layer_name = match.group("layer_name").strip().replace("_", " ")

                    group_key = f"{song}_{version}"
                    groups[group_key].append({
                        "path": file_path,
                        "song": song,
                        "version": version,
                        "layer_no": layer_no,
                        "name": layer_name
                    })

            if not groups:
                self.log("[-] ไม่พบไฟล์ WAV ที่ตรงตามรูปแบบ Pattern ในโฟลเดอร์นี้")
                return

            sorted_groups = sorted(groups.items(), key=lambda item: natural_sort_key(item[0]))
            total = len(sorted_groups)
            self.log(f"\n[+] สแกนพบเพลงทั้งหมด {total} ชุด (กำลังเรียงแทร็ก 1 ถึง {total})\n")

            for current_idx, (group_name, tracks) in enumerate(sorted_groups, start=1):
                raw_song_name = tracks[0]["song"]
                global_title = format_song_title(raw_song_name)

                self.status_var.set(f"กำลังประมวลผล ({current_idx}/{total}): {global_title}")
                tracks = sorted(tracks, key=lambda x: x["layer_no"])
                has_master = any(t["layer_no"] == 0 for t in tracks)
                auto_mixed_wav = None

                # -----------------------------------------------------------------
                # 0. Auto-Mixdown: ผสานเสียง + Glue Compression + True Peak Limiter
                # -----------------------------------------------------------------
                if not has_master:
                    comp = DSP_CONFIG["compressor"]
                    lim = DSP_CONFIG["limiter"]

                    comp_thresh_lin = round(10 ** (comp["threshold"] / 20.0), 4)
                    comp_makeup_lin = round(10 ** (comp["makeup"] / 20.0), 4)
                    comp_attack_ms = int(comp["attack"] * 1000)
                    comp_release_ms = int(comp["release"] * 1000)
                    lim_ceiling_lin = round(10 ** (lim["ceiling"] / 20.0), 4)
                    lim_release_ms = int(lim["release"] * 1000)

                    self.log(f"[*] [Track {current_idx}/{total}] ไม่พบ Track 0 -> Auto-mixdown Master...")
                    auto_mixed_wav = os.path.join(output_dir, f"temp_{group_name}_autofull.wav")
                    inputs = " ".join([f'-i "{t["path"]}"' for t in tracks])
                    input_tags = "".join([f"[{i}:a]" for i in range(len(tracks))])

                    filter_mix = (
                        f"{input_tags}amix=inputs={len(tracks)}:normalize=0,"
                        f"acompressor=threshold={comp_thresh_lin}:ratio={comp['ratio']}:"
                        f"attack={comp_attack_ms}:release={comp_release_ms}:makeup={comp_makeup_lin},"
                        f"alimiter=limit={lim_ceiling_lin}:attack=5:release={lim_release_ms}:asc=0[out]"
                    )

                    cmd_mix = (
                        f'ffmpeg -y {inputs} -filter_complex "{filter_mix}" '
                        f'-map "[out]" -c:a pcm_f32le "{auto_mixed_wav}"'
                    )
                    subprocess.run(cmd_mix, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    tracks.insert(0, {
                        "path": auto_mixed_wav,
                        "song": raw_song_name,
                        "version": tracks[0]["version"],
                        "layer_no": 0,
                        "name": "Master"
                    })

                # คำนวณจำนวนแทร็ก และสร้างข้อความ Description บอก Patch ของแต่ละ Layer
                num_tracks = len(tracks)
                track_patches = [f"Trk {t['layer_no']} ({t['name']})" for t in tracks]
                description_text = f"Total {num_tracks} Tracks - " + " | ".join(track_patches)

                self.log("-" * 55)
                self.log(f"แทร็ก {current_idx}/{total}: {global_title} (ไฟล์: {group_name})")
                self.log(f"จำนวน Stream: {num_tracks} แทร็ก")
                self.log(f"คำอธิบาย Patch: {description_text}")
                self.log("-" * 55)

                # -----------------------------------------------------------------
                # 1. FLAC Multi-track (.mka)
                # -----------------------------------------------------------------
                if self.make_mka_var.get():
                    mka_filename = f"{group_name}_{num_tracks}trk.mka"
                    mka_output = os.path.join(output_dir, mka_filename)
                    ffmpeg_inputs = " ".join([f'-i "{t["path"]}"' for t in tracks])
                    ffmpeg_maps = " ".join([f"-map [a{i}]" for i in range(len(tracks))])
                    
                    ffmpeg_global_meta = (
                        f'-metadata title="{global_title}" '
                        f'-metadata artist="{artist_name}" '
                        f'-metadata album="{album_name}" '
                        f'-metadata album_artist="{album_artist_name}" '
                        f'-metadata track="{current_idx}/{total}" '
                        f'-metadata description="{description_text}" '
                        f'-metadata comment="{description_text}"'
                    )
                    ffmpeg_track_meta = " ".join([
                        f'-metadata:s:a:{i} title="{tracks[i]["name"]}" '
                        f'-metadata:s:a:{i} handler_name="{tracks[i]["name"]}"'
                        for i in range(len(tracks))
                    ])

                    filter_chains = []
                    for i in range(len(tracks)):
                        filter_chains.append(
                            f"[{i}:a]aresample=44100:dither_method=none,aformat=sample_fmts=s16[a{i}]"
                        )
                    complex_filter = ";".join(filter_chains)

                    attach_cmd = ""
                    if cover_file and os.path.isfile(cover_file):
                        attach_cmd = (
                            f'-attach "{cover_file}" '
                            f'-metadata:s:t:0 filename="cover.jpg" '
                            f'-metadata:s:t:0 mimetype="image/jpeg" '
                            f'-metadata:s:t:0 description="Front Cover" '
                        )

                    cmd_mka = (
                        f'ffmpeg -y {ffmpeg_inputs} -filter_complex "{complex_filter}" {ffmpeg_maps} '
                        f'-map_metadata -1 {ffmpeg_global_meta} {attach_cmd}'
                        f'-c:a flac -compression_level 8 {ffmpeg_track_meta} "{mka_output}"'
                    )
                    self.log(f"[*] [{group_name}] กำลังสร้าง {mka_filename}...")
                    result = subprocess.run(cmd_mka, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    if result.returncode != 0:
                        self.log(f"[Error FFmpeg MKA]: {result.stderr.decode(errors='ignore')}")

                # -----------------------------------------------------------------
                # 2. NI Stem (.stem.m4a) + FastStart + Tag Metadata
                # -----------------------------------------------------------------
                if self.make_stem_var.get():
                    stem_filename = f"{group_name}_{num_tracks}trk.stem.m4a"
                    self.log(f"[*] [{group_name}] แปลง Stems ด้วย QAAC (44.1kHz Float32)...")
                    temp_m4a_files = []
                    for i, t in enumerate(tracks):
                        temp_m4a = os.path.join(output_dir, f"temp_{group_name}_tr{i}.m4a")
                        temp_m4a_files.append(temp_m4a)
                        self.convert_to_qaac_float32(t["path"], temp_m4a)

                    # สร้าง JSON Metadata
                    stem_json_path = os.path.join(output_dir, f"temp_{group_name}_meta.json")
                    stems_list = []
                    for idx, t in enumerate(tracks[1:]):
                        color = STEM_COLORS[idx % len(STEM_COLORS)]
                        stems_list.append({
                            "color": color,
                            "name": t["name"],
                            "label": f"Stem {idx+1}: {t['name']}"
                        })

                    stem_metadata = {
                        "version": 1,
                        "title": global_title,
                        "artist": artist_name,
                        "album": album_name,
                        "track": current_idx,
                        "total_tracks": num_tracks,
                        "description": description_text,
                        "stems": stems_list,
                        "mastering_dsp": {
                            "compressor": DSP_CONFIG["compressor"],
                            "limiter": DSP_CONFIG["limiter"]
                        }
                    }

                    with open(stem_json_path, "w", encoding="utf-8") as f:
                        json.dump(stem_metadata, f, ensure_ascii=False, indent=2)

                    # เตรียม Tag สำหรับ MP4Box (ใช้ - แทน : ภายในข้อความเพื่อไม่ให้ syntax พัง)
                    itags_elements = [
                        f'name="{global_title}"',
                        f'artist="{artist_name}"',
                        f'album="{album_name}"',
                        f'album_artist="{album_artist_name}"',
                        f'track={current_idx}/{total}',
                        f'description="{description_text}"',
                        f'comment="{description_text}"'
                    ]

                    temp_cover_in_out = None
                    if cover_file:
                        temp_cover_in_out = os.path.join(output_dir, f"temp_{group_name}_cov.jpg")
                        shutil.copy2(cover_file, temp_cover_in_out)
                        itags_elements.append(f'cover="{temp_cover_in_out}"')

                    itags_str = ":".join(itags_elements)

                    stem_output = os.path.join(output_dir, stem_filename)
                    mp4box_args = [
                        MP4BOX_EXEC,
                        "-inter", "500",       # FastStart
                        "-brand", "isom",
                        "-itags", itags_str,   # Global Tags + Cover + Description
                        "-add", f'"{temp_m4a_files[0]}#audio:lang=qaa:name=Master (Full Mix)"'
                    ]

                    for i, m4a_path in enumerate(temp_m4a_files[1:], start=1):
                        label_name = f"{tracks[i]['name']} (Stem {i})"
                        stem_lang = f"qt{chr(ord('a') + (i - 1))}"
                        
                        mp4box_args.extend([
                            "-add", f'"{m4a_path}#audio:lang={stem_lang}:name={label_name}:disabled"'
                        ])
                
                    temp_txt_files = []
                    for i, t in enumerate(tracks, start=1):
                        track_title = "Master (Full Mix)" if i == 1 else f"{t['name']} (Stem {i-1})"
                        txt_path = os.path.join(output_dir, f"temp_{group_name}_name_{i}.txt")
                        with open(txt_path, "w", encoding="utf-8") as tf:
                            tf.write(track_title)
                        temp_txt_files.append(txt_path)
                        mp4box_args.extend(["-udta", f'{i}:type=name:src="{txt_path}"'])
                    
                    mp4box_args.extend([
                        "-udta", f'0:type=stem:src="{stem_json_path}"',
                        "-new", f'"{stem_output}"'
                    ])

                    self.log(f"[*] [{group_name}] กำลัง Mux {stem_filename} ด้วย MP4Box...")
                    cmd_mp4box = " ".join(mp4box_args)
                    subprocess.run(cmd_mp4box, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                    # ลบไฟล์ชั่วคราว
                    for f in temp_m4a_files:
                        if os.path.exists(f):
                            os.remove(f)
                    for f in temp_txt_files:
                        if os.path.exists(f):
                            os.remove(f)
                    if os.path.exists(stem_json_path):
                        os.remove(stem_json_path)
                    if temp_cover_in_out and os.path.exists(temp_cover_in_out):
                        os.remove(temp_cover_in_out)

                if auto_mixed_wav and os.path.exists(auto_mixed_wav):
                    os.remove(auto_mixed_wav)

                self.log(f"[OK] สำเร็จ: {global_title} (Track {current_idx}/{total})\n")

            self.log(f"\n🎉 [SUCCESS] ดำเนินการเสร็จสิ้นทั้งหมด {total} เพลง!")
            self.log(f"📁 โฟลเดอร์ผลลัพธ์: {output_dir}")
            messagebox.showinfo("เสร็จสิ้น", f"แปลงไฟล์เสร็จเรียบร้อยทั้งหมด {total} เพลง!\nผลลัพธ์อยู่ที่: {output_dir}")

        except Exception as e:
            self.log(f"\n[FATAL ERROR]: {str(e)}")
            messagebox.showerror("เกิดข้อผิดพลาด", str(e))
        finally:
            self.is_running = False
            self.root.after(0, lambda: self.btn_start.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.status_var.set("พร้อมทำงาน"))


def main():
    root = tk.Tk()
    app = StemConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()