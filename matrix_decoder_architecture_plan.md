# แผนงานทางเทคนิคและการออกแบบสถาปัตยกรรม (Architecture & Implementation Plan)
## CLI ถอดรหัสสัญญาณเสียง Matrix Lt/Rt 2.0 → 5.1 Surround (Float32)
**โมดูลเป้าหมาย:** `lib/2to6matrixdecoder.py` (รันเป็น CLI ได้ และ Import ใช้งานเป็นโมดูลได้)  
**สถานะ:** แผนงานเพื่อรอการอนุมัติ (ห้ามเขียนโค้ดจนกว่าจะได้รับอนุมัติ)  
**การจัดชั้นความแม่นยำ:**
- **[E1 ยืนยันได้]** สมการ passive matrix, การแมปช่องเสียง, และค่าคงที่ที่คำนวณย้อนกลับจากสมการ Encoder / มาตรฐานทางคณิตศาสตร์
- **[E2 ตามเอกสารสาธารณะ]** พารามิเตอร์, ตัวกรอง, และสถาปัตยกรรมที่ระบุในเอกสาร Dolby หรือสิทธิบัตรสาธารณะที่ตรวจสอบแล้ว (S1–S7)
- **[E3 สร้างขึ้นใหม่]** ลอจิกจำลอง, การแก้จุดกำกวมของสิทธิบัตร, การประมาณค่า, และโครงสร้าง DSP ภายในที่ไม่การันตี bit-exact กับชิปฮาร์ดแวร์ Dolby

---

## 0. แหล่งอ้างอิงที่ผ่านการตรวจสอบจริง (Verified References)
1. **[S1]** Dolby Laboratories, "Surround Pro Logic II Decoder – Principles of Operation" (Dressler, Roger, 2000) [E2]
2. **[S2]** Dolby Laboratories, "Surround Pro Logic Decoder – Principles of Operation" [E2]
3. **[S3]** US Patent 7,280,664 B2 / US 2004/0125960 A1: "Method and apparatus for audio matrix decoding" (James W. Fosgate, Stephen D. Vernon, Robert L. Andersen - Assigned to Dolby Laboratories Licensing Corp.) [E2]
4. **[S4]** Dolby Professional Support, "How do the 5.1 and Stereo downmix settings work?" [E2]
5. **[S5]** US Patent 8,280,743: "Surround sound downmixing and upmixing" (PLII encode/decode equations) [E2]
6. **[S6]** US Patent 5,862,228: "Matrix surround sound encoder and decoder" (4-channel Dolby Surround equations) [E2]
7. **[S7]** US Patent 9,311,923: "Multichannel audio processing" (กล่าวถึง PL emulation LPF 7 kHz) [E2]
8. **[S8]** US Patent 4,799,260: "Surround sound processor" (Original Pro Logic feed-forward servo) [E2]
9. **[Ref-Enc]** `lib/6to2matrixencoder.py` (มาตรฐานโครงสร้างโค้ด, I/O, WAV/RF64, exit codes ของโปรเจกต์) [E1]
10. **[Ref-Legacy]** `wav2ac3dpl2.py` (สูตรสัมประสิทธิ์ legacy encode เดิม) [E1]

---

## B. Errata และข้อสรุปทางคณิตศาสตร์ฝั่ง Encoder (เพื่อการทดสอบ Round-Trip)
ก่อนวางแผน Decoder ต้องบันทึก Errata ของสมการ Encoder เพื่อให้การทดสอบ Round-Trip มีจุดอ้างอิงที่แน่นอน:
1. **นิยามเชิงซ้อน $j$ และ Phase Shift [E1]:**
   - ในระบบดิจิทัล $x^*(n) = \mathcal{H}\{x(n)\}$ กำหนดให้เป็นผลลัพธ์ของ Hilbert Transformer ซึ่งหมุนเฟส $+90^\circ$ (เทียบเท่าตัวคูณ $+j = e^{+j\pi/2}$)
   - การทำ $\mathcal{H}\{\mathcal{H}\{x\}\} = -x$ ดังนั้น $-\mathcal{H}\{\cdot\}$ จึงหมุนเฟส $-90^\circ$ (เทียบเท่า $-j$)
2. **ความแตกต่างระหว่าง `wav2ac3dpl2.py` กับ `6to2matrixencoder.py` และมาตรฐาน S4/S5 [E1/E2]:**
   - ใน `wav2ac3dpl2.py`:
     $$Lt = L + 0.7079\cdot C - 0.8710\cdot Ls^* - 0.4898\cdot Rs^*$$
     $$Rt = R + 0.7079\cdot C + 0.4898\cdot Ls^* + 0.8710\cdot Rs^*$$
     โดยใช้ $a = 10^{-1.2/20} \approx 0.8710$, $b = 10^{-6.2/20} \approx 0.4898$ ($a^2 + b^2 \approx 0.9985 \approx 1.0$)
   - ใน `6to2matrixencoder.py` โหมด `standard`:
     $$Lt = L + 0.7071\cdot C - 0.8165\cdot Ls^* - 0.5774\cdot Rs^*$$
     $$Rt = R + 0.7071\cdot C + 0.5774\cdot Ls^* + 0.8165\cdot Rs^*$$
     โดยใช้ $a = \sqrt{2/3} \approx 0.8165$ ($-1.76\text{ dB}$), $b = \sqrt{1/3} \approx 0.5774$ ($-4.77\text{ dB}$)
   - **ข้อสรุป Errata:**
     - สูตร Dolby ดั้งเดิม (S4/S5) กำหนด $a = 10^{-1.2/20}$ และ $b = 10^{-6.2/20}$
     - สูตร $\sqrt{2/3}$ และ $\sqrt{1/3}$ เป็นสูตรสมมาตรพลังงานบริสุทธิ์ (Constant-Power)
     - ใน Decoder จะออกแบบให้ถอดรหัสเชิงเมทริกซ์แบบ **Real-Valued Adaptive Matrix** ตาม S3 ซึ่งรองรับทั้งสองแบบโดยอัตโนมัติ
3. **สมการ PLII โหมด $180^\circ$ (เดิมคือ pl2s) [E2]:**
   - ไม่มี Hilbert ($j=0$):
     $$Lt = L + 0.7071\cdot C - a\cdot Ls - b\cdot Rs$$
     $$Rt = R + 0.7071\cdot C + b\cdot Ls + a\cdot Rs$$
   - รองรับตัวเลือก `--pl2s-coef {dolby,a52}`:
     - `dolby`: $a \approx 0.871, b \approx 0.490$ (S4)
     - `a52`: $a = b = 0.7071$ (ATSC A/52 §7.8)
4. **สมการ Dolby Surround (ds) / Pro Logic (pl) Encode [E2]:**
   $$Lt = L + 0.7071\cdot C - 0.7071\cdot S^*$$
   $$Rt = R + 0.7071\cdot C + 0.7071\cdot S^*$$
   (โดย $S$ ผ่าน Band-pass Filter $100\text{ Hz} - 7\text{ kHz}$)

---

