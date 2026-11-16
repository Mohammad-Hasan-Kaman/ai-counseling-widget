# -*- coding: utf-8 -*-
"""
ماژول ابزارهای پنل ادمین - مرکز مشاوره نمونه
شامل: خروجی اکسل دیتای کاربران و ارسال پیام برادکست (همگانی)
این ماژول مستقل است و هیچ تغییری در کدهای موجود ایجاد نمی‌کند.
"""
import io
import json
import asyncio
import logging

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from app.config import USER_RECORDS_DB
from app.ghq_analyzer import GHQ_SEVERE_TOTAL, GHQ_ELEVATED_TOTAL

log = logging.getLogger(__name__)

BROADCAST_BATCH_DELAY = 0.05   # فاصله بین ارسال‌ها برای جلوگیری از محدودیت سرور بله
BROADCAST_REPORT_EVERY = 20    # هر چند نفر یک‌بار گزارش پیشرفت ارسال شود


# ══════════════════════════════════════════════
#  بخش ۱: خروجی اکسل دیتای کاربران
# ══════════════════════════════════════════════

_HEADER_FILL = PatternFill("solid", fgColor="2E5E4E")
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)

USERS_HEADERS = REQUESTS_HEADERS = [
    "ردیف", "نام و نام خانوادگی مراجع", "ثبت‌کننده", "رابطه", "شماره تماس", "شناسه بله (Chat ID)",
    "جنسیت", "سن", "موضوع مشاوره (مشکل اصلی)",
    "سابقه مراجعه قبلی به روانشناس/روانپزشک", "توضیحات درمان قبلی",
    "انتظار یا رویکرد موردنظر از مشاوره", "ترجیح جنسیت مشاور",
    "شعبه انتخابی",
    "GHQ - جسمانی‌شدن (سوماتیک)", "GHQ - اضطراب و بی‌خوابی",
    "GHQ - اختلال کارکرد اجتماعی", "GHQ - افسردگی",
    "نتیجه نهایی GHQ (امتیاز کل)", "وضعیت GHQ",
    "مشاوران پیشنهادی شده", "دلیل پیشنهاد", "شماره مراجعه", "تاریخ ثبت درخواست",
    "پاسخ سؤال‌های سفارشی",
]


def _safe_cell(v):
    """جلوگیری از formula injection در اکسل: مقادیر متنی که با = + - @ شروع شوند"""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def _style_header(ws, headers):
    fill, font, align = _HEADER_FILL, _HEADER_FONT, _HEADER_ALIGN
    for col_idx, title in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=title)
        cell.fill = fill
        cell.font = font
        cell.alignment = align
    ws.freeze_panes = "A2"
    for col_idx in range(1, len(headers) + 1):
        width = max(14, min(30, len(headers[col_idx - 1]) * 1.6 + 6))
        ws.column_dimensions[get_column_letter(col_idx)].width = width


def _fmt_recs(recommendations_json: str | None) -> tuple[str, str]:
    """استخراج نام مشاوران و دلایل پیشنهاد از JSON ذخیره‌شده"""
    if not recommendations_json:
        return "", ""
    try:
        import json
        recs = json.loads(recommendations_json)
        if isinstance(recs, list):
            names = " | ".join(r.get("name", "") for r in recs if isinstance(r, dict))
            reasons = " | ".join(
                f"{r.get('name','')}: {r.get('reason','')}" for r in recs if isinstance(r, dict)
            )
            return names, reasons
        return str(recs), ""
    except Exception:
        return "", ""


def _ghq_status(total) -> str:
    """برچسب وضعیت بر اساس امتیاز کل GHQ — آستانه‌ها از ghq_analyzer (قبلاً ۲۴ در برابر ۲۲ drifted بود)"""
    if total is None:
        return "انجام نداده"
    if total >= GHQ_SEVERE_TOTAL:
        return "شدید"
    if total >= GHQ_ELEVATED_TOTAL:
        return "متوسط"
    return "طبیعی"


