# -*- coding: utf-8 -*-
import binascii
from datetime import datetime, timezone
import hashlib
from email.mime.text import MIMEText
import os
import re
import smtplib

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
from requests.adapters import HTTPAdapter
import streamlit as st
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
from urllib3.util import Retry
import yfinance as yf

"""
Smart Investment Advisor — المستشار المالي الذكي
====================================================
تطبيق Streamlit متكامل لتحليل الأسهم والصناديق في بورصات مصر وأمريكا ولندن:
  - تسجيل حساب ذاتي (Sign Up) وتسجيل دخول
  - تحليل فني (SMA/RSI/MACD) + مالي (قوائم مالية) + معنويات الأخبار
  - مقارنة السهم بأسهم أخرى في نفس القطاع
  - قائمة متابعة شخصية مع تنبيهات الاقتراب من المقاومة/القمة أو الدعم
    (داخل التطبيق + بريد إلكتروني اختياري)
  - دعم اللغتين العربية والإنجليزية

التشغيل محلياً:
    pip install -r requirements.txt
    streamlit run smart_investment_advisor.py

الرفع المجاني للمشاركة مع الأصدقاء: انظر ملف README.md المرفق
(يُنصح باستخدام Streamlit Community Cloud - مجاني بالكامل).

تنويه: أداة تحليل آلية تعليمية — وليست نصيحة استثمارية أو مالية مُلزمة.
"""

st.set_page_config(page_title="Smart Investment Advisor", page_icon="📈", layout="wide")

# =====================================================================
# 0) قاعدة البيانات (Supabase / PostgreSQL) — تسجيل الحسابات وقائمة المتابعة
# =====================================================================
@st.cache_resource
def get_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    options = ClientOptions(schema="public")
    return create_client(url, key, options=options)

# =====================================================================
# 1) الأمان: تجزئة كلمات المرور والمصادقة
# =====================================================================
def hash_password(password: str, salt_hex: str = None):
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return binascii.hexlify(pwd_hash).decode(), binascii.hexlify(salt).decode()