## 1. สถาปัตยกรรม โมดูล คลาส และ ฟังก์ชัน (Class & Module Architecture)
โมดูล `lib/2to6matrixdecoder.py` วางโครงสร้างสอดคล้องกับ `lib/6to2matrixencoder.py` 100%:

```python
# ==============================================================================
# โครงสร้างโมดูล lib/2to6matrixdecoder.py (เฉพาะ Signature และ Type Hint)
# ==============================================================================

class MatrixDecoderError(Exception):
    def __init__(self, message: str, exit_code: int = 1): ...

class MatrixMode(str, Enum):
    DS = "ds"
    PL = "pl"
    PL2 = "pl2"
    PL2MUSIC = "pl2music"

class SteeringEngine(str, Enum):
    FOSGATE = "fosgate"      # ค่าเริ่มต้น: S3 active matrix servo
    PASSIVE = "passive"      # เมทริกซ์คงที่ ล้วน
    PAIRINV = "pairinv"      # โหมดทดลอง Pair-inverse (Phase 2)

class ServoImpl(str, Enum):
    FAST = "fast"            # lfilter บล็อก + gain hop (ค่าเริ่มต้นเมื่อไม่มี numba)
    EXACT = "exact"          # Sample-by-sample ภายใน servo loop (ใช้ numba หากมี)

class OutLayout(str, Enum):
    SIDE_51 = "5.1side"      # Mask 0x060F (FL, FR, FC, LFE, SL, SR)
    BACK_51 = "5.1back"      # Mask 0x003F (FL, FR, FC, LFE, BL, BR)

class LfeMode(str, Enum):
    SILENT = "silent"        # LFE เงียบสนิท (0.0)
    BASS = "bass"            # ดึงเบสจาก Front L/R ผ่าน Low-Pass

@dataclass
class DecoderConfig:
    mode: MatrixMode
    steering: bool
    steer_engine: SteeringEngine
    servo_impl: ServoImpl
    coef_set: str            # 'auto', 'standard', 'pl-emulation'
    lookahead_ms: float      # 5.0 ms
    hop_size: int            # 8 samples
    servo_env_tc_ms: float   # 800.0 ms
    servo_fast_tc_ms: float  # 2.5 ms
    surround_delay_ms: float # pl/pl2: 10 ms (ปรับได้), ds: 20 ms
    surround_lpf: str        # 'off', '7k'
    surround_hp: str         # 'auto', 'off', or Hz
    cutoff_type: str         # 'hill', 'brick'
    slope: int               # 12 dB/oct
    makeup_gain_db: float    # 0.0 or +2.6 dB
    round_trip_phase: bool   # False (เปิดใช้ Hilbert ถอดเฟส 90° เฉพาะทดสอบ)
    out_layout: OutLayout
    lfe_mode: LfeMode
    lfe_lp_hz: float         # 120.0 Hz
    lfe_gain_db: float       # 0.0 dB
    lfe_redirect: bool       # False
    trims: Dict[str, float]  # {'L': 0.0, 'C': 0.0, ...}
    headroom: str            # 'none', 'static', 'peak'
    out_gain_db: float       # 0.0 dB
    block_size: int          # 65536
    ffmpeg_bin: Optional[str]

class FosgateServoControlPath:
    """แกนประมวลผลเส้นทางควบคุม servo 2 แกน (LR, FB) และตัวกำเนิดเกน 6 ตัว (gL, gR, gF, gB, gLB, gRB) [E2]"""
    def __init__(self, config: DecoderConfig, fs: int = 48000): ...
    def process_block(self, lt_prime: np.ndarray, rt_prime: np.ndarray) -> np.ndarray: ...
    # ส่งกลับ gains ขนาด (N_hops, 6) พร้อม interpolated ต่อตัวอย่าง

class FosgateAdaptiveMatrix:
    """คูณสัญญาณ Lt', Rt' ด้วยเมทริกซ์ 5x2 ที่แปรผันตามเกน g_k [E2]"""
    def __init__(self, coef_set: str = "standard"): ...
    def compute_matrix(self, gains: np.ndarray) -> np.ndarray: ... # ได้ M(n) ขนาด (N, 5, 2)
    def apply(self, lt_delayed: np.ndarray, rt_delayed: np.ndarray, gains: np.ndarray) -> np.ndarray: ...

class DecoderDSPPipeline:
    """ไปป์ไลน์ DSP จัดการ Signal Flow, Delay, Filter, Matrix, LFE และ Flush หาง [E1/E2/E3]"""
    def __init__(self, config: DecoderConfig, fs: int = 48000): ...
    def process_block(self, block_2ch: np.ndarray) -> np.ndarray: ...
    def flush(self) -> np.ndarray: ...

class DecoderPipeline:
    """ตัวควบคุมวงรอบการทำงานหลัก: I/O, Parsing, Block Streaming, Progress, RF64 [E1]"""
    def __init__(self, args: argparse.Namespace): ...
    def run(self) -> int: ...
```

---

## 2. แผนภาพ Signal Flow (End-to-End Processing Flow)

```mermaid
flowchart TD
    In[Input File / Stream Lt, Rt 2.0] --> Reader[WavStreamReader / FFmpeg Pipe]
    Reader --> Resample{Sample Rate == 48kHz?}
    Resample -- No --> FFResample[FFmpeg Pipe Resampler 48000 Hz] --> InputCheck
    Resample -- Yes --> InputCheck[Input Check & Channel Verification FL, FR]
    
    InputCheck --> AutoBal[Input Balance: off / auto / dB]
    AutoBal --> InGain[Input Attenuation -3.0 dB: Lt', Rt' [E2]]
    
    %% แยกเป็น 2 เส้นทาง: Control Path กับ Audio Path
    InGain --> LookaheadDelay[Look-ahead Delay Line: 5.0 ms / 240 samples [E2]]
    InGain --> CtrlBPF[BPF Pre-Filter: HP 788/1878Hz, LP 13466Hz [E2]]
    
    %% Control Path
    subgraph Control Path (Rate: 6000 hops/sec @ hop=8) [E2/E3]
        CtrlBPF --> AxisGen["แกนสเตอริโอ: Lt', Rt' และ แกนหน้าหลัง: Ft=(Lt'+Rt')/2, Bt=(Rt'-Lt')/2"]
        AxisGen --> RectSmooth["Absolute Value + Smooth 800 ms (|u_X|)"]
        RectSmooth --> LogSub["A0 Scale + Log2 + A1 Scale + Subtract (d = l_X - l_Y)"]
        LogSub --> PwLinear["A2 Scale + Piecewise-Linear Non-linear Function (v)"]
        PwLinear --> SmoothFast["Smooth 2.5 ms -> Error Signals: LR, FB"]
        SmoothFast --> VCAExp["A3/A4 Scale + Exp2 -> gL, gR, gF, gB"]
        SmoothFast --> RearLogic["B0-B5 Logic -> gLB, gRB (Rear Steering)"]
        VCAExp --> Interp[Linear Interpolation ระหว่าง Hop]
        RearLogic --> Interp
    end
    
    %% Audio Path
    LookaheadDelay --> AdaptiveMat["Adaptive Matrix Multiplier M(n) ขนาด 5x2 [E2]"]
    Interp -.->|Interpolated Gains g_k| AdaptiveMat
    
    AdaptiveMat --> SurrProc[Surround Post-Processing: Ls, Rs]
    subgraph Surround Path Conditioning [E2]
        SurrProc --> SurrLPF{Mode == ds / pl ?}
        SurrLPF -- Yes --> LPF7k[LPF 7 kHz Butterworth + Delay 10-20 ms]
        SurrLPF -- No (pl2) --> Delay10[Surround Delay 10 ms]
    end
    
    AdaptiveMat --> FrontProc[Front Channels: L, C, R]
    
    FrontProc & Delay10 & LPF7k --> BassRedir{LFE Mode: bass?}
    BassRedir -- Yes --> LfeGen["LFE Filter (120 Hz LP) + Redirect from Front"]
    BassRedir -- No --> LfeSilent[LFE = 0.0 Silent]
    
    FrontProc & Delay10 & LPF7k & LfeGen & LfeSilent --> TrimGain[Channel Trim + Makeup Gain + Master Gain]
    TrimGain --> OutMap[Output Channel Mapping: 5.1side / 5.1back]
    OutMap --> Writer[WavStreamWriter / RF64 Auto-Switch]
    Writer --> OutWAV[Output: 5.1 Float32 WAV @ 48kHz]
```

