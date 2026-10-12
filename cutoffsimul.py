import os
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


class AudioConverterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Audio to FLAC Converter (Low-Pass Cutoff & Log)")
        self.root.geometry("580x640")
        self.root.resizable(True, True)

        # ตัวแปรเก็บข้อมูล
        self.file_path = tk.StringVar()
        self.cutoff_var = tk.IntVar(value=15000)
        self.slope_var = tk.StringVar(value="12 dB/oct (2 poles) - มาตรฐาน")

        self.slope_options = {
            "6 dB/oct (1 pole) - ชันน้อย": 1,
            "12 dB/oct (2 poles) - มาตรฐาน": 2,
            "24 dB/oct (4 poles) - ชันมาก": 4,
            "36 dB/oct (6 poles) - ชันสูงมาก": 6,
            "48 dB/oct (8 poles) - ชันเกือบเป็นเส้นตรง": 8,
        }

        self.create_widgets()

    def create_widgets(self):
        pad = {'padx': 12, 'pady': 5}

        # --- Section 1: เลือกไฟล์ ---
        file_frame = ttk.LabelFrame(self.root, text=" เลือกไฟล์เพลง ")
        file_frame.pack(fill="x", **pad)

        ttk.Entry(file_frame, textvariable=self.file_path, width=54).pack(side="left", padx=5, pady=8)
        ttk.Button(file_frame, text="เรียกดู...", command=self.browse_file).pack(side="left", padx=5, pady=8)

        # --- Section 2: ตั้งค่า Cutoff Frequency ---
        cutoff_frame = ttk.LabelFrame(self.root, text=" ตั้งค่า Low-Pass Cutoff Frequency (Hz) ")
        cutoff_frame.pack(fill="x", **pad)

        self.slider = ttk.Scale(
            cutoff_frame,
            from_=500,
            to=22050,
            orient="horizontal",
            variable=self.cutoff_var,
            command=self.on_slider_move
        )
        self.slider.pack(fill="x", padx=15, pady=(6, 2))

        entry_frame = ttk.Frame(cutoff_frame)
        entry_frame.pack(pady=3)

        ttk.Label(entry_frame, text="ความถี่ตัด (Cutoff):").pack(side="left", padx=5)
        self.entry = ttk.Entry(entry_frame, textvariable=self.cutoff_var, width=10, justify="center")
        self.entry.pack(side="left", padx=5)
        ttk.Label(entry_frame, text="Hz").pack(side="left")

        self.entry.bind("<FocusOut>", self.validate_entry)
        self.entry.bind("<Return>", self.validate_entry)

        # Preset Buttons
        preset_frame = ttk.Frame(cutoff_frame)
        preset_frame.pack(pady=4)
        presets = [("8 kHz", 8000), ("12 kHz", 12000), ("15 kHz", 15000), ("18 kHz", 18000), ("20 kHz", 20000)]
        for label, val in presets:
            ttk.Button(preset_frame, text=label, width=8, command=lambda v=val: self.set_cutoff(v)).pack(side="left", padx=2)

        # --- Section 3: ตั้งค่า Slope / Roll-off Rate ---
        slope_frame = ttk.LabelFrame(self.root, text=" ตั้งค่าความชันของการตัด (Slope) ")
        slope_frame.pack(fill="x", **pad)

        ttk.Label(slope_frame, text="เลือกระดับ Slope:").pack(side="left", padx=10, pady=6)
        slope_combo = ttk.Combobox(
            slope_frame,
            textvariable=self.slope_var,
            values=list(self.slope_options.keys()),
            state="readonly",
            width=38
        )
        slope_combo.pack(side="left", padx=5, pady=6)

        # --- Section 4: ปุ่มดำเนินการและสถานะ ---
        action_frame = ttk.Frame(self.root)
        action_frame.pack(fill="x", **pad)

        self.convert_btn = ttk.Button(action_frame, text="แปลงเป็น FLAC", command=self.start_conversion)
        self.convert_btn.pack(pady=4)

        self.status_label = ttk.Label(action_frame, text="สถานะ: พร้อมใช้งาน", font=("Tahoma", 9))
        self.status_label.pack(pady=2)

        self.progress = ttk.Progressbar(action_frame, mode="indeterminate")

        # --- Section 5: กล่องแสดง FFmpeg Command & Output Log ---
        log_frame = ttk.LabelFrame(self.root, text=" FFmpeg Command & Output Log ")
        log_frame.pack(fill="both", expand=True, **pad)

        self.log_box = ScrolledText(log_frame, height=10, font=("Consolas", 8), bg="#1e1e1e", fg="#00ff00")
        self.log_box.pack(fill="both", expand=True, padx=5, pady=5)

    def append_log(self, text):
        """เขียนข้อความลงในกล่อง Log"""
        self.log_box.insert(tk.END, text + "\n")
        self.log_box.see(tk.END)

    def clear_log(self):
        """ล้างกล่อง Log"""
        self.log_box.delete("1.0", tk.END)

    def browse_file(self):
        filename = filedialog.askopenfilename(
            title="เลือกไฟล์เสียง",
            filetypes=[("Audio Files", "*.mp3 *.wav *.m4a *.aac *.ogg *.flac *.wma"), ("All Files", "*.*")]
        )
        if filename:
            self.file_path.set(filename)

    def set_cutoff(self, value):
        self.cutoff_var.set(value)

    def on_slider_move(self, val):
        self.cutoff_var.set(int(float(val)))

    def validate_entry(self, event=None):
        try:
            val = int(self.cutoff_var.get())
            val = max(500, min(22050, val))
            self.cutoff_var.set(val)
        except tk.TclError:
            self.cutoff_var.set(15000)

    def generate_filter_string(self, cutoff, total_poles):
        """สร้างฟิลเตอร์สตรีมโดยการต่อซ้อน lowpass=f=...:p=2 เพื่อหลีกเลี่ยงข้อจำกัดของ FFmpeg"""
        if total_poles == 1:
            return f"lowpass=f={cutoff}:p=1"
        
        passes = total_poles // 2
        filter_list = [f"lowpass=f={cutoff}:p=2"] * passes
        return ",".join(filter_list)

    def start_conversion(self):
        input_file = self.file_path.get()
        if not input_file or not os.path.exists(input_file):
            messagebox.showerror("ผิดพลาด", "กรุณาเลือกไฟล์เพลงที่ถูกต้องก่อน!")
            return

        self.clear_log()
        self.convert_btn.config(state="disabled")
        self.status_label.config(text="กำลังแปลงไฟล์...", foreground="blue")
        self.progress.pack(fill="x", padx=30, pady=4)
        self.progress.start(10)

        threading.Thread(target=self.convert_audio, daemon=True).start()

    def convert_audio(self):
        input_file = self.file_path.get()
        cutoff = self.cutoff_var.get()
        
        selected_slope_str = self.slope_var.get()
        poles = self.slope_options.get(selected_slope_str, 2)
        db_label = selected_slope_str.split(" ")[0]

        # สร้าง Filter String แบบCascading (ซ้อนกัน)
        audio_filter = self.generate_filter_string(cutoff, poles)

        base_name, _ = os.path.splitext(input_file)
        output_file = f"{base_name}_cutoff_{cutoff}Hz_{db_label}.flac"

        cmd = [
            "ffmpeg", "-y",
            "-i", input_file,
            "-af", audio_filter,
            "-c:a", "flac",
            output_file
        ]

        # แสดงคำสั่งที่ใช้รันในกล่อง Log
        cmd_str = " ".join([f'"{arg}"' if " " in arg else arg for arg in cmd])
        self.root.after(0, self.append_log, f"[EXECUTING COMMAND]:\n{cmd_str}\n" + "-"*60)

        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            # รันกระบวนการ FFmpeg
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # รวม stderr ไว้ที่ stdout เพื่อแสดงผลใน log
                startupinfo=startupinfo,
                text=True,
                encoding="utf-8",
                errors="replace"
            )

            # ดึง Log ออกมาทีละบรรทัดแบบ Real-time
            for line in iter(process.stdout.readline, ''):
                if line:
                    self.root.after(0, self.append_log, line.strip())

            process.stdout.close()
            process.wait()

            if process.returncode == 0:
                self.root.after(0, self.on_success, output_file)
            else:
                self.root.after(0, self.on_error, f"FFmpeg Exit Code: {process.returncode}")
        except Exception as e:
            self.root.after(0, self.on_error, str(e))

    def on_success(self, output_file):
        self.progress.stop()
        self.progress.pack_forget()
        self.convert_btn.config(state="normal")
        self.status_label.config(text="แปลงไฟล์สำเร็จ!", foreground="green")
        self.append_log(f"\n[SUCCESS]: Saved to {output_file}")
        messagebox.showinfo("สำเร็จ", f"แปลงไฟล์เป็น FLAC เรียบร้อยแล้ว!\n\nบันทึกไฟล์ไว้ที่:\n{output_file}")

    def on_error(self, err):
        self.progress.stop()
        self.progress.pack_forget()
        self.convert_btn.config(state="normal")
        self.status_label.config(text="เกิดข้อผิดพลาดในการแปลงไฟล์", foreground="red")
        self.append_log(f"\n[ERROR]: {err}")
        
        err_msg = str(err)
        if "ffmpeg" in err_msg.lower() or "not found" in err_msg.lower():
            err_msg = "ไม่พบโปรแกรม FFmpeg ในระบบ กรุณาติดตั้ง FFmpeg และตั้งค่า PATH ก่อนใช้งาน"
        
        messagebox.showerror("ข้อผิดพลาด", f"ไม่สามารถแปลงไฟล์ได้ ดูรายละเอียดใน Log ด้านล่าง")


if __name__ == "__main__":
    root = tk.Tk()
    app = AudioConverterApp(root)
    root.mainloop()