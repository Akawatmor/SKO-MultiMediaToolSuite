# Version 2.6 - MKV/MP4 Multi-Audio & H.265 NVENC Video Batch Encoder
import json
import os
import re
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


class DualAudioVideoEncoderGUI:

  def __init__(self, root):
    self.root = root
    self.root.title(
        "MKV/MP4 Multi-Audio & H.265 NVENC Encoder (+ MP4Box FastStart &"
        " MediaInfo DPL II)"
    )
    self.root.geometry("840x1020")
    self.root.minsize(780, 920)

    pad_opts = {"padx": 10, "pady": 3}

    # -------------------------------------------------------------
    # 1. File / Folder Selection Frame
    # -------------------------------------------------------------
    file_frame = ttk.LabelFrame(
        root, text="1. เลือกไฟล์หรือโฟลเดอร์ (Input Video / Folder)"
    )
    file_frame.pack(fill="x", **pad_opts)

    self.scan_mode = tk.StringVar(value="file")
    mode_bar = ttk.Frame(file_frame)
    mode_bar.pack(fill="x", padx=10, pady=(4, 2))
    ttk.Radiobutton(
        mode_bar,
        text="ไฟล์เดี่ยว (Single File)",
        variable=self.scan_mode,
        value="file",
    ).pack(side="left", padx=(0, 15))
    ttk.Radiobutton(
        mode_bar,
        text="สแกนทั้งโฟลเดอร์ (Batch Folder - .mkv, .mp4)",
        variable=self.scan_mode,
        value="folder",
    ).pack(side="left")

    input_bar = ttk.Frame(file_frame)
    input_bar.pack(fill="x", padx=10, pady=(2, 6))

    self.entry_path = ttk.Entry(input_bar, width=58)
    self.entry_path.pack(side="left", fill="x", expand=True, padx=(0, 8))

    self.btn_browse_file = ttk.Button(
        input_bar, text="Browse File...", command=self.browse_file
    )
    self.btn_browse_file.pack(side="left", padx=2)

    self.btn_browse_folder = ttk.Button(
        input_bar, text="Browse Folder...", command=self.browse_folder
    )
    self.btn_browse_folder.pack(side="left", padx=2)

    # -------------------------------------------------------------
    # 2. Subtitle & Audio Priority / Fallback Options
    # -------------------------------------------------------------
    pref_frame = ttk.LabelFrame(
        root, text="2. จัดการภาษา, ซับไตเติล และ Fallback ค่าเริ่มต้น"
    )
    pref_frame.pack(fill="x", **pad_opts)

    sub_row = ttk.Frame(pref_frame)
    sub_row.pack(fill="x", padx=10, pady=2)
    self.filter_sub_th_en = tk.BooleanVar(value=True)
    ttk.Checkbutton(
        sub_row,
        text="กรอง Subtitle: ตัดภาษาอื่นทิ้งทั้งหมด ให้เหลือเฉพาะ Thai และ"
        " English",
        variable=self.filter_sub_th_en,
    ).pack(side="left")

    prio_row = ttk.Frame(pref_frame)
    prio_row.pack(fill="x", padx=10, pady=2)
    ttk.Label(prio_row, text="เลือกเสียงเริ่มต้น (Default Audio Track):").pack(
        side="left"
    )
    self.default_audio_pref = tk.StringVar(value="eng")
    ttk.Radiobutton(
        prio_row,
        text="English (เสียงอังกฤษ)",
        variable=self.default_audio_pref,
        value="eng",
    ).pack(side="left", padx=(8, 12))
    ttk.Radiobutton(
        prio_row,
        text="Thai (เสียงไทย)",
        variable=self.default_audio_pref,
        value="tha",
    ).pack(side="left")

    fallback_lf = ttk.LabelFrame(
        pref_frame,
        text="การตั้งค่ากรณีไฟล์ไม่มีข้อมูลภาษา (Unknown Language Fallback):",
    )
    fallback_lf.pack(fill="x", padx=8, pady=3)

    fb_grid = ttk.Frame(fallback_lf)
    fb_grid.pack(fill="x", padx=8, pady=2)

    ttk.Label(fb_grid, text="Track 1 -> Code:").grid(
        row=0, column=0, sticky="w"
    )
    self.fb_tr1_lang = tk.StringVar(value="eng")
    self.cb_fb_tr1 = ttk.Combobox(
        fb_grid,
        textvariable=self.fb_tr1_lang,
        values=["eng", "tha", "und"],
        width=5,
        state="readonly",
    )
    self.cb_fb_tr1.grid(row=0, column=1, padx=4, pady=2)
    ttk.Label(fb_grid, text="Title:").grid(
        row=0, column=2, sticky="w", padx=(6, 2)
    )
    self.fb_tr1_title = ttk.Entry(fb_grid, width=18)
    self.fb_tr1_title.insert(0, "English Audio")
    self.fb_tr1_title.grid(row=0, column=3, padx=4, pady=2)

    ttk.Label(fb_grid, text="Track 2 -> Code:").grid(
        row=0, column=4, sticky="w", padx=(12, 2)
    )
    self.fb_tr2_lang = tk.StringVar(value="tha")
    self.cb_fb_tr2 = ttk.Combobox(
        fb_grid,
        textvariable=self.fb_tr2_lang,
        values=["tha", "eng", "und"],
        width=5,
        state="readonly",
    )
    self.cb_fb_tr2.grid(row=0, column=5, padx=4, pady=2)
    ttk.Label(fb_grid, text="Title:").grid(
        row=0, column=6, sticky="w", padx=(6, 2)
    )
    self.fb_tr2_title = ttk.Entry(fb_grid, width=18)
    self.fb_tr2_title.insert(0, "Thai Audio")
    self.fb_tr2_title.grid(row=0, column=7, padx=4, pady=2)

    # -------------------------------------------------------------
    # 3. Video Encoding (H.265 NVENC) Frame + Independent Deinterlace
    # -------------------------------------------------------------
    video_frame = ttk.LabelFrame(
        root, text="3. ตั้งค่าเข้ารหัสวิดีโอ (H.265 NVENC - 8-Bit Main Profile)"
    )
    video_frame.pack(fill="x", **pad_opts)

    # 3.1 NVENC Tuned Profiles (พารามิเตอร์คงเดิม 100% ห้ามแก้ไข)
    self.nvenc_presets = {
        "Animate 24fps (Modern Anime)": {
            "desc": "สำหรับอนิเมะ 24fps คุณภาพสำหรับ Streaming (GOP 360)",
            "params": (
                "rc=vbr:cq=32:qmin=30:qmax=51:rc-lookahead=32:surfaces=32:multipass=fullres:gop=360:bf=5:b_ref_mode=each:spatial-aq=1:aq-strength=8:temporal-aq=1:ref=5:weighted_pred=0:b_adapt=1:nonref_p=0:strict_gop=0:tune=hq"
            ),
        },
        "90s MV Tape (มี Noise / Grain)": {
            "desc": (
                "ม้วนเทปยุค 90 มี Noise (คำนวณ GOP ตาม FPS อัตโนมัติ"
                " ปรับเลือก Deinterlace แยกได้อิสระ)"
            ),
            "params": (
                "rc=vbr:cq=32:qmin=30:qmax=51:rc-lookahead=32:surfaces=32:multipass=fullres:gop=375:bf=5:b_ref_mode=each:spatial-aq=1:aq-strength=4:temporal-aq=0:ref=5:weighted_pred=0:b_adapt=1:nonref_p=0:strict_gop=0:tune=hq"
            ),
        },
        "90s MV Tape (ไม่มี Noise / Clean)": {
            "desc": (
                "ม้วนเทปยุค 90 ไม่มี Noise (คำนวณ GOP ตาม FPS อัตโนมัติ"
                " ปรับเลือก Deinterlace แยกได้อิสระ)"
            ),
            "params": (
                "rc=vbr:cq=32:qmin=30:qmax=51:rc-lookahead=32:surfaces=32:multipass=fullres:tier=high:gop=375:strict_gop=0:bf=5:b_ref_mode=each:b_adapt=1:nonref_p=0:ref=5:weighted_pred=0:spatial-aq=1:aq-strength=6:temporal-aq=1:tune=hq"
            ),
        },
        "Movie / Film 24fps (หนังฟิล์ม / เกรนธรรมชาติ)": {
            "desc": (
                "หนัง 24fps ปรับลด Spatial AQ=5 และปิด Temporal AQ เพื่อรักษาสภาพเกรน"
            ),
            "params": (
                "rc=vbr:cq=32:qmin=30:qmax=51:rc-lookahead=32:surfaces=32:multipass=fullres:gop=360:bf=5:b_ref_mode=each:spatial-aq=1:aq-strength=5:temporal-aq=0:ref=5:weighted_pred=0:b_adapt=1:nonref_p=0:strict_gop=0:tune=hq"
            ),
        },
        "Classic Anime 24fps (อนิเมะคลาสสิก / วาดมือ มีเกรน)": {
            "desc": (
                "อนิเมะวาดมือ 24fps ผสมผสาน Spatial AQ=6 + Temporal AQ"
                " ช่วยเกลี่ยสี"
            ),
            "params": (
                "rc=vbr:cq=32:qmin=30:qmax=51:rc-lookahead=32:surfaces=32:multipass=fullres:gop=360:bf=5:b_ref_mode=each:spatial-aq=1:aq-strength=6:temporal-aq=1:ref=5:weighted_pred=0:b_adapt=1:nonref_p=0:strict_gop=0:tune=hq"
            ),
        },
        "Copy Video Stream (ไม่แปลงวิดีโอ - Passthrough เดิม)": {
            "desc": "Copy สตรีมวิดีโอเดิมโดยตรง ไม่ผ่านการเข้ารหัสซ้ำ",
            "params": "",
        },
    }

    row_v = ttk.Frame(video_frame)
    row_v.pack(fill="x", padx=10, pady=3)
    ttk.Label(row_v, text="Video Preset:").pack(side="left")

    self.selected_preset_name = tk.StringVar(
        value="Animate 24fps (Modern Anime)"
    )
    self.cb_v_preset = ttk.Combobox(
        row_v,
        textvariable=self.selected_preset_name,
        values=list(self.nvenc_presets.keys()),
        width=48,
        state="readonly",
    )
    self.cb_v_preset.pack(side="left", padx=8)
    self.cb_v_preset.bind("<<ComboboxSelected>>", self.on_preset_change)

    self.lbl_v_desc = ttk.Label(
        video_frame,
        text=self.nvenc_presets[self.selected_preset_name.get()]["desc"],
        foreground="#555",
    )
    self.lbl_v_desc.pack(anchor="w", padx=12, pady=(0, 4))

    # 3.2 แยก Deinterlace Controls ออกมาเป็นสัดส่วน
    deint_frame = ttk.LabelFrame(
        video_frame, text="ตั้งค่า Deinterlace (ถอดเส้นสลับ 2x Field Rate)"
    )
    deint_frame.pack(fill="x", padx=10, pady=(2, 6))

    deint_top = ttk.Frame(deint_frame)
    deint_top.pack(fill="x", padx=8, pady=2)

    self.enable_deint = tk.BooleanVar(value=False)
    self.cb_enable_deint = ttk.Checkbutton(
        deint_top,
        text="เปิดใช้งาน Deinterlace (ติ๊กออกหากวิดีโอเป็น Progressive หรือถอดเส้นมาแล้ว)",
        variable=self.enable_deint,
        command=self.toggle_deint_controls,
    )
    self.cb_enable_deint.pack(side="left")

    deint_opts = ttk.Frame(deint_frame)
    deint_opts.pack(fill="x", padx=15, pady=(2, 4))

    ttk.Label(deint_opts, text="Algorithm:").pack(side="left")
    self.deint_algo = tk.StringVar(value="bwdif")
    self.cb_deint_algo = ttk.Combobox(
        deint_opts,
        textvariable=self.deint_algo,
        values=[
            "bwdif (BWDIF CUDA - Bob Weaver คุณภาพสูง)",
            "yadif (YADIF CUDA - คลาสสิก)",
        ],
        width=36,
        state="readonly",
    )
    self.cb_deint_algo.pack(side="left", padx=(4, 15))

    ttk.Label(deint_opts, text="Field Order (Parity):").pack(side="left")
    self.deint_parity = tk.StringVar(value="auto")
    self.cb_deint_parity = ttk.Combobox(
        deint_opts,
        textvariable=self.deint_parity,
        values=[
            "auto (ตรวจจับอัตโนมัติ)",
            "tff (Top Field First)",
            "bff (Bottom Field First)",
        ],
        width=25,
        state="readonly",
    )
    self.cb_deint_parity.pack(side="left", padx=4)

    # -------------------------------------------------------------
    # 4. Audio Processing Master Frame
    # -------------------------------------------------------------
    audio_opt_frame = ttk.LabelFrame(
        root, text="4. ตัวเลือกการแปลงสัญญาณเสียง (Audio Encoding Options)"
    )
    audio_opt_frame.pack(fill="x", **pad_opts)

    self.enable_aac = tk.BooleanVar(value=True)
    aac_lf = ttk.LabelFrame(audio_opt_frame)
    aac_lf.pack(fill="x", padx=8, pady=3)

    cb_enable_aac = ttk.Checkbutton(
        aac_lf,
        text="แปลงเป็น Apple AAC 2.0 (ผ่าน QAAC)",
        variable=self.enable_aac,
        command=self.toggle_aac_controls,
    )
    cb_enable_aac.pack(anchor="w", padx=8, pady=(3, 2))

    self.aac_frame = ttk.Frame(aac_lf)
    self.aac_frame.pack(fill="x", padx=15, pady=(0, 4))

    ttk.Label(self.aac_frame, text="Mode:").pack(side="left")
    self.aac_mode = tk.StringVar(value="tvbr")
    self.cb_aac_mode = ttk.Combobox(
        self.aac_frame,
        textvariable=self.aac_mode,
        values=["tvbr", "cvbr", "abr"],
        width=8,
        state="readonly",
    )
    self.cb_aac_mode.pack(side="left", padx=(4, 15))

    ttk.Label(self.aac_frame, text="Bitrate / Target:").pack(side="left")
    self.aac_bitrate_var = tk.StringVar(value="192")
    self.cb_aac_bitrate = ttk.Combobox(
        self.aac_frame,
        textvariable=self.aac_bitrate_var,
        values=["96", "112", "128", "160", "192", "224", "256", "288", "320"],
        width=8,
        state="readonly",
    )
    self.cb_aac_bitrate.pack(side="left", padx=4)
    self.lbl_aac_note = ttk.Label(
        self.aac_frame, text="Kbps (Default TVBR ~192k)"
    )
    self.lbl_aac_note.pack(side="left", padx=6)

    self.enable_opus = tk.BooleanVar(value=False)
    opus_lf = ttk.LabelFrame(audio_opt_frame)
    opus_lf.pack(fill="x", padx=8, pady=3)

    cb_enable_opus = ttk.Checkbutton(
        opus_lf,
        text="แปลงเป็น Opus (ผ่าน FFmpeg libopus)",
        variable=self.enable_opus,
        command=self.toggle_opus_controls,
    )
    cb_enable_opus.pack(anchor="w", padx=8, pady=(3, 2))

    self.opus_frame = ttk.Frame(opus_lf)
    self.opus_frame.pack(fill="x", padx=15, pady=(0, 4))

    ttk.Label(self.opus_frame, text="Mode:").grid(
        row=0, column=0, sticky="w", pady=2
    )
    self.opus_mode = tk.StringVar(value="vbr")
    self.cb_opus_mode = ttk.Combobox(
        self.opus_frame,
        textvariable=self.opus_mode,
        values=["vbr", "abr"],
        width=8,
        state="readonly",
    )
    self.cb_opus_mode.grid(row=0, column=1, sticky="w", padx=4, pady=2)

    ttk.Label(self.opus_frame, text="Bitrate:").grid(
        row=0, column=2, sticky="w", padx=(12, 0), pady=2
    )
    self.opus_bitrate_var = tk.StringVar(value="128")
    self.cb_opus_bitrate = ttk.Combobox(
        self.opus_frame,
        textvariable=self.opus_bitrate_var,
        values=[
            "64",
            "96",
            "128",
            "160",
            "192",
            "224",
            "256",
            "320",
            "384",
            "448",
            "512",
        ],
        width=8,
        state="readonly",
    )
    self.cb_opus_bitrate.grid(row=0, column=3, sticky="w", padx=4, pady=2)
    ttk.Label(self.opus_frame, text="Kbps").grid(
        row=0, column=4, sticky="w", padx=2, pady=2
    )

    ttk.Label(self.opus_frame, text="Channels:").grid(
        row=1, column=0, sticky="w", pady=3
    )
    self.opus_channels_mode = tk.StringVar(value="downmix")
    self.rb_opus_dm = ttk.Radiobutton(
        self.opus_frame,
        text="Mixdown 2.0 (ใช้สูตรด้านล่าง)",
        variable=self.opus_channels_mode,
        value="downmix",
    )
    self.rb_opus_dm.grid(
        row=1, column=1, columnspan=2, sticky="w", padx=4, pady=2
    )

    self.rb_opus_pt = ttk.Radiobutton(
        self.opus_frame,
        text="Passthrough Channels (คง 7.1/5.1 แชนแนลเดิม)",
        variable=self.opus_channels_mode,
        value="passthrough",
    )
    self.rb_opus_pt.grid(
        row=1, column=3, columnspan=2, sticky="w", padx=4, pady=2
    )

    downmix_lf = ttk.LabelFrame(
        audio_opt_frame,
        text="สูตร Downmix 2.0 (รองรับ 7.1, 7.0, 5.1, 5.0, 4.1, 4.0)",
    )
    downmix_lf.pack(fill="x", padx=8, pady=3)

    self.downmix_mode = tk.StringVar(value="dplii")
    ttk.Radiobutton(
        downmix_lf,
        text="Dolby Pro Logic II (Matrix Lt/Rt - ถอดมิติรอบทิศทาง 5.1/7.1 ได้)",
        variable=self.downmix_mode,
        value="dplii",
    ).pack(anchor="w", padx=10, pady=2)
    ttk.Radiobutton(
        downmix_lf,
        text="Standard Stereo (Lo/Ro - สเตอริโอบาลานซ์มาตรฐาน ITU-R)",
        variable=self.downmix_mode,
        value="standard",
    ).pack(anchor="w", padx=10, pady=2)

    # -------------------------------------------------------------
    # 5. Output Container, MP4Box FastStart & Save Mode
    # -------------------------------------------------------------
    opt_frame = ttk.LabelFrame(
        root, text="5. รูปแบบคอนเทนเนอร์, MP4Box FastStart และโหมดบันทึกไฟล์"
    )
    opt_frame.pack(fill="x", **pad_opts)

    # 5.1 Container Selection
    c_row = ttk.Frame(opt_frame)
    c_row.pack(fill="x", padx=10, pady=(3, 2))
    ttk.Label(c_row, text="คอนเทนเนอร์ปลายทาง:").pack(side="left")
    self.output_container = tk.StringVar(value="mp4")
    ttk.Radiobutton(
        c_row,
        text="MP4 (.mp4 - สำหรับ Web Streaming)",
        variable=self.output_container,
        value="mp4",
        command=self.on_container_change,
    ).pack(side="left", padx=(10, 15))
    ttk.Radiobutton(
        c_row,
        text="MKV (.mkv - สำหรับไฟล์ Master / ซับไตเติลครบ)",
        variable=self.output_container,
        value="mkv",
        command=self.on_container_change,
    ).pack(side="left")

    # 5.2 FastStart via MP4Box
    fs_row = ttk.Frame(opt_frame)
    fs_row.pack(fill="x", padx=10, pady=2)
    self.enable_faststart = tk.BooleanVar(value=True)
    self.cb_faststart = ttk.Checkbutton(
        fs_row,
        text=(
            "ทำ HTTP FastStart ผ่าน MP4Box (-inter 500) (จัด chunk 500ms และย้าย"
            " moov atom ไปต้นไฟล์สำหรับ MP4)"
        ),
        variable=self.enable_faststart,
    )
    self.cb_faststart.pack(side="left")

    # 5.3 Save Mode
    sm_row = ttk.Frame(opt_frame)
    sm_row.pack(fill="x", padx=10, pady=(3, 4))
    ttk.Label(sm_row, text="การบันทึกไฟล์:").pack(side="left")
    self.save_mode = tk.StringVar(value="new_file")
    ttk.Radiobutton(
        sm_row,
        text="สร้างเป็นไฟล์ใหม่ (_multi_audio)",
        variable=self.save_mode,
        value="new_file",
    ).pack(side="left", padx=(10, 15))
    ttk.Radiobutton(
        sm_row,
        text="แทนที่ไฟล์เดิม (Replace Original - ปลอดภัยด้วย Safe Swap)",
        variable=self.save_mode,
        value="replace",
    ).pack(side="left")

    # -------------------------------------------------------------
    # 6. Action & Log Frame
    # -------------------------------------------------------------
    self.btn_run = ttk.Button(
        root,
        text="เริ่มประมวลผลและแปลงไฟล์ (Start Process)",
        command=self.start_thread,
    )
    self.btn_run.pack(fill="x", padx=10, pady=5)

    log_frame = ttk.LabelFrame(root, text="สถานะการทำงาน (Log Output)")
    log_frame.pack(fill="both", expand=True, **pad_opts)

    self.log_text = tk.Text(
        log_frame, wrap="word", height=8, state="disabled", bg="#f8f9fa"
    )
    self.log_text.pack(fill="both", expand=True, padx=5, pady=5)

    self.toggle_aac_controls()
    self.toggle_opus_controls()
    self.toggle_deint_controls()
    self.on_container_change()

  def on_container_change(self):
    if self.output_container.get() == "mkv":
      self.cb_faststart.config(state="disabled")
    else:
      self.cb_faststart.config(state="normal")

  def on_preset_change(self, event=None):
    selected = self.selected_preset_name.get()
    desc = self.nvenc_presets.get(selected, {}).get("desc", "")
    self.lbl_v_desc.config(text=desc)

    # กรณีเลือก Copy Video ให้ปิดการทำงานของ Deinterlace เพื่อป้องกันคำสั่งขัดแย้ง
    if "Copy Video" in selected:
      self.cb_enable_deint.config(state="disabled")
      self.cb_deint_algo.config(state="disabled")
      self.cb_deint_parity.config(state="disabled")
    else:
      self.cb_enable_deint.config(state="normal")
      self.toggle_deint_controls()

  def toggle_deint_controls(self):
    state = "readonly" if self.enable_deint.get() else "disabled"
    self.cb_deint_algo.config(state=state)
    self.cb_deint_parity.config(state=state)

  def toggle_aac_controls(self):
    state = "normal" if self.enable_aac.get() else "disabled"
    self.cb_aac_mode.config(
        state="readonly" if state == "normal" else "disabled"
    )
    self.cb_aac_bitrate.config(
        state="readonly" if state == "normal" else "disabled"
    )

  def toggle_opus_controls(self):
    state = "normal" if self.enable_opus.get() else "disabled"
    self.cb_opus_mode.config(
        state="readonly" if state == "normal" else "disabled"
    )
    self.cb_opus_bitrate.config(
        state="readonly" if state == "normal" else "disabled"
    )
    self.rb_opus_dm.config(state=state)
    self.rb_opus_pt.config(state=state)

  def log(self, message):
    self.log_text.config(state="normal")
    self.log_text.insert("end", message + "\n")
    self.log_text.see("end")
    self.log_text.config(state="disabled")

  def browse_file(self):
    filename = filedialog.askopenfilename(
        filetypes=[
            ("Video Files", "*.mkv *.mp4"),
            ("MKV Video", "*.mkv"),
            ("MP4 Video", "*.mp4"),
            ("All Files", "*.*"),
        ]
    )
    if filename:
      self.scan_mode.set("file")
      self.entry_path.delete(0, tk.END)
      self.entry_path.insert(0, filename)

  def browse_folder(self):
    folder = filedialog.askdirectory()
    if folder:
      self.scan_mode.set("folder")
      self.entry_path.delete(0, tk.END)
      self.entry_path.insert(0, folder)

  def start_thread(self):
    input_path = self.entry_path.get().strip()
    if not input_path or not os.path.exists(input_path):
      messagebox.showerror(
          "Error", "กรุณาเลือกไฟล์หรือโฟลเดอร์ปลายทางที่มีอยู่จริง"
      )
      return

    if not self.enable_aac.get() and not self.enable_opus.get():
      messagebox.showerror(
          "Error", "กรุณาเลือกรูปแบบเสียงอย่างน้อย 1 อย่าง (AAC หรือ Opus)"
      )
      return

    self.btn_run.config(state="disabled")
    threading.Thread(
        target=self.batch_manager, args=(input_path,), daemon=True
    ).start()

  # -------------------------------------------------------------
  # Video & FastStart Utilities
  # -------------------------------------------------------------
  def run_mp4box_faststart(self, file_path):
    self.log(
        "📦 [MP4Box] กำลังจัดเรียง Interleaving 500ms & ย้าย moov atom"
        " (FastStart)..."
    )
    commands = [
        f'mp4box -inter 500 "{file_path}"',
        f'MP4Box -inter 500 "{file_path}"',
    ]

    success = False
    last_err = ""
    for cmd in commands:
      try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            shell=True,
            encoding="utf-8",
            errors="ignore",
        )
        if res.returncode == 0:
          success = True
          break
        else:
          last_err = res.stderr.strip() or res.stdout.strip()
      except Exception as e:
        last_err = str(e)

    if success:
      self.log("✨ MP4Box FastStart (-inter 500) สำเร็จเรียบร้อย 100%!")
      return True
    else:
      self.log(
          f"⚠️ MP4Box แจ้งเตือน: {last_err or 'ไม่พบคำสั่ง mp4box/MP4Box ใน System PATH'}"
      )
      self.log(
          "💡 แนะนำ: ติดตั้ง GPAC/MP4Box ลงใน PATH เครื่องเพื่อผลลัพธ์ที่ดีที่สุด"
          " (ไฟล์ยังมี FastStart พื้นฐานจาก FFmpeg)"
      )
      return False

  def get_video_fps(self, input_file):
    try:
      cmd = f'ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate,avg_frame_rate -of json "{input_file}"'
      res = subprocess.run(
          cmd,
          capture_output=True,
          text=True,
          shell=True,
          encoding="utf-8",
          errors="ignore",
      )
      if res.returncode == 0 and res.stdout.strip():
        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        if streams:
          for key in ["r_frame_rate", "avg_frame_rate"]:
            rate_str = streams[0].get(key, "")
            if "/" in rate_str:
              num, den = rate_str.split("/")
              if float(den) > 0:
                val = float(num) / float(den)
                if 10.0 <= val <= 240.0:
                  return val
    except Exception as e:
      self.log(f"⚠️ ตรวจสอบ FPS ขัดข้อง: {str(e)}")
    return 24.0

  def calculate_gop(self, in_fps, is_deinterlace_2x):
    effective_fps = in_fps * 2.0 if is_deinterlace_2x else in_fps
    return int(round(effective_fps * 15.0))

  def build_nvenc_ffmpeg_args(
      self,
      raw_param_str,
      gop_val=None,
      deinterlace=False,
      deint_algo="bwdif",
      deint_parity="auto",
  ):
    args = [
        "-c:v",
        "hevc_nvenc",
        "-preset",
        "p7",
        "-tune",
        "hq",
        "-profile:v",
        "main",
        "-pix_fmt",
        "yuv420p",
    ]

    # กำหนดตัวถอดสลับเส้น (CUDA Accelerated Filter)
    if deinterlace:
      algo_filter = "bwdif_cuda" if "bw" in deint_algo.lower() else "yadif_cuda"

      p_clean = "auto"
      if "tff" in deint_parity.lower():
        p_clean = "tff"
      elif "bff" in deint_parity.lower():
        p_clean = "bff"

      args.extend(
          ["-vf", f"{algo_filter}=mode=send_field:parity={p_clean}:deint=all"]
      )

    for pair in raw_param_str.split(":"):
      if not pair or "=" not in pair:
        continue
      k, v = pair.split("=", 1)
      k, v = k.strip(), v.strip()

      if k == "gop":
        final_gop = str(gop_val) if gop_val is not None else v
        args.extend(["-g", str(final_gop)])
      elif k == "ref":
        args.extend(["-refs", str(v)])
      elif k == "tune":
        pass
      elif k == "rc":
        args.extend(["-rc:v", str(v)])
      else:
        args.extend([f"-{k}", str(v)])

    return args

  # -------------------------------------------------------------
  # Audio Processing Utilities (QAAC + Opus Pipe)
  # -------------------------------------------------------------
  def get_audio_dialnorms_mediainfo(self, input_file):
    dialnorms = {}
    try:
      cmd_json = f'mediainfo --Output=JSON "{input_file}"'
      res = subprocess.run(
          cmd_json,
          capture_output=True,
          text=True,
          shell=True,
          encoding="utf-8",
          errors="ignore",
      )

      if res.returncode == 0 and res.stdout.strip():
        data = json.loads(res.stdout)
        tracks = data.get("media", {}).get("track", [])
        a_idx = 0
        for t in tracks:
          if t.get("@type") == "Audio":
            dn_val = None
            combined = {**t, **t.get("extra", {})}
            for k, v in combined.items():
              if any(
                  key in k.lower()
                  for key in [
                      "dialnorm",
                      "dialnorm_average",
                      "dialog_normalization",
                  ]
              ):
                match = re.search(r"-?\d+", str(v))
                if match:
                  dn_val = int(match.group(0))
                  break
            dialnorms[a_idx] = dn_val
            a_idx += 1

      if not dialnorms:
        cmd_inform = (
            f'mediainfo --Inform="Audio;%dialnorm%|%extra%\\n" "{input_file}"'
        )
        res_inf = subprocess.run(
            cmd_inform,
            capture_output=True,
            text=True,
            shell=True,
            encoding="utf-8",
            errors="ignore",
        )
        if res_inf.returncode == 0:
          lines = [
              l.strip()
              for l in res_inf.stdout.strip().split("\n")
              if l.strip()
          ]
          for idx, line in enumerate(lines):
            match = re.search(
                r"(?:dialnorm\s*:\s*|dialnorm_Average\s*:\s*|^)(-?\d+)",
                line,
                re.IGNORECASE,
            )
            if match:
              dialnorms[idx] = int(match.group(1))

    except Exception as e:
      self.log(f"⚠️ เกิดข้อผิดพลาดในการรัน MediaInfo: {str(e)}")

    return dialnorms

  def build_downmix_filter(self, channels, channel_layout, is_dplii):
    if channels <= 2:
      return "aformat=channel_layouts=stereo"

    layout = (channel_layout or "").lower()

    if is_dplii:
      return "aresample=matrix_encoding=dplii,aformat=channel_layouts=stereo"

    if channels >= 8 or "7.1" in layout or "7.0" in layout:
      return (
          "pan=stereo|"
          "FL=0.37*FL+0.26*FC+0.26*BL+0.26*SL|"
          "FR=0.37*FR+0.26*FC+0.26*BR+0.26*SR,"
          "aformat=channel_layouts=stereo"
      )
    elif channels == 6 or "5.1" in layout or channels == 5 or "5.0" in layout:
      return (
          "pan=stereo|"
          "FL=0.46*FL+0.32*FC+0.32*BL|"
          "FR=0.46*FR+0.32*FC+0.32*BR,"
          "aformat=channel_layouts=stereo"
      )
    elif channels == 4 or "4.0" in layout or "quad" in layout:
      return (
          "pan=stereo|"
          "FL=0.5*FL+0.5*BL|"
          "FR=0.5*FR+0.5*BR,"
          "aformat=channel_layouts=stereo"
      )
    elif "4.1" in layout:
      return (
          "pan=stereo|"
          "FL=0.5*FL+0.35*FC+0.35*BL|"
          "FR=0.5*FR+0.35*FC+0.35*BR,"
          "aformat=channel_layouts=stereo"
      )
    else:
      return "aresample=matrix_encoding=none,aformat=channel_layouts=stereo"

  def detect_streams_info(self, input_file):
    cmd = f'ffprobe -v error -show_streams -of json "{input_file}"'
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        shell=True,
        encoding="utf-8",
        errors="ignore",
    )
    if res.returncode != 0:
      raise Exception("ไม่สามารถอ่านข้อมูลสตรีมด้วย ffprobe ได้")

    probe_data = json.loads(res.stdout)
    all_audio_streams = []
    matched_audio_tracks = []
    sub_tracks = []

    audio_seq = 0
    for s in probe_data.get("streams", []):
      codec_type = s.get("codec_type")
      codec_name = s.get("codec_name", "").lower()
      tags = s.get("tags", {})
      lang = tags.get("language", "").lower().strip()
      title = tags.get("title", "").lower().strip()
      disp = s.get("disposition", {})

      if codec_type == "audio":
        all_audio_streams.append(s)
        target_lang = None
        display_name = ""

        if lang in ["tha", "th", "thai"] or any(
            k in title for k in ["thai", "พากย์ไทย", "ภาษาไทย", "เสียงไทย"]
        ):
          target_lang = "tha"
          display_name = "Thai"
        elif lang in ["eng", "en", "english"] or any(
            k in title for k in ["english", "eng", "original"]
        ):
          target_lang = "eng"
          display_name = "English"

        if target_lang:
          matched_audio_tracks.append({
              "stream_index": s["index"],
              "audio_seq_index": audio_seq,
              "channels": s.get("channels", 2),
              "channel_layout": s.get("channel_layout", ""),
              "lang_code": target_lang,
              "display_name": display_name,
              "orig_title": tags.get("title", ""),
              "disposition": disp,
          })
        audio_seq += 1

      elif codec_type == "subtitle":
        is_th = lang in ["tha", "th", "thai"] or any(
            k in title
            for k in ["thai", "ไทย", "พากย์ไทย", "ซับไทย", "บรรยายไทย"]
        )
        is_en = lang in ["eng", "en", "english"] or any(
            k in title for k in ["english", "eng"]
        )
        is_sdh = disp.get("hearing_impaired") == 1 or any(
            k in title
            for k in ["sdh", "[sdh]", "(sdh)", "cc", "closed caption"]
        )

        sub_tracks.append({
            "stream_index": s["index"],
            "codec_name": codec_name,
            "lang": "tha" if is_th else ("eng" if is_en else lang or "und"),
            "orig_lang": lang,
            "title": tags.get("title", ""),
            "is_th": is_th,
            "is_en": is_en,
            "is_sdh": is_sdh,
            "disposition": disp,
        })

    if not matched_audio_tracks and all_audio_streams:
      self.log(
          "⚠️ ไม่พบ Language Tag ในแทร็กเสียง -> ใช้ค่า Fallback"
          " ที่ผู้ใช้กำหนดใน UI"
      )
      fb_configs = [
          (
              self.fb_tr1_lang.get(),
              self.fb_tr1_title.get().strip() or "English Audio",
          ),
          (
              self.fb_tr2_lang.get(),
              self.fb_tr2_title.get().strip() or "Thai Audio",
          ),
      ]

      for idx, s in enumerate(all_audio_streams):
        if idx < len(fb_configs):
          lang_c, t_title = fb_configs[idx]
        else:
          lang_c, t_title = f"und{idx+1}", f"Audio {idx+1}"

        disp_name = (
            "English"
            if lang_c == "eng"
            else ("Thai" if lang_c == "tha" else "Unknown")
        )
        matched_audio_tracks.append({
            "stream_index": s["index"],
            "audio_seq_index": idx,
            "channels": s.get("channels", 2),
            "channel_layout": s.get("channel_layout", ""),
            "lang_code": lang_c,
            "display_name": disp_name,
            "orig_title": t_title,
            "disposition": s.get("disposition", {}),
        })

    filtered_subs = []
    if self.filter_sub_th_en.get():
      filtered_subs = [st for st in sub_tracks if st["is_th"] or st["is_en"]]
      self.log(
          f"🔍 ตรวจพบ Subtitle ทั้งหมด {len(sub_tracks)} แทร็ก -> กรองเหลือ"
          f" TH/EN {len(filtered_subs)} แทร็ก"
      )
    else:
      filtered_subs = sub_tracks

    return matched_audio_tracks, filtered_subs

  # -------------------------------------------------------------
  # Batch Manager & Single Process Pipeline
  # -------------------------------------------------------------
  def batch_manager(self, input_path):
    try:
      files_to_process = []
      if os.path.isdir(input_path):
        for root_dir, _, filenames in os.walk(input_path):
          for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in [".mkv", ".mp4"] and not fn.startswith("__temp_"):
              if fn.endswith("_multi_audio.mkv") or fn.endswith(
                  "_multi_audio.mp4"
              ):
                continue
              files_to_process.append(os.path.join(root_dir, fn))
      else:
        files_to_process.append(input_path)

      if not files_to_process:
        self.log("❌ ไม่พบไฟล์วิดีโอ .mkv หรือ .mp4 ที่ต้องการประมวลผล")
        messagebox.showwarning(
            "Warning", "ไม่พบไฟล์วิดีโอ .mkv หรือ .mp4 ในตำแหน่งที่เลือก"
        )
        return

      total = len(files_to_process)
      self.log(
          f"🚀 ตรวจพบไฟล์ที่ต้องประมวลผลทั้งหมด {total} ไฟล์ เริ่มต้นทำงาน..."
      )

      success_count = 0
      for idx, fpath in enumerate(files_to_process, 1):
        self.log(f"\n{'='*65}")
        self.log(
            f"[{idx}/{total}] เริ่มประมวลผล: {os.path.basename(fpath)}"
        )
        self.log(f"{'='*65}")
        ok = self.process_single_video(fpath)
        if ok:
          success_count += 1

      self.log(
          f"\n✨ การประมวลผลเสร็จสมบูรณ์ทุกรายการ! ({success_count}/{total} ไฟล์"
          " สำเร็จ)"
      )
      messagebox.showinfo(
          "Complete",
          f"ประมวลผลเสร็จสิ้นเรียบร้อยทั้งหมด {success_count}/{total} ไฟล์!",
      )

    except Exception as e:
      self.log(f"\n❌ ข้อผิดพลาดใน Batch Manager: {str(e)}")
      messagebox.showerror("Error", str(e))
    finally:
      self.btn_run.config(state="normal")

  def process_single_video(self, input_file):
    base_dir = os.path.dirname(input_file)
    file_name, _ = os.path.splitext(os.path.basename(input_file))
    container = self.output_container.get().lower()
    is_mp4 = container == "mp4"

    temp_files = []
    temp_final = os.path.join(base_dir, f"__temp_{file_name}_final.{container}")

    if self.save_mode.get() == "new_file":
      target_output = os.path.join(
          base_dir, f"{file_name}_multi_audio.{container}"
      )
    else:
      orig_base, _ = os.path.splitext(input_file)
      target_output = f"{orig_base}.{container}"

    is_dplii = self.downmix_mode.get() == "dplii"
    do_aac = self.enable_aac.get()
    do_opus = self.enable_opus.get()

    aac_mode = self.aac_mode.get()
    aac_bitrate = self.aac_bitrate_var.get().strip() or "160"

    opus_mode = self.opus_mode.get()
    opus_bitrate = self.opus_bitrate_var.get().strip() or "128"
    opus_channels = self.opus_channels_mode.get()

    tvbr_map = {
        "96": "54",
        "112": "63",
        "128": "73",
        "160": "82",
        "192": "91",
        "224": "100",
        "256": "109",
        "288": "118",
        "320": "127",
    }

    preset_name = self.selected_preset_name.get()
    preset_data = self.nvenc_presets.get(preset_name, {})
    preferred_default_lang = self.default_audio_pref.get()

    try:
      # [1] วิเคราะห์แทร็กเสียงและซับไตเติล
      self.log(
          "[1/3] ตรวจสอบโครงสร้างไฟล์และค้นหาแทร็ก TH / EN / Subtitle..."
      )
      matched_tracks, filtered_subs = self.detect_streams_info(input_file)

      if not matched_tracks:
        raise Exception(
            "ไม่พบแทร็กเสียงภาษาไทย (TH) หรือภาษาอังกฤษ (EN) ในไฟล์นี้"
        )

      dialnorm_map = self.get_audio_dialnorms_mediainfo(input_file)

      default_audio_idx = -1
      for idx, trk in enumerate(matched_tracks):
        is_special = (
            trk["disposition"].get("commentary") == 1
            or trk["disposition"].get("hearing_impaired") == 1
            or trk["disposition"].get("visual_impaired") == 1
            or any(
                k in trk["orig_title"].lower()
                for k in ["commentary", "sdh", "desc"]
            )
        )
        if trk["lang_code"] == preferred_default_lang and not is_special:
          default_audio_idx = idx
          break

      if default_audio_idx == -1:
        for idx, trk in enumerate(matched_tracks):
          if trk["lang_code"] == preferred_default_lang:
            default_audio_idx = idx
            break

      if default_audio_idx == -1:
        default_audio_idx = 0

      for idx, trk in enumerate(matched_tracks):
        def_tag = " [★ DEFAULT]" if idx == default_audio_idx else ""
        layout_str = (
            f", {trk['channel_layout']}" if trk["channel_layout"] else ""
        )
        self.log(
            f"   - Audio {idx+1}: {trk['display_name']} ({trk['lang_code']},"
            f" {trk['channels']}ch{layout_str}) [Stream"
            f" #{trk['stream_index']}]{def_tag} {trk['orig_title']}"
        )

      for idx, sub in enumerate(filtered_subs):
        sdh_tag = " [SDH]" if sub["is_sdh"] else ""
        self.log(
            f"   - Subtitle {idx+1}: {sub['lang'].upper()}{sdh_tag} [Stream"
            f" #{sub['stream_index']}] {sub['title']}"
        )

      # [2] เริ่มกระบวนการแปลงรหัสเสียง (QAAC / Opus Pipe)
      self.log("\n[2/3] เริ่มกระบวนการแปลงรหัสเสียง...")
      encoded_audio_outputs = []
      downmix_name = (
          "Dolby Pro Logic II (DPL II)" if is_dplii else "Standard Stereo"
      )

      for i, trk in enumerate(matched_tracks):
        dialnorm_val = dialnorm_map.get(trk["audio_seq_index"])
        gain_filter = []
        if dialnorm_val is not None:
          dn_norm = -abs(dialnorm_val)
          gain_db = -(31 + dn_norm)
          gain_filter.append(f"volume={gain_db}dB")
          self.log(
              f"   -> แทร็ก #{i+1} ({trk['display_name']}): ตรวจพบ Dialnorm"
              f" {dn_norm} dB -> ชดเชย Gain {gain_db:+d} dB"
          )
        else:
          self.log(
              f"   -> แทร็ก #{i+1} ({trk['display_name']}): ไม่พบค่า Dialnorm"
              " (ใช้ระดับเสียงเดิม)"
          )

        dm_matrix = self.build_downmix_filter(
            trk["channels"], trk["channel_layout"], is_dplii
        )
        dm_filters = (
            list(gain_filter)
            + [dm_matrix]
            + ["alimiter=limit=-1.0dB:attack=5:release=50"]
        )
        dm_filter_chain = ",".join(dm_filters)

        pt_filters = list(gain_filter) + [
            "alimiter=limit=-1.0dB:attack=5:release=50"
        ]
        pt_filter_chain = ",".join(pt_filters)

        # Apple AAC
        if do_aac:
          temp_aac = os.path.join(base_dir, f"__temp_{file_name}_tr{i}_aac.m4a")
          temp_files.append(temp_aac)

          if aac_mode == "tvbr":
            q_val = tvbr_map.get(aac_bitrate, "91")
            qaac_opt = f"--tvbr {q_val}"
            mode_desc = f"TVBR ~{aac_bitrate}k (q{q_val})"
          elif aac_mode == "cvbr":
            qaac_opt = f"--cvbr {aac_bitrate}"
            mode_desc = f"CVBR {aac_bitrate}k"
          else:
            qaac_opt = f"--abr {aac_bitrate}"
            mode_desc = f"ABR {aac_bitrate}k"

          self.log(
              f"     [AAC] แทร็ก #{i+1} ({trk['display_name']}) -> Downmix 2.0"
              f" ({downmix_name}) | QAAC {mode_desc}"
          )

          cmd_qaac = (
              f'ffmpeg -v error -stats -i "{input_file}" -vn -map'
              f' 0:{trk["stream_index"]} -filter:a "{dm_filter_chain}" -c:a'
              f" pcm_f32le -f wav - | qaac64.exe {qaac_opt} --no-delay -q 2 --normalize -"
              f' -o "{temp_aac}"'
          )
          ret_aac = subprocess.run(cmd_qaac, shell=True)
          if ret_aac.returncode != 0:
            raise Exception(
                f"QAAC Encoding ล้มเหลวที่แทร็ก {trk['display_name']}"
            )

          downmix_tag = (
              "DPL II" if is_dplii and trk["channels"] > 2 else "Stereo"
          )
          aac_title = (
              f"{trk['display_name']} ({downmix_tag} 2.0 - Apple AAC"
              f" {mode_desc})"
          )
          encoded_audio_outputs.append({
              "file": temp_aac,
              "title": aac_title,
              "lang_code": trk["lang_code"],
              "orig_index": i,
              "disposition": trk["disposition"],
              "orig_title": trk["orig_title"],
              "is_aac": True,
          })

        # Opus
        if do_opus:
          temp_opus = os.path.join(
              base_dir, f"__temp_{file_name}_tr{i}_opus.opus"
          )
          temp_files.append(temp_opus)

          vbr_opt = "on" if opus_mode == "vbr" else "constrained"
          mode_desc = (
              f"VBR {opus_bitrate}k"
              if opus_mode == "vbr"
              else f"ABR {opus_bitrate}k"
          )

          if opus_channels == "downmix":
            chosen_filter = dm_filter_chain
            ch_tag = "DPL II 2.0" if is_dplii and trk["channels"] > 2 else "2.0"
          else:
            chosen_filter = pt_filter_chain
            ch_tag = f"{trk['channels']}ch"

          self.log(
              f"     [Opus] แทร็ก #{i+1} ({trk['display_name']}) -> {ch_tag} |"
              f" libopus {mode_desc}"
          )

          cmd_opus = (
              f'ffmpeg -y -v error -stats -i "{input_file}" -vn -map'
              f' 0:{trk["stream_index"]} -filter:a "{chosen_filter}" -c:a'
              f" libopus -b:a {opus_bitrate}k -vbr {vbr_opt} -f ogg"
              f' "{temp_opus}"'
          )
          ret_opus = subprocess.run(cmd_opus, shell=True)
          if ret_opus.returncode != 0:
            raise Exception(
                f"Opus Encoding ล้มเหลวที่แทร็ก {trk['display_name']}"
            )

          opus_title = (
              f"{trk['display_name']} ({ch_tag} - Opus {mode_desc})"
          )
          encoded_audio_outputs.append({
              "file": temp_opus,
              "title": opus_title,
              "lang_code": trk["lang_code"],
              "orig_index": i,
              "disposition": trk["disposition"],
              "orig_title": trk["orig_title"],
              "is_aac": False,
          })

      # [3] รวมไฟล์ Container (MKV / MP4) & รัน FastStart
      self.log(
          f"\n[3/3] กำลังประกอบไฟล์ {container.upper()} & เข้ารหัสวิดีโอ"
          f" ({preset_name})..."
      )

      cmd_remux = [
          "ffmpeg",
          "-y",
          "-v",
          "warning",
          "-stats",
      ]
      is_copy_video = "Copy Video" in preset_name
      if not is_copy_video:
        cmd_remux.extend(["-hwaccel", "cuda"])

      cmd_remux.extend(["-i", f'"{input_file}"'])

      for item in encoded_audio_outputs:
        cmd_remux.extend(["-i", f'"{item["file"]}"'])

      # Video Muxing
      if is_copy_video:
        cmd_remux.extend([
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-bsf:v",
            "setts=pts=PTS-STARTPTS:dts=DTS-STARTDTS",
        ])
        if is_mp4:
          cmd_remux.extend(["-tag:v", "hvc1"])
      else:
        in_fps = self.get_video_fps(input_file)
        is_deint = self.enable_deint.get()
        deint_algo_sel = self.deint_algo.get()
        deint_parity_sel = self.deint_parity.get()

        gop_calc = self.calculate_gop(in_fps, is_deint)
        eff_fps = in_fps * 2.0 if is_deint else in_fps

        deint_status_str = (
            f"เปิดใช้งาน ({deint_algo_sel.split(' ')[0].upper()} -"
            f" {deint_parity_sel.split(' ')[0].upper()})"
            if is_deint
            else "ปิด (Progressive)"
        )
        self.log(
            f"   🎬 [NVENC] ตรวจพบ FPS={in_fps:.2f} -> Effective"
            f" FPS={eff_fps:.2f} [Deinterlace: {deint_status_str}] -> คำนวณ"
            f" GOP={gop_calc}"
        )

        nvenc_args = self.build_nvenc_ffmpeg_args(
            preset_data.get("params", ""),
            gop_val=gop_calc,
            deinterlace=is_deint,
            deint_algo=deint_algo_sel,
            deint_parity=deint_parity_sel,
        )
        cmd_remux.extend(["-map", "0:v:0"])
        cmd_remux.extend(nvenc_args)
        if is_mp4:
          cmd_remux.extend(["-tag:v", "hvc1"])

      # Audio Muxing
      primary_default_set = False
      for out_idx, item in enumerate(encoded_audio_outputs):
        input_id = out_idx + 1
        desc = []
        if (
            item["disposition"].get("hearing_impaired") == 1
            or "sdh" in item["orig_title"].lower()
        ):
          desc.append("SDH")
        if (
            item["disposition"].get("commentary") == 1
            or "commentary" in item["orig_title"].lower()
        ):
          desc.append("Commentary")
        if (
            item["disposition"].get("visual_impaired") == 1
            or "desc" in item["orig_title"].lower()
        ):
          desc.append("Audio Description")

        desc_str = f" [{', '.join(desc)}]" if desc else ""
        final_track_title = f"{item['title']}{desc_str}"

        is_default = False
        if not primary_default_set and item["orig_index"] == default_audio_idx:
          is_default = True
          primary_default_set = True

        disp_flags = []
        if is_default:
          disp_flags.append("default")
        if "SDH" in desc:
          disp_flags.append("hearing_impaired")
        if "Commentary" in desc:
          disp_flags.append("commentary")
        if "Audio Description" in desc:
          disp_flags.append("visual_impaired")
        disp_str = "+".join(disp_flags) if disp_flags else "0"

        cmd_remux.extend([
            "-map",
            f"{input_id}:a:0",
            f"-c:a:{out_idx}",
            "copy",
            f"-metadata:s:a:{out_idx}",
            f'language={item["lang_code"]}',
            f"-metadata:s:a:{out_idx}",
            f'title="{final_track_title}"',
            f"-disposition:a:{out_idx}",
            disp_str,
        ])

      # Subtitle Muxing
      sub_out_idx = 0
      for st in filtered_subs:
        if is_mp4 and st["codec_name"] in [
            "hdmv_pgs_subtitle",
            "dvd_subtitle",
            "dvb_subtitle",
        ]:
          self.log(
              f"   ⚠️ ข้าม Subtitle แทร็ก {st['stream_index']} ({st['codec_name']})"
              " เนื่องจากคอนเทนเนอร์ MP4 รองรับเฉพาะ Text Subtitle"
          )
          continue

        sub_codec = "mov_text" if is_mp4 else "copy"
        cmd_remux.extend([
            "-map",
            f"0:{st['stream_index']}",
            f"-c:s:{sub_out_idx}",
            sub_codec,
            f"-metadata:s:s:{sub_out_idx}",
            f"language={st['lang']}",
        ])
        if st["title"]:
          cmd_remux.extend(
              [f"-metadata:s:s:{sub_out_idx}", f'title="{st["title"]}"']
          )

        sub_disp_flags = []
        if st["is_sdh"]:
          sub_disp_flags.append("hearing_impaired")
        if st["is_en"] and sub_out_idx == 0:
          sub_disp_flags.append("default")

        s_disp_str = "+".join(sub_disp_flags) if sub_disp_flags else "0"
        cmd_remux.extend([f"-disposition:s:{sub_out_idx}", s_disp_str])
        sub_out_idx += 1

      # Metadata & Chapter Marker Passthrough 100%
      cmd_remux.extend([
          "-map_chapters",
          "0",
          "-map_metadata",
          "0",
          "-strict",
          "-2",  # รองรับ Opus ใน MP4
          "-max_interleave_delta",
          "0",
          "-avoid_negative_ts",
          "make_zero",
      ])

      if is_mp4:
        cmd_remux.extend(["-movflags", "+faststart"])

      cmd_remux.append(f'"{temp_final}"')

      ret_remux = subprocess.run(" ".join(cmd_remux), shell=True)
      if ret_remux.returncode != 0:
        raise Exception(
            f"FFmpeg Remuxing / NVENC ล้มเหลวที่ไฟล์ {os.path.basename(input_file)}"
        )

      # เรียกใช้งาน MP4Box FastStart (-inter 500)
      if is_mp4 and self.enable_faststart.get():
        self.run_mp4box_faststart(temp_final)

      # จัดการบันทึกไฟล์ (Safe Swap)
      if self.save_mode.get() == "replace":
        self.log("กำลังแทนที่ไฟล์เดิม (Safe Swap)...")
        if os.path.abspath(input_file) != os.path.abspath(target_output):
          if os.path.exists(target_output):
            os.remove(target_output)
          os.rename(temp_final, target_output)
          if os.path.exists(input_file):
            os.remove(input_file)
        else:
          os.replace(temp_final, input_file)
      else:
        if os.path.exists(target_output):
          os.remove(target_output)
        os.rename(temp_final, target_output)

      self.log(f"✅ ประมวลผลสำเร็จ: {target_output}")
      return True

    except Exception as e:
      self.log(f"❌ ล้มเหลวที่ไฟล์ {os.path.basename(input_file)}: {str(e)}")
      return False

    finally:
      for tf in temp_files:
        if os.path.exists(tf):
          os.remove(tf)
      if os.path.exists(temp_final):
        os.remove(temp_final)


if __name__ == "__main__":
  root = tk.Tk()
  app = DualAudioVideoEncoderGUI(root)
  root.mainloop()