---

## 3. ตารางสมการ Decode ทุกโหมด (Mathematical Decoding Specifications)

### 3.1 ตารางสรุปโหมดและชุดสมการ
| โหมด (`--mode`) | ชุดสัมประสิทธิ์ | เซอร์ราวด์เอาต์พุต | ป้ายกำกับ | ที่มาและเอกสารอ้างอิง |
|---|---|---|---|---|
| `ds` | Passive Matrix | โมโน $S = 0.707\cdot(Rt - Lt) \rightarrow$ LPF 7k $\rightarrow$ ดีเลย์ 20ms $\rightarrow Ls=Rs$ (ไม่มี C) | **[E1 / E2]** | S2, S6, Dressler 1993 |
| `pl` | PL Emulation | โมโน $S$ ผ่าน servo Steering $\rightarrow$ LPF 7k $\rightarrow$ ดีเลย์ 10–20ms $\rightarrow Ls=Rs$ | **[E2]** | S2, S3 (US 7,280,664 B2) |
| `pl2` | Standard | สเตอริโอ $Ls, Rs$ ผ่าน servo Steering เต็มรูปแบบ ไม่มี LPF, ดีเลย์ 10ms | **[E2]** | S1, S3 (US 7,280,664 B2) |
| `pl2music` | Standard | สเตอริโอ $Ls, Rs$ มี HF shelf อ่อน ๆ ไม่มีดีเลย์ (พารามิเตอร์ควบคุม = เฟส 2) | **[E2]** | S1 |

### 3.2 สมการ Passive Decode ฐาน (Fallback และ `--steering off`) [E1]
เมื่อปิดระบบ Steering หรือในส่วนคงที่ ($M_{fix}$):
1. **Dolby Surround (`ds`):**
   $$L = Lt, \quad R = Rt, \quad C = 0 \text{ (Phantom)}, \quad S = \frac{1}{\sqrt{2}}(Rt - Lt), \quad Ls = Rs = S$$
   *หมายเหตุเฟส:* สัญญาณ $S$ ที่ได้จะตามหลังต้นฉบับ $90^\circ$ ซึ่งเป็นพฤติกรรมธรรมชาติของวงจร Passive Analog Matrix [E2/S2]
2. **Pro Logic II Passive (`pl2 --steering off`):**
   ใช้ค่าแถวคงที่ $M_{fix}$ ของชุด Standard (ตาม S3):
   $$L = 0.74\cdot Lt$$
   $$C = 0.524\cdot Lt + 0.524\cdot Rt$$
   $$R = 0.74\cdot Rt$$
   $$Ls = 0.76\cdot Lt - 0.17\cdot Rt$$
   $$Rs = -0.17\cdot Lt + 0.76\cdot Rt$$
3. **การวิเคราะห์ Crosstalk ของ Passive Matrix PLII [E1]:**
   เมื่อป้อนสัญญาณ $Ls_{in}$ บริสุทธิ์ผ่าน Encoder ($Lt = -a\cdot Ls^*, Rt = b\cdot Ls^*$ โดย $a=0.871, b=0.490$):
   - ช่อง $Ls_{dec} = 0.76(-a) - 0.17(b) = -0.662 - 0.083 = -0.745$
   - ช่อง $Rs_{dec} = -0.17(-a) + 0.76(b) = +0.148 + 0.372 = +0.520$
   - อัตราการแยกช่องสัญญาณ (Separation):
     $$20\log_{10}\left(\frac{0.745}{0.520}\right) \approx 3.12\text{ dB}$$
   *ข้อสรุป E1:* ในโหมด Passive การแยกช่อง $Ls/Rs$ ทำได้เพียงประมาณ **$3.1\text{ dB}$** เท่านั้น จึงจำเป็นอย่างยิ่งที่ต้องมี **Active Steering Servo** เพื่อขยายความต่างระดับเสียงให้ได้ $>30\text{ dB}$ ตามเกณฑ์มาตรฐาน

---

## 4. รายละเอียดสถาปัตยกรรม Steering Servo (ตามสิทธิบัตร Fosgate S3)

### 4.1 สัญญาณเสียงอินพุตและ Look-Ahead Delay [E2]
1. สัญญาณอินพุต $Lt, Rt$ ถูกลดระดับลง $-3.0\text{ dB}$ (คูณด้วย $10^{-3/20} \approx 0.707946$) เพื่อป้องกันการคลิป (Overload Clipping) ในกระบวนการบวกสัญญาณภายในเมทริกซ์ ได้เป็น $Lt', Rt'$
2. เส้นทางเสียงหลัก (Audio Path) ผ่าน Delay Line ขนาด **$5.0\text{ ms}$ (240 ตัวอย่างที่ 48 kHz)** เพื่อให้เส้นทางควบคุม (Control Path) มีเวลาตรวจจับและคำนวณเกนล่วงหน้า (Look-Ahead) ก่อนที่สัญญาณเสียงชั่วครู่ (Transient) จะมาถึงเมทริกซ์

### 4.2 การสกัดสัญญาณแกนหลัก (Axis Signals) [E2]
สกัดแกนทิศทาง 2 แกนตั้งฉากกัน:
- **แกนซ้าย-ขวา (Left/Right Axis):** คู่สัญญาณ $(Lt', Rt')$
- **แกนหน้า-หลัง (Front/Back Axis):** คู่สัญญาณ $(Ft, Bt)$ โดย:
  $$Ft = \frac{Lt' + Rt'}{2}, \quad Bt = \frac{Rt' - Lt'}{2}$$

### 4.3 ตัวกรองสัญญาณควบคุม BPF (Control Path BPF) [E2]
สัญญาณควบคุมผ่านตัวกรอง Band-Pass เพื่อตัดความถี่ต่ำมากและสูงมากที่อาจรบกวนการตัดสินใจทิศทาง:
- **High-Pass Section:** 2 Poles / 2 Zeros
  - Zeros: $0\text{ Hz}$ และ $641\text{ Hz}$
  - Poles: $788\text{ Hz}$ และ $1878\text{ Hz}$
  - แปลงเป็น Digital SOS ด้วย **Bilinear Transformation**
