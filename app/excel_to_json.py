# -*- coding: utf-8 -*-
"""
استخراج پروفایل مشاوران از فایل اکسل مرکز.
ساختار واقعی فایل: بلوکی — هر مشاور چند سطر دارد؛ سطر اول نام، و سطرهای
ادامه (تا نام بعدی) بقیه اطلاعات اوست. پس داده‌ها باید در سطح بلوک جمع شوند.
"""
import json
import math
import os

import pandas as pd

from app.config import PROFILES_JSON

EXCEL_FILE = PROFILES_JSON.parent / "consultants_upload.xlsx"
JSON_OUTPUT = str(PROFILES_JSON)

HEADER_ROW = 1          # ردیف سرستون‌های جزئی (شاخص صفرمبنا)
DATA_START = 2          # داده از این ردیف شروع می‌شود
NAME_COL = 2            # ستون نام مشاور (C)
ABILITY_COL = 1         # ستون ضریب توانمندی (B)
LOCATION_COL = 3        # ستون محل کار (D)
EDU_COL = 4             # ستون تحصیلات و سوابق (E)
GENERAL_COLS = range(5, 18)    # حوزه‌های عمومی (F تا R)
DETAIL_COLS = range(19, 31)    # موضوعات جزئی (T تا AF)
GENERAL2_COL = 18       # حوزه کلی (S)
FREE31_COL = 31         # موضوعات آزاد (AF)
AGE_COL = 32            # محدوده سنی (AG)
LICENSE_COL = 33        # پروانه (AH)
NOTES_COL = 34          # ملاحظات (AI)

ABILITY_VALUES = (1.0, 2.0, 3.0)

LOCATION_LABELS = {
    "zafar": "ظفر",
    "iran": "خیابان ایران",
}


def _txt(v) -> str:
    s = str(v).strip() if pd.notna(v) else ""
    return "" if s.lower() == "nan" else s


def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def normalize_location(raw: str) -> str:
    """یکدست‌سازی مقادیر محل کار به سه حالت مجاز: ظفر / خیابان ایران / هر دو"""
    t = (raw or "").replace("ي", "ی").replace("ك", "ک")
    if " مجازی" in t or "آنلاین" in t:
        return "هر دو"
    if "ایران" in t:
        return "خیابان ایران"
    if "ظفر" in t or "زعفرانیه" in t:
        return "ظفر"
    if "هر دو" in t or "هردو" in t:
        return "هر دو"
    return "هر دو"


def convert_excel_to_json(excel_path, json_output=JSON_OUTPUT):
    if not os.path.exists(excel_path):
        print("فایل اکسل در مسیر %s یافت نشد." % excel_path)
        return 0

    df = pd.read_excel(excel_path, header=None)
    if len(df) <= DATA_START:
        print("فایل اکسل خالی است.")
        return 0

    # سرستون‌ها از ردیف HEADER_ROW (با fallback به ردیف اول)
    headers = {}
    for c in list(GENERAL_COLS) + list(DETAIL_COLS):
        h = _txt(df.iloc[HEADER_ROW, c]) or _txt(df.iloc[0, c])
        headers[c] = h or ("ستون %d" % (c + 1))

    # بلوک‌ها: هر سطر دارای نام تا نام بعدی
    name_rows = [i for i in range(DATA_START, len(df)) if _txt(df.iloc[i, NAME_COL])]

    output_data = []
    for bi, r in enumerate(name_rows):
        end = name_rows[bi + 1] if bi + 1 < len(name_rows) else len(df)

        # ضریب توانمندی: همان سطر، وگرنه اولین عدد معتبر همان بلوک
        ability = _num(df.iloc[r, ABILITY_COL])
        if ability not in ABILITY_VALUES:
            ability = None
            for rr in range(r + 1, min(end, r + 6)):
                v = _num(df.iloc[rr, ABILITY_COL])
                if v in ABILITY_VALUES:
                    ability = v
                    break
        if ability is None:
            ability = 2.0

        def _join(col):
            parts = []
            for rr in range(r, end):
                if col == NAME_COL and rr != r:
                    continue
                t = _txt(df.iloc[rr, col])
                if t and t not in parts:
                    parts.append(t)
            return " | ".join(parts)

        gen_dict = {}
        for c in GENERAL_COLS:
            v = _join(c)
            if v:
                gen_dict[headers[c]] = v
        det_dict = {}
        for c in DETAIL_COLS:
            v = _join(c)
            if v:
                det_dict[headers[c]] = v

        output_data.append({
            "name": _txt(df.iloc[r, NAME_COL]),
            "ability": ability,
            "location": normalize_location(_join(LOCATION_COL)),
            "education_experience": _join(EDU_COL),
            "general_area": _join(GENERAL2_COL),
            "general_area_2": "",
            "specializations": {**gen_dict, **det_dict},
            "detailed_topics": _join(FREE31_COL),
            "age_range": _join(AGE_COL),
            "license": _join(LICENSE_COL),
            "notes": _join(NOTES_COL),
        })

    with open(json_output, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print("%d consultants saved to %s" % (len(output_data), json_output))
    return len(output_data)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        convert_excel_to_json(sys.argv[1])
    else:
        print("usage: python -m app.excel_to_json <xlsx_path>")
