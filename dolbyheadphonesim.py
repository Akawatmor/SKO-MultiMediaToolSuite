#!/usr/bin/env python3
# simulate_dolby_headphone.py

import numpy as np
from scipy.signal import fftconvolve

def simulate_binaural_headphone(
    data_51: np.ndarray, 
    hrir_dict: dict[str, np.ndarray]
) -> np.ndarray:
    """
    data_51  : Array [N_samples, 6] (FL, FR, C, LFE, Ls, Rs) ชนิด float32
    hrir_dict: Dictionary เก็บ Impulse Response 10 ตัว (FL_L, FL_R, FR_L, FR_R, ...)
    """
    fl, fr, c, lfe, ls, rs = [data_51[:, i] for i in range(6)]

    # 1. คำนวณสัญญาณหูซ้าย (Left Ear) ผ่าน FFT Convolution
    left_ear = (
        fftconvolve(fl, hrir_dict['FL_L'], mode='full') +
        fftconvolve(fr, hrir_dict['FR_L'], mode='full') +
        fftconvolve(c,  hrir_dict['C_L'],  mode='full') +
        fftconvolve(ls, hrir_dict['Ls_L'], mode='full') +
        fftconvolve(rs, hrir_dict['Rs_L'], mode='full')
    )

    # 2. คำนวณสัญญาณหูขวา (Right Ear) ผ่าน FFT Convolution
    right_ear = (
        fftconvolve(fl, hrir_dict['FL_R'], mode='full') +
        fftconvolve(fr, hrir_dict['FR_R'], mode='full') +
        fftconvolve(c,  hrir_dict['C_R'],  mode='full') +
        fftconvolve(ls, hrir_dict['Ls_R'], mode='full') +
        fftconvolve(rs, hrir_dict['Rs_R'], mode='full')
    )

    # ตัดความยาวให้เท่ากับสัญญาณต้นฉบับ
    n_samples = len(data_51)
    binaural_stereo = np.column_stack((left_ear[:n_samples], right_ear[:n_samples]))
    
    # Static Normalization
    max_p = np.max(np.abs(binaural_stereo))
    if max_p > 1.0:
        binaural_stereo /= max_p

    return binaural_stereo.astype(np.float32)