- **Low-Pass Section:** 2 Poles ที่ความถี่ $13,466\text{ Hz}$
  - แปลงเป็น Digital SOS ด้วย **Bilinear Transformation พร้อม Pre-warping ที่ $-3\text{ dB}$ ($13,466\text{ Hz}$)**
- ตัวกรองนี้ทำงานเฉพาะใน Control Path เท่านั้น **ไม่มีผลต่อคุณภาพเนื้อเสียงใน Audio Path**

### 4.4 ลูป Servo ซ้าย-ขวา (LR) และ หน้า-หลัง (FB) [E2/E3]
ทำงานที่อัตรา Hop Size = **8 ตัวอย่าง** ($6,000\text{ Hops/sec}$ ที่ $48\text{ kHz}$):

```
       u_X = BPF( (1 - g_X) * X )       <-- สัญญาณ Intermediate
           │
           ▼
       e_X = Smooth_800ms( |u_X| )      <-- Rectification & Envelope
           │
           ▼
       l_X = A1 * log2( A0 * e_X )      <-- Logarithmic Compression
           │
           ▼
       d = l_X - l_Y                    <-- Difference (Error Signal)
           │
           ▼
       v = A2 * d -> Piecewise Linear   <-- Non-linear Variable Gain
           │
           ▼
       LR (หรือ FB) = Smooth_2.5ms( v ) <-- Symmetrical Fast Smoothing
           │
           ├────────────────────────────┐
           ▼                            ▼
  g_X = 2^(A4 + A3 * LR)       g_Y = 2^(A4 - A3 * LR)
```

#### ค่าคงที่และสูตรคณิตศาสตร์ที่ตรวจสอบตรงกับสิทธิบัตร US 7,280,664 B2:
1. **$A_0$ ถึง $A_4$ [E2]:**
   - $A_0 = 0.707106781 \times 0.000022 \approx 1.555635 \times 10^{-5}$
   - $A_1 = \frac{3.182732}{4.0} = 0.795683$ *(แก้ไขข้อผิดพลาด OCR ของผู้ใช้เดิมที่อ่านเป็น /40)*
   - $A_2 = 32 \times 4 = 128$
   - $A_3 = -0.2375$
   - $A_4 = -0.2400$
2. **ฟังก์ชันไม่เชิงเส้นแบบ 3 ช่วง (Piecewise-Linear Transfer Function) [E2]:**
   $$\text{output} = \begin{cases} 
   2.871432 \times \text{input} & \text{เมื่อ } |\text{input}| < 0.240714 \\
   0.406707 \times \text{input} + 0.593293 & \text{เมื่อ } \text{input} \ge 0.240714 \\
   0.406707 \times \text{input} - 0.593293 & \text{เมื่อ } \text{input} \le -0.240714 
   \end{cases}$$
   *(ความต่อเนื่องสมบูรณ์ที่รอยต่อ $\pm 0.240714$: $0.240714 \times 2.871432 = 0.6912$, และ $0.240714 \times 0.406707 + 0.593293 = 0.6912$)*
3. **การแก้จุดกำกวมของ Negative Feedback ใน C6 [E3]:**
   - ในสิทธิบัตรระบุสัญญาณ Intermediate เป็น $(1 - g_L)\cdot Lt'$ และ $(1 - g_R)\cdot Rt'$
   - หาก $Lt > Rt \Rightarrow d = l_L - l_R > 0 \Rightarrow LR > 0$
   - ถ้า $g_L = 2^{A4 + A3\cdot LR}$ เนื่องจาก $A_3 = -0.2375 < 0$ จะทำให้ $g_L$ ลดลง ซึ่งจะส่งผลให้ตัวคูณ $(1 - g_L)$ เพิ่มขึ้น (ขยาย $Lt'$ ให้ดังขึ้น ซึ่งเป็น Positive Feedback ที่ไม่เสถียร)
   - **ทางเลือกที่พิสูจน์แล้วว่าเสถียร (Direct Gain Representation):**
     กำหนดให้ $h_L = 2^{A4 + A3\cdot LR}$ ทำหน้าที่เป็นตัวคูณ Direct Attenuator โดยตรงบน $Lt'$ ($u_L = \text{BPF}(h_L \cdot Lt')$) ซึ่งเมื่อ $Lt > Rt$, $h_L$ จะลดลงกดสัญญาณ $Lt'$ ให้เล็กลงเท่ากับ $Rt'$ อย่างเสถียรสมบูรณ์
     และกำหนดค่าเกนที่ส่งไปยัง Adaptive Matrix เป็น $g_L = 1 - h_L$ (หรือใช้นิยามกลับเครื่องหมาย $A_3 = +0.2375$ หาก $g_L$ นิยามเป็นตัวกด) ซึ่งจะถูกยืนยันผ่านการจำลอง (Simulation Test) ก่อนล็อกในโค้ด

### 4.5 ลอจิกควบคุมเซอร์ราวด์ด้านหลัง ($g_{LB}, g_{RB}$) [E2]
สร้างจากสัญญาณความผิดพลาด $LR$ และ $FB$:
1. คำนวณสัญญาณควบคุมเชิงมุม:
   $$p_1 = \max(-LR, \, B_0 \cdot FB)$$
   $$p_2 = \max(LR, \, B_0 \cdot FB)$$
2. คูณสเกลและจำกัดขอบเขตไม่ให้เป็นบวก (Clamp to zero):
   $$q_1 = \min(0.0, \, p_1 \cdot B_1)$$
   $$q_2 = \min(0.0, \, p_2 \cdot B_1)$$
3. ปรับสเกลและเลื่อน Offset:
   $$r_1 = q_1 \cdot B_2 + B_3$$
   $$r_2 = q_2 \cdot B_2 + B_3$$
4. ยกกำลังฐานสอง (Antilog) และปรับช่วงสุดท้าย:
   $$g_{LB} = (2^{r_1} + B_4) \cdot B_5$$
   $$g_{RB} = (2^{r_2} + B_4) \cdot B_5$$
5. **ค่าคงที่ $B_0$ ถึง $B_5$ ที่ตรวจสอบตรงกับสิทธิบัตร [E2]:**
   - $B_0 = 0.79$ (กำหนดตำแหน่งมุมที่เกนเซอร์ราวด์ขึ้นสูงสุด $\approx 31^\circ$)
   - $B_1 = 1.451$ *(ตรวจสอบแล้วในสิทธิบัตรพิมพ์ 1.451 ไม่ใช่ 1451)*
   - $B_2 = -0.15415$
   - $B_3 = -0.15415$ *(ข้อสังเกต: หาก $B_3 = 0.0$ เกนสูงสุดเมื่อ $q=0$ จะได้ $(2^0 - 0.21927/1.21927)\times 1.21927 = 1.0000$ พอดี ในระบบจะเปิดให้ปรับค่านี้ผ่าน `--servo-param`)*
   - $B_4 = -\frac{0.21927}{1.21927} \approx -0.179838$
   - $B_5 = 1.21927$