def is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def register_user(username: str, password: str, email: str):
    username = username.strip()
    if len(username) < 3:
        return False, "err_username_short"
    if len(password) < 6:
        return False, "err_password_short"
    if not is_valid_email(email):
        return False, "err_email_invalid"

    sb = get_supabase()
    existing = sb.table("users").select("username").eq("username", username).execute()
    if existing.data:
        return False, "err_username_taken"

    pwd_hash, salt = hash_password(password)
    sb.table("users").insert({
        "username": username, "password_hash": pwd_hash, "salt": salt,
        "email": email.strip(), "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    return True, "ok"


def authenticate_user(username: str, password: str):
    sb = get_supabase()
    res = sb.table("users").select("password_hash, salt, email").eq("username", username.strip()).execute()
    if not res.data:
        return False, None
    row = res.data[0]
    computed_hash, _ = hash_password(password, row["salt"])
    if computed_hash == row["password_hash"]:
        return True, row["email"]
    return False, None


def get_user_email(username: str):
    sb = get_supabase()
    res = sb.table("users").select("email").eq("username", username).execute()
    return res.data[0]["email"] if res.data else None


# ---------- قائمة المتابعة ----------
def add_to_watchlist(username, ticker, market_key, threshold_pct, email_enabled):
    sb = get_supabase()
    try:
        sb.table("watchlist").upsert({
            "username": username, "ticker": ticker, "market_key": market_key,
            "alert_threshold_pct": threshold_pct, "email_alerts_enabled": bool(email_enabled),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="username,ticker").execute()
        return True
    except Exception:
        return False


def remove_from_watchlist(username, ticker):
    sb = get_supabase()
    sb.table("watchlist").delete().eq("username", username).eq("ticker", ticker).execute()


def get_watchlist(username):
    sb = get_supabase()
    res = (sb.table("watchlist")
           .select("ticker, market_key, alert_threshold_pct, email_alerts_enabled")
           .eq("username", username).order("created_at").execute())
    return [{"ticker": r["ticker"], "market_key": r["market_key"], "threshold": r["alert_threshold_pct"],
             "email_enabled": bool(r["email_alerts_enabled"])} for r in res.data]


# =====================================================================
# 2) البريد الإلكتروني (تنبيهات اختيارية)
# =====================================================================
def get_smtp_config():
    try:
        cfg = st.secrets["smtp"]
        return {
            "server": cfg["server"],
            "port": int(cfg["port"]),
            "sender_email": cfg["sender_email"],
            "sender_password": cfg["sender_password"],
        }
    except Exception:
        return None


def send_alert_email(to_email: str, subject: str, body: str):
    config = get_smtp_config()
    if not config:
        return False, "smtp_not_configured"
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = config["sender_email"]
        msg["To"] = to_email
        with smtplib.SMTP(config["server"], config["port"], timeout=15) as server:
            server.starttls()
            server.login(config["sender_email"], config["sender_password"])
            server.sendmail(config["sender_email"], [to_email], msg.as_string())
        return True, "sent"
    except Exception as e:
        return False, str(e)


# =====================================================================
# 3) الترجمة (عربي / إنجليزي)
# =====================================================================
LANG_DICT = {
    "العربية": {
        "app_title": "📈 المستشار المالي الذكي",
        "app_subtitle": "تحليل فني ومالي وأخبار وقطاعي آلي + تنبيهات مقاومة/دعم",
        "nav_analysis": "🔍 التحليل والتوصية",
        "nav_watchlist": "🔔 قائمة المتابعة والتنبيهات",
        "nav_account": "👤 الحساب",
        "login_title": "تسجيل الدخول",
        "signup_title": "إنشاء حساب جديد",
        "username": "اسم المستخدم",
        "password": "كلمة المرور",
        "confirm_password": "تأكيد كلمة المرور",
        "email": "البريد الإلكتروني",
        "btn_login": "دخول",
        "btn_signup": "إنشاء الحساب",
        "err_login_failed": "❌ اسم المستخدم أو كلمة المرور غير صحيحة.",
        "err_username_short": "❌ اسم المستخدم يجب ألا يقل عن 3 أحرف.",
        "err_password_short": "❌ كلمة المرور يجب ألا تقل عن 6 أحرف.",
        "err_email_invalid": "❌ صيغة البريد الإلكتروني غير صحيحة.",
        "err_username_taken": "❌ اسم المستخدم مستخدم بالفعل، اختر اسماً آخر.",
        "err_password_mismatch": "❌ كلمتا المرور غير متطابقتين.",
        "signup_success": "✅ تم إنشاء الحساب بنجاح! يمكنك تسجيل الدخول الآن.",
        "welcome": "👋 مرحباً، {name}",
        "logout": "تسجيل الخروج 🚪",
        "select_market": "اختر السوق",
        "market_us": "🇺🇸 البورصة الأمريكية",
        "market_egx": "🇪🇬 البورصة المصرية (.CA)",
        "market_lse": "🇬🇧 بورصة لندن (.L)",
        "enter_ticker": "أدخل رمز السهم أو الصندوق",
        "ticker_help": "أمثلة: AAPL أو MSFT (أمريكية) — COMI أو HRHO (مصرية) — HSBA أو VOD (لندن)",
        "btn_analyze": "ابدأ التحليل الذكي 🚀",
        "loading": "جاري سحب البيانات وتحليلها فنياً ومالياً وقطاعياً وإخبارياً...",
        "error_fetch": "❌ تعذر جلب بيانات هذا الرمز. تأكد من صحة الرمز والسوق المختار، أو حاول مرة أخرى بعد قليل.",
        "warn_empty_ticker": "⚠️ من فضلك أدخل رمز السهم أولاً.",
        "asset_type_equity": "سهم شركة",
        "asset_type_fund": "صندوق استثماري / ETF",
        "asset_type_other": "أداة مالية",
        "basic_info": "📌 البيانات الأساسية",
        "price": "السعر الحالي",
        "day_change": "التغير اليومي",
        "market_cap": "القيمة السوقية",
        "currency": "العملة",
        "day_range": "أعلى/أقل سعر اليوم",
        "year_range": "أعلى/أقل سعر (52 أسبوع)",
        "chart_title": "📈 حركة السعر والمؤشرات الفنية (6 أشهر)",
        "price_panel": "السعر والمتوسطات المتحركة",
        "rsi_panel": "مؤشر القوة النسبية RSI",
        "macd_panel": "مؤشر MACD",
        "levels_title": "🎯 مستويات الدعم والمقاومة",
        "resistance": "المقاومة (القمة الأخيرة)",
        "support": "الدعم (القاع الأخير)",
        "dist_to_resistance": "المسافة للمقاومة",
        "dist_to_support": "المسافة للدعم",
        "fund_section": "💰 التحليل المالي الأساسي (القوائم المالية)",
        "pe": "مكرر الربحية P/E",
        "pb": "مكرر القيمة الدفترية P/B",
        "roe": "العائد على حقوق الملكية ROE",
        "net_margin": "هامش صافي الربح",
        "debt_equity": "نسبة الدين إلى حقوق الملكية",
        "current_ratio": "نسبة السيولة الحالية",
        "revenue_growth": "نمو الإيرادات (سنوي)",
        "dividend_yield": "عائد التوزيعات",
        "fund_not_available": "⚠️ لا تتوفر بيانات قوائم مالية تفصيلية لهذه الأداة (شائع في الصناديق وETFs)، سيعتمد التحليل بشكل أكبر على الجانب الفني والأخبار.",
        "sector_section": "🏭 مقارنة القطاع",
        "sector_label": "القطاع",
        "sector_not_available": "⚠️ لا تتوفر بيانات قطاع كافية أو منافسون محددون مسبقاً لهذا الرمز.",
        "sector_col_ticker": "الرمز",
        "sector_col_name": "الاسم",
        "sector_col_pe": "P/E",
        "sector_col_change": "التغير (3 أشهر)",
        "sector_avg_pe": "متوسط P/E للقطاع",
        "sector_avg_change": "متوسط أداء القطاع (3 أشهر)",
        "news_section": "📰 تحليل مشاعر الأخبار الحديثة",
        "no_news": "لا توجد أخبار حديثة متاحة لهذا الرمز حالياً.",
        "sent_pos": "إيجابية 👍",
        "sent_neg": "سلبية 👎",
        "sent_neu": "محايدة ⚖️",
        "final_rec": "🎯 التوصية النهائية",
        "confidence": "درجة الثقة في التوصية",
        "reasons_title": "الأسباب الرئيسية للتوصية:",
        "buy": "✅ شراء (Buy)",
        "sell": "🚨 بيع / تجنب (Sell)",
        "hold": "⏳ احتفاظ / مراقبة (Hold)",
        "conf_high": "مرتفعة",
        "conf_medium": "متوسطة",
        "conf_low": "منخفضة",
        "tech_axis": "الدرجة الفنية",
        "fund_axis": "الدرجة المالية",
        "news_axis": "درجة الأخبار",
        "sector_axis": "الدرجة القطاعية",
        "final_axis": "الدرجة الإجمالية",
        "add_watchlist_title": "🔔 أضف هذا السهم لقائمة المتابعة والتنبيهات",
        "alert_threshold": "نسبة التنبيه عند الاقتراب (%)",
        "email_alerts_toggle": "تفعيل تنبيهات البريد الإلكتروني لهذا السهم",
        "btn_add_watchlist": "أضف لقائمة المتابعة 💾",
        "added_to_watchlist": "✅ تمت إضافة {ticker} إلى قائمة المتابعة.",
        "watchlist_title": "🔔 قائمة المتابعة والتنبيهات",
        "watchlist_empty": "قائمة متابعتك فارغة حالياً. اذهب لصفحة التحليل وأضف أسهماً لمتابعتها.",
        "btn_check_alerts": "🔄 افحص التنبيهات الآن",
        "btn_remove": "إزالة 🗑️",
        "alert_near_resistance": "⚠️ اقتراب من مستوى المقاومة/القمة!",
        "alert_near_support": "⚠️ اقتراب من مستوى الدعم/القاع!",
        "no_alert": "لا توجد تنبيهات حالياً — السعر ضمن النطاق الطبيعي.",
        "email_sent": "📧 تم إرسال تنبيه بالبريد الإلكتروني.",
        "email_failed": "⚠️ تعذر إرسال البريد الإلكتروني (تحقق من إعدادات SMTP).",
        "email_not_configured": "ℹ️ لم يتم إعداد إرسال البريد الإلكتروني بعد من مالك التطبيق (سيتم عرض التنبيهات داخل التطبيق فقط).",
        "account_title": "👤 بيانات حسابك",
        "account_username": "اسم المستخدم",
        "account_email": "البريد الإلكتروني المسجل",
        "cost_calc_title": "🧾 حاسبة تكاليف الشراء والبيع والاحتفاظ",
        "cost_calc_note": "⚠️ القيم الافتراضية تقريبية وقابلة للتعديل — تأكد من الرسوم الفعلية لدى وسيطك قبل اتخاذ أي قرار.",
        "shares_qty": "عدد الأسهم",
        "commission_pct": "عمولة الوسيط (%)",
        "min_commission": "الحد الأدنى للعمولة",
        "vat_pct": "ضريبة القيمة المضافة على العمولة (%)",
        "custody_pct_annual": "رسوم الحفظ السنوية (%)",
        "buy_cost_total": "إجمالي تكلفة الشراء",
        "sell_proceeds_total": "صافي عوائد البيع",
        "breakeven_price": "سعر التعادل (Break-even)",
        "roundtrip_cost_pct": "تكلفة الدخول والخروج (% من المبلغ)",
        "custody_annual_cost": "تكلفة الحفظ السنوية التقديرية",
        "cost_warning": "⚠️ تكلفة الدخول والخروج مرتفعة نسبياً مقارنة بالمسافة المتاحة للمقاومة — قد تقلل هذه التكاليف من الجدوى الفعلية للصفقة.",
        "tax_section_title": "🧾 الوضع الضريبي المتوقع",
        "tax_residency_label": "حالتك الضريبية (تخص السوق الأمريكية فقط)",
        "tax_status_egypt_treaty": "مقيم/مواطن مصري — مع نموذج W-8BEN مُقدَّم للوسيط",
        "tax_status_egypt_no_treaty": "مقيم/مواطن مصري — بدون نموذج W-8BEN",
        "tax_status_us_person": "مواطن/مقيم أمريكي (US Person)",
        "dividend_wht_label": "ضريبة التوزيعات المستقطعة",
        "estimated_annual_dividend": "التوزيعات السنوية المتوقعة (تقديري)",
        "estimated_dividend_tax": "الضريبة المستقطعة تقديريًا",
        "net_annual_dividend": "صافي التوزيعات بعد الضريبة",
        "capital_gains_label": "ضريبة الأرباح الرأسمالية",
        "no_dividend_data": "لا تتوفر بيانات عائد توزيعات لهذا السهم لتقدير الضريبة.",
        "tax_disclaimer": "⚠️ معلومات عامة مبنية على القوانين والمعاهدات السارية حاليًا (تم التحقق منها عبر بحث محدث)، وليست استشارة ضريبية شخصية — بعض هذه الأحكام حديثة التعديل، فتأكد من وضعك الدقيق مع محاسب ضريبي مختص.",
        "note_div_us_treaty": "بموجب معاهدة الازدواج الضريبي بين مصر وأمريكا (1980)، تُخفَّض الضريبة من 30% إلى 15% بشرط تقديم نموذج W-8BEN لوسيطك.",
        "note_div_us_no_treaty": "بدون تقديم نموذج W-8BEN، يُطبَّق المعدل الافتراضي الكامل 30% على التوزيعات.",
        "note_div_us_person": "بصفتك مواطن/مقيم أمريكي، التوزيعات تُضاف لدخلك الخاضع للضريبة الأمريكية العادية حسب شريحتك، وليست خاضعة لاستقطاع ثابت بنفس آلية غير المقيمين — راجع محاسبك لحساب الشريحة الدقيقة.",
        "note_div_uk": "المملكة المتحدة لا تفرض ضريبة استقطاع على توزيعات الأرباح لغير المقيمين (باستثناء صناديق الـ REIT الخاضعة لـ20%).",
        "note_div_egx": "ضريبة استقطاع 5% على توزيعات الأسهم المقيّدة في البورصة المصرية، تنطبق على المقيمين وغير المقيمين.",
        "note_cg_us_nra": "كغير مقيم في أمريكا، أرباح بيع الأسهم الأمريكية معفاة عادة من الضريبة الأمريكية (0%)، بشرط عدم الإقامة 183 يومًا أو أكثر في أمريكا خلال السنة الضريبية.",
        "note_cg_us_person": "بصفتك مواطن/مقيم أمريكي، أرباح البيع تخضع لضريبة الأرباح الرأسمالية الأمريكية العادية (قصيرة أو طويلة الأجل حسب مدة الاحتفاظ) — يختلف المعدل حسب دخلك ومدة الاحتفاظ؛ يُنصح بمراجعة محاسب.",
        "note_cg_uk": "أرباح بيع الأسهم البريطانية لغير المقيمين في المملكة المتحدة تكون عادة خارج نطاق ضريبة الأرباح الرأسمالية البريطانية.",
        "note_cg_egx": "بموجب التعديل التشريعي الصادر في يوليو 2026، أرباح بيع الأسهم المقيّدة في البورصة المصرية معفاة من الضريبة (0%) لكل من المقيمين وغير المقيمين.",
        "transfer_calc_title": "🌍 حاسبة مصاريف التحويل البنكي بين الدول/البنوك",
        "transfer_calc_note": "⚠️ رسوم التحويل الفعلية تختلف كثيراً بين البنوك وتتغير باستمرار — هذه القيم إرشادية فقط، تأكد من بنكك.",
        "transfer_amount": "المبلغ المراد تحويله",
        "transfer_flat_fee": "رسم التحويل الثابت",
        "transfer_fx_margin_pct": "هامش سعر الصرف التقريبي (%)",
        "transfer_net_amount": "المبلغ الصافي بعد الرسوم",
        "transfer_total_cost": "إجمالي تكلفة التحويل",
        "execute_section_title": "🔗 تنفيذ الصفقة (شبه آلي)",
        "execute_section_note": "هذه الروابط تفتح لك منصة الوسيط لتتخذ القرار وتنفذ الصفقة بنفسك — التطبيق لا يقوم بأي عملية شراء أو بيع تلقائية نيابة عنك، ولا يصل لحسابك المالي بأي شكل.",