def export_users_excel(tenant_id: int = 1) -> bytes:
    """
    تولید خروجی اکسل دیتای کاربران در حافظه (بدون ساخت فایل روی دیسک).
    هر دو شیت کامل و یکسان‌اند: شیت ۱ «کاربران» (مرتب بر اساس نام) و
    شیت ۲ «سوابق درخواست‌ها» (مرتب بر اساس تاریخ، جدیدترین اول) —
    هر ردیف = یک درخواست با تمام اطلاعات پرسیده‌شده از کاربر.
    خروجی: بایت‌های فایل xlsx
    """
    import sqlite3
    conn = sqlite3.connect(str(USER_RECORDS_DB))

    requests_rows_name = conn.execute("""
        SELECT full_name, phone, telegram_id, gender, age, topic,
               has_prev_therapy, prev_detail, expectation, preferred_gender,
               branch, ghq_scores, ghq_total, recommendations, request_number, created_at, custom_data
        FROM user_requests WHERE tenant_id=? ORDER BY full_name COLLATE NOCASE ASC, request_number ASC
    """, (tenant_id,)).fetchall()

    requests_rows_date = conn.execute("""
        SELECT full_name, phone, telegram_id, gender, age, topic,
               has_prev_therapy, prev_detail, expectation, preferred_gender,
               branch, ghq_scores, ghq_total, recommendations, request_number, created_at, custom_data
        FROM user_requests WHERE tenant_id=? ORDER BY created_at DESC
    """, (tenant_id,)).fetchall()
    conn.close()

    wb = Workbook()

    def _fmt_custom(custom_json: str | None) -> str:
        """پاسخ سؤال‌های سفارشی فلو (q_*) به‌صورت «کلید: مقدار | ...» — بدون relationship/initiator"""
        if not custom_json:
            return ""
        try:
            d = json.loads(custom_json)
            if not isinstance(d, dict) or not d:
                return ""
            skip = {"relationship", "initiator_name"}
            parts = [f"{k}: {v}" for k, v in d.items() if k not in skip]
            return " | ".join(parts)
        except Exception:
            return ""

    def _custom_field(custom_json: str | None, key: str) -> str:
        if not custom_json:
            return ""
        try:
            d = json.loads(custom_json)
            return str(d.get(key) or "") if isinstance(d, dict) else ""
        except Exception:
            return ""

    def _write_request_row(ws, i, r):
        (name, phone, tg_id, gender, age, topic, has_prev, prev_detail,
         expectation, pref_gender, branch, ghq_json, ghq_total, recs,
         req_number, created, custom_json) = r

        # استخراج زیرمقیاس‌های GHQ از JSON ذخیره‌شده
        somatic = anxiety = social = depression = None
        if ghq_json:
            try:
                g = json.loads(ghq_json)
                somatic = g.get("somatic")
                anxiety = g.get("anxiety")
                social = g.get("social")
                depression = g.get("depression")
                if ghq_total is None and isinstance(g.get("total"), int):
                    ghq_total = g["total"]
            except Exception:
                pass

        rec_names, rec_reasons = _fmt_recs(recs)
        relationship = _custom_field(custom_json, "relationship")
        initiator = _custom_field(custom_json, "initiator_name") or (name or "")

        def _clean(v):
            """تبدیل None و رشته 'None' به رشته خالی برای نمایش تمیز در اکسل"""
            if v is None:
                return ""
            s = str(v)
            return "" if s.strip().lower() == "none" else s

        ws.append([_safe_cell(v) for v in [
            i, name, initiator, relationship, phone, tg_id or "",
            gender or "",
            age if age is not None else "",
            topic or "",
            "بله" if has_prev else "خیر",
            _clean(prev_detail),
            expectation or "",
            pref_gender or "",
            branch or "-",
            somatic if somatic is not None else "-",
            anxiety if anxiety is not None else "-",
            social if social is not None else "-",
            depression if depression is not None else "-",
            ghq_total if ghq_total is not None else "-",
            _ghq_status(ghq_total),
            rec_names,
            rec_reasons,
            req_number if req_number is not None else "",
            created or "",
            _fmt_custom(custom_json),
        ]])

    # ── شیت ۱: کاربران (همه اطلاعات، مرتب بر اساس نام) ──
    ws_users = wb.active
    ws_users.title = "کاربران"
    _style_header(ws_users, USERS_HEADERS)
    for i, r in enumerate(requests_rows_name, start=1):
        _write_request_row(ws_users, i, r)

    # ── شیت ۲: سوابق درخواست‌ها (همان ستون‌ها، مرتب بر اساس تاریخ) ──
    ws_req = wb.create_sheet("سوابق درخواست‌ها")
    _style_header(ws_req, REQUESTS_HEADERS)
    for i, r in enumerate(requests_rows_date, start=1):
        _write_request_row(ws_req, i, r)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


def export_report_excel(tenant_id: int, days: int) -> bytes:
    """گزارش دوره‌ای به‌صورت xlsx در حافظه — ۴ شیت (خلاصه/روزانه/مشاوران برتر/موضوعات)"""
    from app.report_service import build_report

    rep = build_report(tenant_id, days)
    wb = Workbook()

    ws_sum = wb.active
    ws_sum.title = "خلاصه"
    _style_header(ws_sum, ["شاخص", "مقدار"])
    summary = [
        ("بازه", f"{rep['days']} روز اخیر"),
        ("گفتگوها", "—" if rep["partial_conversations"] else rep["conversations"]),
        ("درخواست‌ها (لید)", rep["leads"]),
        ("معرفی مشاور (مشاوره)", rep["consultations"]),
        ("میانگین نمره GHQ", "—" if rep["ghq_avg"] is None else rep["ghq_avg"]),
        ("تعداد آزمون GHQ", rep["ghq_n"]),
    ]
    for i, (k, v) in enumerate(summary, start=2):
        ws_sum.cell(row=i, column=1, value=_safe_cell(k))
        ws_sum.cell(row=i, column=2, value=_safe_cell(v))
    if rep["partial_conversations"]:
        note = ws_sum.cell(
            row=len(summary) + 3, column=1,
            value=f"گفتگوها حداکثر {rep['retention_days']} روز نگهداری می‌شوند؛ بازهٔ انتخابی بلندتر است.",
        )
        note.alignment = Alignment(wrap_text=True)

    ws_day = wb.create_sheet("روزانه")
    _style_header(ws_day, ["تاریخ (به وقت تهران)", "گفتگوها", "درخواست‌ها", "معرفی مشاور"])
    for i, d in enumerate(rep["axis"], start=2):
        ws_day.cell(row=i, column=1, value=d)
        ws_day.cell(row=i, column=2, value=rep["daily"]["conversations"][d])
        ws_day.cell(row=i, column=3, value=rep["daily"]["leads"][d])
        ws_day.cell(row=i, column=4, value=rep["daily"]["consultations"][d])

    ws_con = wb.create_sheet("مشاوران برتر")
    _style_header(ws_con, ["مشاور", "تعداد معرفی"])
    for i, r in enumerate(rep["top_consultants"], start=2):
        ws_con.cell(row=i, column=1, value=_safe_cell(r["name"]))
        ws_con.cell(row=i, column=2, value=r["count"])

    ws_top = wb.create_sheet("موضوعات")
    _style_header(ws_top, ["موضوع", "تعداد"])
    for i, r in enumerate(rep["top_topics"], start=2):
        ws_top.cell(row=i, column=1, value=_safe_cell(r["name"]))
        ws_top.cell(row=i, column=2, value=r["count"])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()