---

## 5. ตารางสัมประสิทธิ์เมทริกซ์ 5×2 ของสิทธิบัตร Fosgate S3 [E2]
สมการเอาต์พุต 5 ช่องสัญญาณ (ตัดแถว $Bs$ แถวที่ 5 ออก):
$$y(n) = M(n) \cdot \begin{bmatrix} Lt'(n) \\ Rt'(n) \end{bmatrix}$$
โดยที่:
$$M(n) = M_{fix} + g_L(n)\cdot M_{gL} + g_R(n)\cdot M_{gR} + g_F(n)\cdot M_{gF} + g_B(n)\cdot M_{gB} + g_{LB}(n)\cdot M_{gLB} + g_{RB}(n)\cdot M_{gRB}$$

### 5.1 ชุด Standard Coefficients (ใช้สำหรับ `pl2` และ `pl2music`) [E2]
*(ดึงข้อมูลโดยตรงจาก Table ในสิทธิบัตร US 7,280,664 B2)*

| เมทริกซ์ | L (Lt, Rt) | C (Lt, Rt) | R (Lt, Rt) | Ls (Lt, Rt) | Rs (Lt, Rt) |
|---|---|---|---|---|---|
| **$M_{fix}$** | `[ 0.7400,  0.0000]` | `[ 0.5240,  0.5240]` | `[ 0.0000,  0.7400]` | `[ 0.7600, -0.1700]` | `[-0.1700,  0.7600]` |
| **$M_{gL}$**  | `[ 0.3200,  0.0000]` | `[-0.5400,  0.0000]` | `[ 0.0000,  0.0000]` | `[-0.7720,  0.0000]` | `[ 0.1920,  0.0000]` |
| **$M_{gR}$**  | `[ 0.0000,  0.0000]` | `[ 0.0000, -0.5400]` | `[ 0.0000,  0.3200]` | `[ 0.0000,  0.1920]` | `[ 0.0000, -0.7720]` |
| **$M_{gF}$**  | `[-0.3813, -0.3813]` | `[ 0.2240,  0.2240]` | `[-0.3813, -0.3813]` | `[-0.2930, -0.2930]` | `[-0.2930, -0.2930]` |
| **$M_{gB}$**  | `[-0.3849,  0.3849]` | `[ 0.0000,  0.0000]` | `[ 0.3849, -0.3849]` | `[ 0.0697, -0.0697]` | `[-0.0697,  0.0697]` |
| **$M_{gLB}$** | `[-0.2850,  0.2850]` | `[ 0.0000,  0.0000]` | `[ 0.0000,  0.0000]` | `[ 0.3510, -0.3510]` | `[ 0.3700, -0.3700]` |
| **$M_{gRB}$** | `[ 0.0000,  0.0000]` | `[ 0.0000,  0.0000]` | `[ 0.2850, -0.2850]` | `[-0.3700,  0.3700]` | `[-0.3510,  0.3510]` |

### 5.2 ชุด Pro Logic Emulation Coefficients (ใช้สำหรับ `pl`) [E2]

| เมทริกซ์ | L (Lt, Rt) | C (Lt, Rt) | R (Lt, Rt) | Ls (Lt, Rt) | Rs (Lt, Rt) |
|---|---|---|---|---|---|
| **$M_{fix}$** | `[ 0.7400,  0.0000]` | `[ 0.5240,  0.5240]` | `[ 0.0000,  0.7400]` | `[ 0.5370, -0.5370]` | `[ 0.5370, -0.5370]` |
| **$M_{gL}$**  | `[ 0.3200,  0.0000]` | `[-0.5400,  0.0000]` | `[ 0.0000,  0.0000]` | `[-0.5460,  0.0000]` | `[-0.5460,  0.0000]` |
| **$M_{gR}$**  | `[ 0.0000,  0.0000]` | `[ 0.0000, -0.5400]` | `[ 0.0000,  0.3200]` | `[ 0.0000,  0.5460]` | `[ 0.0000,  0.5460]` |
| **$M_{gF}$**  | `[-0.3811, -0.3811]` | `[ 0.2250,  0.2250]` | `[-0.3811, -0.3811]` | `[ 0.0000,  0.0000]` | `[ 0.0000,  0.0000]` |
| **$M_{gB}$**  | `[-0.3811,  0.3811]` | `[ 0.0000,  0.0000]` | `[ 0.3811, -0.3811]` | `[ 0.0000,  0.0000]` | `[ 0.0000,  0.0000]` |
| **$M_{gLB}$** | `[ 0.0000,  0.0000]` | `[ 0.0000,  0.0000]` | `[ 0.0000,  0.0000]` | `[ 0.0000,  0.0000]` | `[ 0.0000,  0.0000]` |
| **$M_{gRB}$** | `[ 0.0000,  0.0000]` | `[ 0.0000,  0.0000]` | `[ 0.0000,  0.0000]` | `[ 0.0000,  0.0000]` | `[ 0.0000,  0.0000]` |

*ข้อสังเกตเชิงคณิตศาสตร์:* ในชุด PL Emulation แถว $Ls$ และ $Rs$ มีสัมประสิทธิ์เท่ากันทุกตัว (`0.5370 Lt - 0.5370 Rt`) ส่งผลให้ได้สัญญาณเซอร์ราวด์เป็นโมโนอย่างแท้จริง และเมทริกซ์ $M_{gLB}, M_{gRB}$ เป็นศูนย์ทั้งหมด

---

## 6. การแมปช่องเสียง ดีเลย์ และ Latency รวม

### 6.1 ตารางแมปช่องเสียงและ Channel Mask ในไฟล์ WAV [E1]
ส่งออกเป็นไฟล์ **WAV 32-bit Float 6 ช่องสัญญาณ ที่ 48000 Hz** ลำดับมาตรฐาน (WAVE_FORMAT_EXTENSIBLE):
- ดัชนี 0: **FL** (Front Left)
- ดัชนี 1: **FR** (Front Right)
- ดัชนี 2: **FC** (Front Center)
- ดัชนี 3: **LFE** (Low Frequency Effects)
- ดัชนี 4: **SL หรือ BL** (Surround Left)
- ดัชนี 5: **SR หรือ BR** (Surround Right)

| ตัวเลือก `--out-layout` | แชนเนลมาสก์ (dwChannelMask) | บทบาทช่อง 4 และ 5 | รายละเอียด |
|---|---|---|---|
| `5.1side` (ค่าเริ่มต้น) | `0x060F` (FL\|FR\|FC\|LFE\|SL\|SR) | Side Left (SL), Side Right (SR) | มาตรฐานระบบโฮมเธียเตอร์ 5.1 สมัยใหม่ |
| `5.1back` | `0x003F` (FL\|FR\|FC\|LFE\|BL\|BR) | Back Left (BL), Back Right (BR) | มาตรฐานดั้งเดิมหรือเครื่องเล่นรุ่นเก่า |

