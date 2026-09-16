"""
驗證碼（VerifyCode）辨識模組
============================

太子學舍電力計費平台 (dormtopup.prince.com.tw) 的登入驗證碼，是一張小圖片，
內容是一道簡單的算式，例如：

    10 x 4 = ?
    13 + 22 = ?
     9 + 8 = ?

圖片本身有很多細小的噪點/雜線干擾 OCR，但文字（數字、運算符號、等號、問號）
都是同一種偏藍色調，比背景明顯偏藍且偏暗，所以我們用「顏色 + 連通元件大小」
先把雜訊濾掉，再丟給 tesseract 辨識。

因為單次辨識不一定 100% 準確，這裡採用「多種前處理參數 + 嚴格格式驗證」的
策略：只要辨識結果不完全符合「數字 運算符 數字 = ?」這種格式，就直接放棄、
換一張新的驗證碼重試，避免把明顯錯誤的答案送出去。即使格式正確但數字辨識錯誤，
送出後系統會回覆「驗證碼錯誤」，程式一樣會自動換圖重試（見 dorm_client.py）。

需求：
    - tesseract-ocr（系統套件，需要另外安裝，見 README）
    - pytesseract, pillow, numpy, scipy（見 requirements.txt）
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image
from scipy import ndimage

try:
    import pytesseract
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "缺少 pytesseract，請先安裝：pip install pytesseract，"
        "並確認系統已安裝 tesseract-ocr 執行檔（見 README 的安裝說明）。"
    ) from exc


# 允許的運算符號：x（乘法，目前實際也看過用小寫 x）、+（加法）。
# 額外保留 - 與 * 以防之後平台換成其他符號。
_OP_MAP = {
    "x": "*",
    "X": "*",
    "*": "*",
    "+": "+",
    "-": "-",
    "÷": "/",
    "/": "/",
}

# 辨識結果必須整串符合這個格式，才會被接受： 例如 "10x4=?"、"13+22=?"
_PATTERN = re.compile(r"^(\d{1,3})\s*([x+\-*])\s*(\d{1,3})\s*=\s*\?$")

# 多組前處理參數的組合 (min_component_size, upscale, dilate_iterations)
# 用多組參數各跑一次 OCR，取「第一個符合格式」的結果；
# 都不符合就代表這張驗證碼這次辨識失敗，回傳 None，讓呼叫端換一張新圖再試。
_VARIANTS = [
    (6, 8, 0),
    (4, 6, 0),
    (4, 10, 0),
    (8, 8, 0),
    (6, 6, 1),
    (3, 6, 0),
]

_TESS_CONFIG = "--psm 7 -c tessedit_char_whitelist=0123456789x+-=?"


@dataclass
class CaptchaResult:
    raw_text: str
    left: int
    op: str
    right: int
    answer: int

    def as_answer_str(self) -> str:
        return str(self.answer)


def _extract_mask(img: Image.Image, min_size: int) -> np.ndarray:
    """把驗證碼圖片轉成乾淨的黑白 mask：留下偏藍色、偏暗的文字像素，
    並丟掉太小的連通元件（雜訊噪點）。"""
    arr = np.array(img.convert("RGB")).astype(int)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    blueness = b - np.maximum(r, g)
    mask = (blueness > 15) & (b < 230)

    labeled, num = ndimage.label(mask, structure=np.ones((3, 3)))
    if num == 0:
        return mask
    sizes = ndimage.sum(mask, labeled, range(1, num + 1))
    clean = np.zeros_like(mask)
    for idx, size in enumerate(sizes, start=1):
        if size >= min_size:
            clean |= labeled == idx
    return clean


def _render_variant(img: Image.Image, min_size: int, scale: int, dilate: int) -> Image.Image:
    mask = _extract_mask(img, min_size)
    if dilate:
        mask = ndimage.binary_dilation(mask, iterations=dilate)
    out = np.where(mask, 0, 255).astype(np.uint8)
    im = Image.fromarray(out)
    im = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
    padded = Image.new("L", (im.width + 40, im.height + 40), 255)
    padded.paste(im, (20, 20))
    return padded


def solve_captcha(image_bytes: bytes) -> Optional[CaptchaResult]:
    """嘗試辨識一張驗證碼圖片。

    回傳 CaptchaResult（辨識出的算式與答案），或者辨識失敗時回傳 None
    （呼叫端應該去要一張新的驗證碼圖片，而不是硬送出一個沒把握的答案）。
    """
    img = Image.open(io.BytesIO(image_bytes))

    for min_size, scale, dilate in _VARIANTS:
        try:
            variant_img = _render_variant(img, min_size, scale, dilate)
        except Exception:
            continue

        text = pytesseract.image_to_string(variant_img, config=_TESS_CONFIG)
        normalized = text.strip().replace(" ", "").replace("\n", "")
        match = _PATTERN.match(normalized)
        if not match:
            continue

        left_s, op_s, right_s = match.groups()
        op = _OP_MAP.get(op_s)
        if op is None:
            continue
        try:
            left = int(left_s)
            right = int(right_s)
            answer = eval(f"{left}{op}{right}")  # 只允許 + - * / 的安全算式
        except Exception:
            continue

        return CaptchaResult(
            raw_text=normalized,
            left=left,
            op=op,
            right=right,
            answer=int(answer),
        )

    return None