### 6.2 การประมวลผลช่อง LFE [E1/E3]
- ค่าเริ่มต้น `--lfe-mode silent`: ช่อง LFE ถูกเติมด้วยค่า $0.0$ ตลอดทั้งไฟล์
- โหมด `--lfe-mode bass`:
  - รวมสัญญาณคู่หน้า: $M_{bass} = 0.5\cdot(L + R)$
  - ผ่าน 4th-order Butterworth Low-Pass Filter ที่ `--lfe-lp` (ค่าเริ่มต้น $120\text{ Hz}$)
  - คูณขยายด้วย `--lfe-gain` (ค่าเริ่มต้น $0.0\text{ dB}$) เข้าช่อง LFE
  - หากระบุ `--lfe-redirect`: หักลบสัญญาณเบสดังกล่าวออกจากช่อง $L, R, C$ ด้วย High-Pass Filter ความถี่เดียวกัน เพื่อรักษาพลังงานเสียงรวมไม่ให้บวมซ้ำซ้อน

### 6.3 Surround Filter และ Delay Budget [E2]
1. **Surround Band-Limit (LPF 7 kHz):**
   - โหมด `ds` และ `pl`: ผ่าน 4th-order Butterworth Low-Pass ที่ $7,000\text{ Hz}$ บนช่อง $Ls, Rs$ (สามารถปิดได้ด้วย `--surround-lpf off`)
   - โหมด `pl2`: ค่าเริ่มต้น Full-Range (ไม่กรอง LPF 7k)
2. **Surround Delay Line:**
   - ค่าเริ่มต้น: `pl` / `pl2` = **$10\text{ ms}$ (480 ตัวอย่าง)**, `ds` = **$20\text{ ms}$ (960 ตัวอย่าง)**
   - ปรับแต่งได้อิสระผ่าน `--surround-delay MS` (ช่วงที่ยอมรับได้: $0 - 100\text{ ms}$)
3. **การชดเชย Latency และขนาดไฟล์ [E1]:**
   - เส้นทางเสียงมี Look-Ahead Delay $5.0\text{ ms}$ (240 ตัวอย่าง)
   - ความหน่วงทั้งหมดจะถูก Flush ตอนสิ้นสุดไฟล์ เพื่อให้ **จำนวนตัวอย่างเอาต์พุตเท่ากับอินพุตเป๊ะ**

---

## 7. สเปกไฟล์นำเข้า ส่งออก และ RF64 Handling [E1]

### 7.1 การตรวจสอบอินพุต (Input Validation)
- ตรวจจับขนาดและข้อมูลช่องเสียงผ่าน Native WAV Parser และ fallback ด้วย `ffprobe` (JSON)
- **Mono (1 แชนเนล):** ปฏิเสธการทำงานและส่งกลับ **Exit Code 3** ทันที
- **มากกว่า 2 แชนเนล:** ส่งกลับ **Exit Code 3** พร้อมข้อความแนะนำให้ระบุคู่ช่องด้วย `--input-pair A,B` หรือให้ใช้ `6to2matrixencoder.py` ก่อน
- **2 แชนเนลที่ไม่ใช่ FL/FR:** แสดงคำเตือนและต้องการให้ยืนยันผ่าน `--layout "L,R"`
- **Sampling Rate อื่น (เช่น 44.1 kHz):** เรียกใช้งาน FFmpeg Resampler แปลงเป็น $48,000\text{ Hz}$ แบบ 32-bit Float อัตโนมัติ

### 7.2 โครงสร้าง WAV/RF64 Writer อัตโนมัติ
- ใช้โครงสร้าง `WavStreamWriter` เดียวกับ Encoder:
  - เขียนลงไฟล์ชั่วคราว `.part`
  - สำรองพื้นที่ส่วนหัวไว้ $512\text{ ไบต์}$
  - เมื่อขนาดข้อมูลเกิน $4\text{ GB} - 512\text{ ไบต์}$ ($0xFFFFFFFF$ ไบต์) จะเขียน Header เป็น **RF64** พร้อม `ds64` chunk (ขนาด 64-bit data size และ sample count) อัตโนมัติ
  - หากขนาดไม่เกิน $4\text{ GB}$ จะเขียน Header เป็น **RIFF/WAVE** พร้อม `WAVEFORMATEXTENSIBLE` ปกติ
  - ทำการ Flush และ Atomic Rename เป็นชื่อไฟล์เป้าหมายเมื่อการประมวลผลเสร็จสิ้นสมบูรณ์

---

## 8. กลยุทธ์การประมวลผลแบบบล็อก (Block Processing & State Continuity) [E1/E3]

### 8.1 การรักษา State ข้ามบล็อก (Block-Invariance)
ระบบต้องให้ผลลัพธ์ระดับบิตเทียบเท่ากันทุกประการไม่ว่าจะรันด้วย `--block-size 4096`, `65536` หรือขนาดใดก็ตาม (ความต่างไม่เกิน $-140\text{ dBFS}$):
1. **ตัวกรอง IIR (BPF, LPF, LFE):** เก็บรักษาเวกเตอร์สถานะ `zi` (float64) ของแต่ละ Section ข้ามบล็อก
2. **ตัวเฉลี่ย Smoothing Filter ($800\text{ ms}$ และ $2.5\text{ ms}$):**
   - คำนวณแบบ 1st-order IIR: $y(n) = (1 - \alpha)\cdot x(n) + \alpha\cdot y(n-1)$
   - เก็บค่าตัวอย่างสุดท้าย $y[-1]$ ไว้เป็นค่าเริ่มต้นของบล็อกถัดไป
3. **การนับ Hop และตำแหน่งกริด:**
   - กริดของ Hop (8 ตัวอย่าง) อิงตาม **ดัชนีตัวอย่างสัมบูรณ์ (Absolute Sample Index)** นับจากตัวอย่างที่ 0 ของไฟล์
   - ตัวอย่างที่เหลือเศษท้ายบล็อก ($N \pmod 8$) จะถูกพักไว้ใน Buffer เพื่อนำไปต่อหัวบล็อกถัดไป
4. **ความจำ (Memory Footprint):**
   - ประมวลผลแบบ Block Streaming โดยมีหน่วยความจำใช้งานคงที่ $< 150\text{ MB}$ ตลอดการประมวลผลไฟล์ขนาดใหญ่ (แม้รันไฟล์ยาว 3 ชั่วโมง)

---

## 9. การปรับระดับเสียงและการจัดการ Headroom [E1]
1. **Makeup Gain (`--makeup-gain DB`):**
   - ค่าเริ่มต้น: `0.0 dB` (หรือเสนอ `+2.6 dB` เพื่อชดเชยการลดเกน $-3\text{ dB}$ ฝั่งอินพุตและสัมประสิทธิ์ $0.74$)
2. **Channel Trim (`--trim CH=dB`):**
   - ปรับแต่งเกนแยกอิสระรายช่องเอาต์พุต: `L`, `C`, `R`, `Ls`, `Rs`, `LFE` (ช่วง $-96.0$ ถึง $+12.0\text{ dB}$, หรือ `-inf` เพื่อ Mute)
3. **Headroom Management (`--headroom {none,static,peak}`):**
   - `none`: ไม่ลดระดับเสียง รายงาน Peak ของแต่ละช่องในสรุปจบงาน
   - `static`: ลดระดับเสียงล่วงหน้าตาม Worst-case สัมประสิทธิ์เมทริกซ์
   - `peak`: ประมวลผลแบบ Two-Pass (สแกนหา True Peak รอบแรก แล้วปรับลดเกนรอบสอง)

---

## 10. สเปก CLI และ HELP ฉบับร่าง (CLI Arguments & Thai Help)

### 10.1 ข้อความช่วยเหลือ (Draft HELP Text)
```text
โปรแกรมถอดรหัสสัญญาณเสียง Matrix Lt/Rt 2.0 เป็น 5.1 เซอร์ราวด์ (Float32 48 kHz)
พัฒนาขึ้นตามสถาปัตยกรรม Active Steering Matrix ของสิทธิบัตรสาธารณะ US 7,280,664 B2 (James W. Fosgate)
หมายเหตุ: โปรแกรมนี้เป็นการพัฒนาอิสระตามเอกสารสาธารณะ ไม่ได้รับการรับรองจาก Dolby Laboratories
เครื่องหมายการค้า Dolby, Pro Logic, Dolby Surround เป็นของ Dolby Laboratories

ตารางโหมดการถอดรหัส (--mode):
  ds        Dolby Surround ดั้งเดิม (Passive Matrix, S โมโน LPF 7kHz, Delay 20ms, Phantom Center)
  pl        Dolby Pro Logic (PL Emulation Active Steering, S โมโน LPF 7kHz, Delay 10ms, Center อิสระ)
  pl2       Dolby Pro Logic II Movie [ค่าเริ่มต้น] (Standard Active Steering เต็มรูปแบบ, Ls/Rs สเตอริโอ Full-range)
  pl2music  Dolby Pro Logic II Music (Standard Active Steering พร้อม HF Shelf, ไม่มี Delay เซอร์ราวด์)

ระดับความแม่นยำทางวิศวกรรม:
  [E1] ยืนยันได้จากคณิตศาสตร์: Passive Matrix, WAV RF64, Channel Mask, Peak Normalization
  [E2] เอกสารสาธารณะ/สิทธิบัตร: วงจร Fosgate Servo Feedback, BPF 788-1878Hz, ค่าคงที่ A0-A4, B0-B5
  [E3] พัฒนาขึ้นใหม่: ลอจิกจำลองความเสถียร Negative Feedback, การสร้างสัญญาณ LFE จากเบส

ตัวอย่างคำสั่งใช้งาน:
  1. ถอดรหัสไฟล์ Lt/Rt ทั่วไปเป็น 5.1 ด้วย Dolby Pro Logic II (ค่าเริ่มต้น):
     python lib/2to6matrixdecoder.py -i movie_ltrt.wav -o movie_51.wav

  2. ถอดรหัสไฟล์คอนเสิร์ตด้วยโหมด Pro Logic II Music:
     python lib/2to6matrixdecoder.py -i music_ltrt.wav -o music_51.wav --mode pl2music

  3. ถอดรหัส Pro Logic ดั้งเดิม พร้อมจำลอง Delay เซอร์ราวด์ 20 ms:
     python lib/2to6matrixdecoder.py -i classic_ltrt.wav --mode pl --surround-delay 20

  4. ถอดรหัสแบบ Passive เมทริกซ์ล้วน (ไม่เปิดระบบ Active Steering):
     python lib/2to6matrixdecoder.py -i input.wav --steering off

  5. สกัดเบสความถี่ต่ำกว่า 120 Hz เข้าช่อง LFE พร้อมปรับชดเชยเกน Center -1.5 dB:
     python lib/2to6matrixdecoder.py -i track.wav --lfe-mode bass --lfe-lp 120 --trim C=-1.5

  6. ตรวจสอบการกำหนดค่าและสัมประสิทธิ์เมทริกซ์โดยไม่เขียนไฟล์ (Dry-run):
     python lib/2to6matrixdecoder.py -i input.wav --dry-run

ข้อจำกัดสำคัญ:
  - วงจรลดเสียงรบกวน Dolby NR (Modified B-type) เป็นสิทธิ์เฉพาะของ Dolby โปรแกรมนี้จึงปิดไว้ (--nr off)
    หากนำไฟล์ที่ผ่านการเข้ารหัส Dolby NR จริงมาถอด เสียงแหลมอาจมีความสว่างมากกว่าปกติเล็กน้อย
  - ในไฟล์ที่สร้างจาก Encoder โหมด 180° แบบดั้งเดิม (pl2s ที่ Ls=Rs) จะไม่สามารถแยกมิติสเตอริโอของ Ls/Rs ออกจากกันได้

รหัสสถานะการสิ้นสุด (Exit Codes):
  0   สำเร็จเรียบร้อย
  1   ข้อผิดพลาดทั่วไป
  2   การกำหนดอาร์กิวเมนต์ไม่ถูกต้อง
  3   ข้อมูลอินพุตไม่รองรับ (ไฟล์โมโน หรือมากกว่า 2 ช่อง)
  4   FFmpeg ทำงานล้มเหลว หรือไม่พบเครื่องมือ
  5   ข้อผิดพลาดในการอ่าน/เขียนไฟล์ (I/O Error)
  130 ผู้ใช้ยกเลิกการทำงาน (Ctrl+C)
```

### 10.2 รายการพารามิเตอร์ CLI ทั้งหมด
`-i, --input` · `-o, --output` · `-y, --yes` · `--mode {ds,pl,pl2,pl2music}` · `--steering {on,off}` · `--steer-engine {fosgate,passive,pairinv}` · `--coef-set {auto,standard,pl-emulation}` · `--servo-impl {fast,exact}` · `--lookahead MS` (5.0) · `--hop N` (8) · `--servo-env-tc MS` (800) · `--servo-fast-tc MS` (2.5) · `--servo-param NAME=VALUE` · `--makeup-gain DB` · `--round-trip-phase` · `--surround-lpf {off,7k}` · `--surround-delay MS` · `--surround-hp` · `--cutoff-type {hill,brick}` · `--slope` · `--nr {off,b}` · `--lfe-mode {silent,bass}` · `--lfe-lp` · `--lfe-gain` · `--lfe-redirect` · `--out-layout {5.1side,5.1back}` · `--trim CH=dB` · `--headroom {none,static,peak}` · `--out-gain DB` · `--block-size N` · `--ffmpeg PATH` · `--dry-run` · `--json` · `-v, --verbose` · `-q, --quiet` · `--version` · `--list-modes`

---

## 11. แผนการทดสอบและความเสี่ยง (Testing Plan & Risk Matrix)

### 11.1 แผนการทดสอบ (Verification Tests)
1. **Matrix Coefficients & Unit Tests:** ตรวจสอบความถูกต้องของค่าเมทริกซ์ $5\times 2$ ทั้ง 7 ตัวของ Standard และ PL Emulation
2. **Channel Separation Matrix (5×5 dB):**
   - ป้อนสัญญาณเดี่ยวทีละช่อง ($L, C, R, Ls, Rs$) ผ่าน Encoder แล้วส่งเข้า Decoder
   - วัดระดับพลังงานเอาต์พุตทั้ง 5 ช่อง เพื่อสร้างตารางการรั่วข้ามช่อง (Separation Matrix)
   - เปรียบเทียบระหว่าง `--steering off` (เป้าหมาย $\approx 3\text{ dB}$) กับ `--steering on` (เป้าหมาย $> 25 - 30\text{ dB}$ ตามเกณฑ์ S2)
3. **Corner Cases Test:**
   - $Lt = Rt$ (Dual Mono): ต้องถอดออกเฉพาะช่อง $C$ เต็มกำลัง และช่อง $L, R, Ls, Rs \approx -\infty\text{ dB}$
   - $Lt = -Rt$ (Out of Phase): ต้องถอดออกเฉพาะช่อง $Ls, Rs$ เท่ากัน และช่อง $C \approx -\infty\text{ dB}$
   - สัญญาณเงียบสนิททั้งสองช่อง หรือเงียบช่องใดช่องหนึ่ง: ระบบต้องคงที่ ไม่เกิด $NaN$, ไม่เกิด $Inf$, ไม่หารศูนย์
4. **Angular Panning Sweep Test:**
   - กวาดมุมเสียงรอบทิศทาง $\alpha \in [0^\circ, 360^\circ]$ ตรวจสอบความเรียบต่อเนื่องของเกน $g_L, g_R, g_F, g_B, g_{LB}, g_{RB}$ เทียบกับกราฟในสิทธิบัตร S3 (FIG. 7, 8, 12)
   - ตรวจสอบว่าสัญญาณเสียงที่ได้ไม่มีเสียงคลิก (Click/Pop) หรือ Zipper Noise
5. **Round-Trip Test:**
   - นำไฟล์เสียงทดสอบ 5.1 $\rightarrow$ `6to2matrixencoder.py` $\rightarrow$ `2to6matrixdecoder.py`
   - ตรวจวัดความเพี้ยน, เฟสสัมพันธ์, และระดับเสียงแต่ละช่อง
6. **Block-Invariance Test:**
   - ประมวลผลไฟล์เดียวกันด้วย `--block-size 4096` และ `--block-size 65536`
   - ตรวจสอบผลต่างของสัญญาณเสียง (Residual Error ต้อง $<-140\text{ dBFS}$)
7. **Listening Test Plan (การฟังทดสอบจริง):**
   - **Track 1 (Dialogue Test):** เสียงพูดสนทนาตรงกลาง ตรวจสอบว่ารั่วไปเซอร์ราวด์หรือไม่
   - **Track 2 (Ambience/Panned Effects):** เสียงฝนตกหรือเสียงยานพาหนะวิ่งวนรอบทิศ
   - **Track 3 (Wide Stereo Music):** เพลงสเตอริโอทั่วไป ตรวจสอบความเป็นธรรมชาติของเวทีเสียงด้านหน้า ไม่ถูกดึงเข้า Center จนแบน

### 11.2 รายการความเสี่ยงและมาตรการป้องกัน (Risks & Mitigations)
| ความเสี่ยง | ระดับ | มาตรการป้องกัน |
|---|---|---|
| Servo Feedback เกิดความไม่เสถียร (Oscillation) | ปานกลาง | จำกัดช่วงของเกน $g_k \in [0.0, 1.0]$, ใส่ Floor ป้องกัน $\log_2(0)$ ที่ $-120\text{ dBFS}$ และตรวจสอบขั้ว Negative Feedback ในการจำลอง |
| ประสิทธิภาพความเร็วของ Python Loop ช้าเมื่อรัน Exact Mode | สูง | รองรับ `--servo-impl fast` เป็นค่าเริ่มต้น (กรอง BPF ระดับบล็อก) และรองรับ Numba (Optional JIT) เพื่อดันความเร็ว $> 100\times$ Realtime |
| ไฟล์ขนาดใหญ่เกิด Memory Leak | ต่ำ | ประมวลผลแบบ Block Generator พร้อม Garbage Collection ชัดเจน |

---

## 12. สรุปรายการคำถามเปิดเพื่อการตัดสินใจ (Open Questions for Approval)
*(ขอความเห็นชอบและทิศทางในการตัดสินใจก่อนเริ่มการเขียนโค้ด)*

1. **การอนุญาตให้ใช้ `numba` (Optional Dependency):**  
   *ข้อเสนอ:* อนุญาตให้ใช้ `numba` หากตรวจพบในระบบ เพื่อให้ลูปตัวอย่างใน Servo Exact Mode ประมวลผลได้เร็วระดับ $>100\times$ Realtime แต่หากผู้ใช้ไม่มี `numba` ให้รันโหมด Pure Python / Fast Mode ได้ตามปกติ
2. **การยอมรับสเปกสิทธิบัตร Fosgate S3 (US 7,280,664 B2) เป็นแกนอ้างอิงหลัก:**  
   *ข้อเสนอ:* ยืนยันใช้โครงสร้าง Feedback Servo และตารางสัมประสิทธิ์เมทริกซ์ 5x2 ตาม S3 เป็นค่าเริ่มต้นของระบบ
3. **วงจร Pro Logic ดั้งเดิมแบบ Feed-Forward (S8 / US 4,799,260):**  
   *ข้อเสนอ:* สำหรับโหมด `pl` ในเฟสแรกจะใช้ชุดสัมประสิทธิ์ PL Emulation ภายใต้โครงสร้าง Fosgate Servo ตามที่ระบุใน S3 และยกยอด Feed-Forward ดั้งเดิมของ S8 ไว้เป็นทางเลือกเสริมในเฟสถัดไป
4. **พารามิเตอร์ของโหมด Music (Center Width, Dimension, Panorama):**  
   *ข้อเสนอ:* ในเฟสแรก โหมด `pl2music` จะใช้ชุดสัมประสิทธิ์ Standard พร้อมปิด Surround Delay ตามเอกสาร S1 ส่วนตัวแปรปรับแต่ง Center Width, Dimension, Panorama ขอยกยอดไปพัฒนาในเฟสที่ 2
5. **ระบบ Auto-Balance (`--input-balance auto`):**  
   *ข้อเสนอ:* เนื่องจากอัลกอริทึม Auto-balance ของ Dolby เป็นความลับทางการค้า จึงเสนอให้กำหนดเป็น [E3] ปิดไว้เป็นค่าเริ่มต้น (`off`) หากเปิดใช้จะใช้ตัวประมาณค่า RMS ความต่างระยะยาว ($>2\text{ วินาที}$) ชดเชยสูงสุดไม่เกิน $\pm 3\text{ dB}$
6. **ไฟล์เสียงอ้างอิงสำหรับการวัดเทียบ:**  
   *คำถาม:* ทางคุณมีไฟล์เสียงคู่เปรียบเทียบ (สัญญาณ Lt/Rt ดิบ และผลลัพธ์ 5.1 ที่ถอดรหัสจากเครื่องรับ AV Receiver / ฮาร์ดแวร์จริงของ Dolby) สำหรับให้ใช้ทดสอบความแม่นยำทางสถิติหรือไม่
