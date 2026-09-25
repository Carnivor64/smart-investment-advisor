# -*- coding: utf-8 -*-
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

import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from supabase import create_client, Client
import hashlib
import os
import re
import binascii
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timezone

st.set_page_config(page_title="Smart Investment Advisor", page_icon="📈", layout="wide")

# =====================================================================
# 0) قاعدة البيانات (Supabase / PostgreSQL) — تسجيل الحسابات وقائمة المتابعة
# =====================================================================
# القاعدة مستضافة سحابياً على Supabase (مجاني) بدل ملف محلي، عشان يقدر يوصلها
# التطبيق نفسه ومهمة الفحص الدورية المنفصلة (GitHub Actions) في نفس الوقت.
# جداول users وwatchlist يجب إنشاؤها مسبقاً في مشروع Supabase (انظر README.md
# لنص SQL الجاهز)، وربط بيانات الاتصال عبر Secrets كما هو موضح في README.md.
@st.cache_resource
def get_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)
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
        "execute_btn_label": "افتح {platform} لتنفيذ الصفقة ↗",
        "execute_disclaimer": "⚠️ هذه معلومات عامة وليست توصية شخصية باختيار وسيط معين — تأكد من الرسوم والتراخيص والتوفر في بلدك قبل فتح حساب.",
        "disclaimer": "⚠️ تنويه هام: هذا التحليل مولَّد بالكامل بواسطة خوارزميات رياضية آلية اعتماداً على بيانات علنية، وليس توصية استثمارية أو نصيحة مالية مُلزمة. استشر مستشاراً مالياً مرخصاً قبل اتخاذ أي قرار استثماري.",
    },
    "English": {
        "app_title": "📈 Smart Investment Advisor",
        "app_subtitle": "Automated Technical, Fundamental, News & Sector Analysis + Support/Resistance Alerts",
        "nav_analysis": "🔍 Analysis & Recommendation",
        "nav_watchlist": "🔔 Watchlist & Alerts",
        "nav_account": "👤 Account",
        "login_title": "Log In",
        "signup_title": "Create New Account",
        "username": "Username",
        "password": "Password",
        "confirm_password": "Confirm Password",
        "email": "Email",
        "btn_login": "Log In",
        "btn_signup": "Create Account",
        "err_login_failed": "❌ Incorrect username or password.",
        "err_username_short": "❌ Username must be at least 3 characters.",
        "err_password_short": "❌ Password must be at least 6 characters.",
        "err_email_invalid": "❌ Invalid email format.",
        "err_username_taken": "❌ Username already taken, choose another.",
        "err_password_mismatch": "❌ Passwords do not match.",
        "signup_success": "✅ Account created successfully! You can log in now.",
        "welcome": "👋 Welcome, {name}",
        "logout": "Log Out 🚪",
        "select_market": "Select Market",
        "market_us": "🇺🇸 US Market",
        "market_egx": "🇪🇬 Egyptian Exchange EGX (.CA)",
        "market_lse": "🇬🇧 London Stock Exchange (.L)",
        "enter_ticker": "Enter Ticker Symbol",
        "ticker_help": "Examples: AAPL or MSFT (US) — COMI or HRHO (Egypt) — HSBA or VOD (London)",
        "btn_analyze": "Start Smart Analysis 🚀",
        "loading": "Fetching data and running technical, fundamental, sector & news analysis...",
        "error_fetch": "❌ Could not fetch data for this ticker. Check the symbol/market, or try again shortly.",
        "warn_empty_ticker": "⚠️ Please enter a ticker symbol first.",
        "asset_type_equity": "Company Stock",
        "asset_type_fund": "Fund / ETF",
        "asset_type_other": "Financial Instrument",
        "basic_info": "📌 Basic Data",
        "price": "Current Price",
        "day_change": "Day Change",
        "market_cap": "Market Cap",
        "currency": "Currency",
        "day_range": "Day High/Low",
        "year_range": "52-Week High/Low",
        "chart_title": "📈 Price Action & Technical Indicators (6 Months)",
        "price_panel": "Price & Moving Averages",
        "rsi_panel": "RSI Indicator",
        "macd_panel": "MACD Indicator",
        "levels_title": "🎯 Support & Resistance Levels",
        "resistance": "Resistance (Recent Peak)",
        "support": "Support (Recent Bottom)",
        "dist_to_resistance": "Distance to Resistance",
        "dist_to_support": "Distance to Support",
        "fund_section": "💰 Fundamental Analysis (Financial Statements)",
        "pe": "P/E Ratio",
        "pb": "P/B Ratio",
        "roe": "Return on Equity (ROE)",
        "net_margin": "Net Profit Margin",
        "debt_equity": "Debt / Equity Ratio",
        "current_ratio": "Current Ratio",
        "revenue_growth": "Revenue Growth (YoY)",
        "dividend_yield": "Dividend Yield",
        "fund_not_available": "⚠️ Detailed financial statement data isn't available for this instrument (common for funds/ETFs). Analysis will rely more on technicals and news.",
        "sector_section": "🏭 Sector Comparison",
        "sector_label": "Sector",
        "sector_not_available": "⚠️ Not enough sector data or predefined peers available for this ticker.",
        "sector_col_ticker": "Ticker",
        "sector_col_name": "Name",
        "sector_col_pe": "P/E",
        "sector_col_change": "3M Change",
        "sector_avg_pe": "Sector Avg P/E",
        "sector_avg_change": "Sector Avg 3M Performance",
        "news_section": "📰 Recent News Sentiment Analysis",
        "no_news": "No recent news available for this ticker.",
        "sent_pos": "Positive 👍",
        "sent_neg": "Negative 👎",
        "sent_neu": "Neutral ⚖️",
        "final_rec": "🎯 Final Recommendation",
        "confidence": "Recommendation Confidence",
        "reasons_title": "Key reasons behind this recommendation:",
        "buy": "✅ Buy",
        "sell": "🚨 Sell / Avoid",
        "hold": "⏳ Hold / Watch",
        "conf_high": "High",
        "conf_medium": "Medium",
        "conf_low": "Low",
        "tech_axis": "Technical Score",
        "fund_axis": "Fundamental Score",
        "news_axis": "News Score",
        "sector_axis": "Sector Score",
        "final_axis": "Overall Score",
        "add_watchlist_title": "🔔 Add this stock to your watchlist & alerts",
        "alert_threshold": "Alert proximity threshold (%)",
        "email_alerts_toggle": "Enable email alerts for this stock",
        "btn_add_watchlist": "Add to Watchlist 💾",
        "added_to_watchlist": "✅ {ticker} added to your watchlist.",
        "watchlist_title": "🔔 Watchlist & Alerts",
        "watchlist_empty": "Your watchlist is empty. Go to the Analysis page and add stocks to track.",
        "btn_check_alerts": "🔄 Check Alerts Now",
        "btn_remove": "Remove 🗑️",
        "alert_near_resistance": "⚠️ Approaching resistance/peak level!",
        "alert_near_support": "⚠️ Approaching support/bottom level!",
        "no_alert": "No alerts right now — price is within normal range.",
        "email_sent": "📧 Alert email sent.",
        "email_failed": "⚠️ Could not send email (check SMTP settings).",
        "email_not_configured": "ℹ️ The app owner hasn't configured email sending yet (alerts will show in-app only).",
        "account_title": "👤 Your Account",
        "account_username": "Username",
        "account_email": "Registered Email",
        "cost_calc_title": "🧾 Buy/Sell/Hold Cost Calculator",
        "cost_calc_note": "⚠️ Default values are approximate and editable — confirm your broker's actual fees before deciding.",
        "shares_qty": "Number of Shares",
        "commission_pct": "Broker Commission (%)",
        "min_commission": "Minimum Commission",
        "vat_pct": "VAT on Commission (%)",
        "custody_pct_annual": "Annual Custody Fee (%)",
        "buy_cost_total": "Total Buy Cost",
        "sell_proceeds_total": "Net Sell Proceeds",
        "breakeven_price": "Break-even Price",
        "roundtrip_cost_pct": "Round-trip Cost (% of amount)",
        "custody_annual_cost": "Estimated Annual Custody Cost",
        "cost_warning": "⚠️ Round-trip costs are relatively high versus the available distance to resistance — this may reduce the trade's real profitability.",
        "tax_section_title": "🧾 Expected Tax Treatment",
        "tax_residency_label": "Your Tax Status (relevant to the US market only)",
        "tax_status_egypt_treaty": "Egyptian resident/citizen — W-8BEN filed with broker",
        "tax_status_egypt_no_treaty": "Egyptian resident/citizen — no W-8BEN filed",
        "tax_status_us_person": "US citizen/resident (US Person)",
        "dividend_wht_label": "Dividend Withholding Tax",
        "estimated_annual_dividend": "Estimated Annual Dividends",
        "estimated_dividend_tax": "Estimated Withheld Tax",
        "net_annual_dividend": "Net Dividends After Tax",
        "capital_gains_label": "Capital Gains Tax",
        "no_dividend_data": "No dividend yield data available for this stock to estimate tax.",
        "tax_disclaimer": "⚠️ General information based on currently applicable laws and treaties (verified via updated search), not personal tax advice — some provisions were recently amended, so confirm your exact status with a qualified tax accountant.",
        "note_div_us_treaty": "Under the US-Egypt tax treaty (1980), the rate is reduced from 30% to 15% provided a W-8BEN form is filed with your broker.",
        "note_div_us_no_treaty": "Without filing a W-8BEN form, the full default 30% rate applies to dividends.",
        "note_div_us_person": "As a US citizen/resident, dividends are added to your regular US taxable income at your bracket rate, not subject to a flat broker withholding like non-residents — consult your accountant for the exact bracket.",
        "note_div_uk": "The UK does not levy withholding tax on dividends for non-residents (except REITs, subject to 20%).",
        "note_div_egx": "A 5% withholding tax applies to dividends on EGX-listed shares, for both residents and non-residents.",
        "note_cg_us_nra": "As a non-US-resident, gains from selling US stocks are generally exempt from US tax (0%), provided you're not present in the US for 183+ days in the tax year.",
        "note_cg_us_person": "As a US citizen/resident, sale gains are subject to regular US capital gains tax (short or long-term depending on holding period) — rate depends on your income and holding period; consult an accountant.",
        "note_cg_uk": "Gains from selling UK stocks by non-UK-residents are generally outside the scope of UK capital gains tax.",
        "note_cg_egx": "Under the legislative amendment issued in July 2026, gains from selling EGX-listed shares are exempt from tax (0%) for both residents and non-residents.",
        "transfer_calc_title": "🌍 Cross-Border/Bank Transfer Fee Calculator",
        "transfer_calc_note": "⚠️ Actual transfer fees vary widely between banks and change often — these are illustrative estimates only, confirm with your bank.",
        "transfer_amount": "Amount to Transfer",
        "transfer_flat_fee": "Flat Transfer Fee",
        "transfer_fx_margin_pct": "Approx. FX Margin (%)",
        "transfer_net_amount": "Net Amount After Fees",
        "transfer_total_cost": "Total Transfer Cost",
        "execute_section_title": "🔗 Execute Trade (Semi-Automated)",
        "execute_section_note": "These links open the broker's platform so YOU decide and execute — the app never places trades automatically on your behalf and never accesses your funds.",
        "execute_btn_label": "Open {platform} to execute ↗",
        "execute_disclaimer": "⚠️ General information only, not a personal recommendation to use a specific broker — verify fees, licensing and availability in your country before opening an account.",
        "disclaimer": "⚠️ Important: This analysis is fully automated based on public data and mathematical scoring. It is NOT financial advice. Consult a licensed financial advisor before making investment decisions.",
    },
}

REASON_TEXT = {
    "trend_up": {"العربية": "السعر أعلى من المتوسطين المتحركين 20 و50 يوم (اتجاه صاعد).",
                 "English": "Price is above SMA20 & SMA50 (uptrend)."},
    "trend_down": {"العربية": "السعر أقل من المتوسطين المتحركين 20 و50 يوم (اتجاه هابط).",
                   "English": "Price is below SMA20 & SMA50 (downtrend)."},
    "trend_sideways": {"العربية": "لا يوجد اتجاه فني واضح حالياً (تذبذب).",
                        "English": "No clear technical trend (sideways)."},
    "golden_cross": {"العربية": "المتوسط 50 يوم أعلى من المتوسط 200 يوم (إشارة صعودية متوسطة المدى).",
                      "English": "SMA50 above SMA200 (medium-term bullish signal)."},
    "death_cross": {"العربية": "المتوسط 50 يوم أقل من المتوسط 200 يوم (إشارة هبوطية متوسطة المدى).",
                     "English": "SMA50 below SMA200 (medium-term bearish signal)."},
    "rsi_oversold": {"العربية": "مؤشر RSI في منطقة التشبع البيعي (فرصة ارتداد محتملة).",
                      "English": "RSI is in oversold territory (potential bounce)."},
    "rsi_overbought": {"العربية": "مؤشر RSI في منطقة التشبع الشرائي (خطر تصحيح).",
                        "English": "RSI is in overbought territory (pullback risk)."},
    "rsi_neutral": {"العربية": "مؤشر RSI في منطقة محايدة.", "English": "RSI is in a neutral zone."},
    "macd_bullish": {"العربية": "مؤشر MACD يعطي إشارة شرائية (فوق خط الإشارة).",
                      "English": "MACD is above its signal line (bullish)."},
    "macd_bearish": {"العربية": "مؤشر MACD يعطي إشارة بيعية (تحت خط الإشارة).",
                      "English": "MACD is below its signal line (bearish)."},
    "pe_cheap": {"العربية": "مكرر الربحية منخفض نسبياً (السهم قد يكون رخيصاً).",
                 "English": "Low P/E ratio (stock may be undervalued)."},
    "pe_fair": {"العربية": "مكرر الربحية في نطاق معقول.", "English": "P/E ratio is in a reasonable range."},
    "pe_expensive": {"العربية": "مكرر الربحية مرتفع (السهم قد يكون مبالغاً في تقييمه).",
                      "English": "High P/E ratio (stock may be overvalued)."},
    "pe_negative": {"العربية": "مكرر الربحية سالب (الشركة تحقق خسائر حالياً).",
                     "English": "Negative P/E (company is currently loss-making)."},
    "roe_strong": {"العربية": "عائد قوي على حقوق الملكية (ROE > 15%).",
                   "English": "Strong return on equity (ROE > 15%)."},
    "roe_ok": {"العربية": "عائد مقبول على حقوق الملكية.", "English": "Acceptable return on equity."},
    "roe_weak": {"العربية": "عائد ضعيف على حقوق الملكية.", "English": "Weak return on equity."},
    "debt_low": {"العربية": "مستوى دين منخفض مقارنة بحقوق الملكية.",
                 "English": "Low debt relative to equity."},
    "debt_moderate": {"العربية": "مستوى دين معتدل.", "English": "Moderate debt level."},
    "debt_high": {"العربية": "مستوى دين مرتفع (مخاطرة مالية أعلى).",
                  "English": "High debt level (higher financial risk)."},
    "margin_strong": {"العربية": "هامش ربح صافٍ قوي.", "English": "Strong net profit margin."},
    "margin_thin": {"العربية": "هامش ربح صافٍ ضعيف نسبياً.", "English": "Relatively thin net margin."},
    "margin_negative": {"العربية": "هامش ربح صافٍ سالب (الشركة خاسرة).",
                         "English": "Negative net margin (company is losing money)."},
    "growth_strong": {"العربية": "نمو قوي في الإيرادات مقارنة بالعام السابق.",
                       "English": "Strong year-over-year revenue growth."},
    "growth_slow": {"العربية": "نمو إيرادات بطيء.", "English": "Slow revenue growth."},
    "growth_negative": {"العربية": "تراجع في الإيرادات مقارنة بالعام السابق.",
                         "English": "Revenue declined year-over-year."},
    "sector_pe_cheap": {"العربية": "مكرر الربحية أقل من متوسط القطاع (تقييم جذاب نسبياً).",
                         "English": "P/E is below sector average (relatively attractive valuation)."},
    "sector_pe_expensive": {"العربية": "مكرر الربحية أعلى من متوسط القطاع (تقييم مرتفع نسبياً).",
                             "English": "P/E is above sector average (relatively expensive)."},
    "sector_pe_inline": {"العربية": "مكرر الربحية قريب من متوسط القطاع.",
                          "English": "P/E is roughly in line with sector average."},
    "sector_outperform": {"العربية": "أداء السعر خلال 3 أشهر أفضل من متوسط أداء القطاع.",
                           "English": "3-month price performance beats the sector average."},
    "sector_underperform": {"العربية": "أداء السعر خلال 3 أشهر أضعف من متوسط أداء القطاع.",
                             "English": "3-month price performance lags the sector average."},
    "sector_inline_performance": {"العربية": "أداء السعر قريب من متوسط أداء القطاع.",
                                   "English": "Price performance is roughly in line with the sector."},
}

# =====================================================================
# 4) شبكة الاتصال الآمنة
# =====================================================================
@st.cache_resource
def create_secure_session():
    session = requests.Session()
    retry = Retry(total=3, connect=3, backoff_factor=0.5,
                   status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def normalize_ticker(raw_ticker: str, market_key: str) -> str:
    ticker = raw_ticker.strip().upper()
    suffix_map = {"market_egx": ".CA", "market_lse": ".L", "market_us": ""}
    suffix = suffix_map.get(market_key, "")
    if suffix and not (ticker.endswith(".CA") or ticker.endswith(".L")):
        ticker = f"{ticker}{suffix}"
    return ticker


# =====================================================================
# 4.1) حاسبة تكاليف الشراء/البيع/الاحتفاظ (تقديرية - قابلة للتعديل)
# =====================================================================
def calculate_trade_costs(quantity: float, price: float, commission_pct: float,
                           min_commission: float, vat_pct: float, custody_pct_annual: float):
    """تحسب تكلفة الشراء وصافي عوائد البيع وسعر التعادل بشكل تقديري.
    كل النسب افتراضية وقابلة للتعديل من المستخدم؛ تختلف الرسوم الفعلية حسب الوسيط."""
    gross_amount = quantity * price
    raw_commission = gross_amount * (commission_pct / 100.0)
    commission = max(raw_commission, min_commission)
    commission_with_vat = commission * (1 + vat_pct / 100.0)

    buy_cost_total = gross_amount + commission_with_vat
    sell_proceeds_total = gross_amount - commission_with_vat

    # سعر التعادل: أقل سعر بيع للسهم يعوّض تكلفة الشراء وعمولة البيع معاً (تقريب تكراري بسيط)
    if quantity > 0:
        breakeven_price = buy_cost_total / quantity
        # نضيف تأثير عمولة البيع نفسها بشكل تقريبي (تكرار واحد كافٍ للتقريب العملي)
        est_sell_commission = max(breakeven_price * quantity * (commission_pct / 100.0), min_commission) * (1 + vat_pct / 100.0)
        breakeven_price = (buy_cost_total + est_sell_commission) / quantity
    else:
        breakeven_price = 0

    roundtrip_cost_pct = (commission_with_vat * 2 / gross_amount * 100) if gross_amount else 0
    custody_annual_cost = gross_amount * (custody_pct_annual / 100.0)

    return {
        "buy_cost_total": buy_cost_total,
        "sell_proceeds_total": sell_proceeds_total,
        "breakeven_price": breakeven_price,
        "roundtrip_cost_pct": roundtrip_cost_pct,
        "custody_annual_cost": custody_annual_cost,
    }


def calculate_transfer_cost(amount: float, flat_fee: float, fx_margin_pct: float):
    """تقدير تكلفة تحويل بنكي دولي: رسم ثابت + هامش تقريبي لسعر الصرف. قيم إرشادية فقط."""
    fx_cost = amount * (fx_margin_pct / 100.0)
    total_cost = flat_fee + fx_cost
    net_amount = max(amount - total_cost, 0)
    return {"total_cost": total_cost, "net_amount": net_amount}


# =====================================================================
# 4.3) الوضع الضريبي المتوقع حسب السوق وحالة الإقامة الضريبية
# =====================================================================
# المصادر (تم التحقق منها ببحث محدث): معاهدة الازدواج الضريبي مصر-أمريكا (1980)،
# قواعد مصلحة الضرائب الأمريكية لغير المقيمين (IRS Pub. 519)، عدم فرض بريطانيا
# ضريبة استقطاع على توزيعات غير المقيمين، والقانون المصري رقم 151/153 لسنة 2026
# الذي أعفى أرباح بيع الأسهم المقيدة في EGX من الضريبة لكل من المقيمين وغير المقيمين
# (ساري من 31 يوليو 2026). هذه معلومات عامة قابلة للتغيير، وليست استشارة ضريبية شخصية.
def get_tax_info(market_key: str, tax_status: str = "egypt_treaty") -> dict:
    if market_key == "market_egx":
        return {"dividend_wht_pct": 5.0, "div_note": "note_div_egx", "cg_note": "note_cg_egx"}
    if market_key == "market_lse":
        return {"dividend_wht_pct": 0.0, "div_note": "note_div_uk", "cg_note": "note_cg_uk"}
    if market_key == "market_us":
        if tax_status == "egypt_no_treaty":
            return {"dividend_wht_pct": 30.0, "div_note": "note_div_us_no_treaty", "cg_note": "note_cg_us_nra"}
        if tax_status == "us_person":
            return {"dividend_wht_pct": None, "div_note": "note_div_us_person", "cg_note": "note_cg_us_person"}
        return {"dividend_wht_pct": 15.0, "div_note": "note_div_us_treaty", "cg_note": "note_cg_us_nra"}
    return {"dividend_wht_pct": None, "div_note": None, "cg_note": None}


# =====================================================================
# 4.2) روابط تنفيذ الصفقة (شبه آلي) — معلومات عامة، بلا اتصال بأي حساب مالي
# =====================================================================
# ملاحظة: هذه معلومات عامة إرشادية وليست توصية شخصية باختيار وسيط معين.
# التطبيق لا ينفذ أي عملية شراء/بيع تلقائياً ولا يصل لأي حساب مالي — فقط يفتح رابط المنصة.
EXECUTION_PLATFORMS = {
    "market_us": [
        ("Thndr", "https://thndr.app"),
        ("Interactive Brokers", "https://www.interactivebrokers.com"),
    ],
    "market_egx": [
        ("Thndr", "https://thndr.app"),
    ],
    "market_lse": [
        ("Interactive Brokers", "https://www.interactivebrokers.com"),
    ],
}


# =====================================================================
# 5) المؤشرات الفنية
# =====================================================================
def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def calculate_macd(close: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def add_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["SMA_20"] = df["Close"].rolling(window=20, min_periods=1).mean()
    df["SMA_50"] = df["Close"].rolling(window=50, min_periods=1).mean()
    df["SMA_200"] = df["Close"].rolling(window=200, min_periods=1).mean()
    df["RSI_14"] = calculate_rsi(df["Close"])
    macd_line, signal_line, hist = calculate_macd(df["Close"])
    df["MACD"] = macd_line
    df["MACD_SIGNAL"] = signal_line
    df["MACD_HIST"] = hist
    return df


def score_technicals(df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    price, sma20, sma50, sma200 = last["Close"], last["SMA_20"], last["SMA_50"], last["SMA_200"]
    rsi, macd, macd_signal = last["RSI_14"], last["MACD"], last["MACD_SIGNAL"]

    contributions = []
    if price > sma20 > sma50:
        contributions.append((1.0, 100, "trend_up"))
    elif price < sma20 < sma50:
        contributions.append((1.0, -100, "trend_down"))
    else:
        contributions.append((1.0, 0, "trend_sideways"))

    if len(df) >= 200 and not np.isnan(sma200):
        if sma50 > sma200:
            contributions.append((0.6, 60, "golden_cross"))
        else:
            contributions.append((0.6, -60, "death_cross"))

    if rsi < 30:
        contributions.append((0.7, 70, "rsi_oversold"))
    elif rsi > 70:
        contributions.append((0.7, -70, "rsi_overbought"))
    else:
        contributions.append((0.7, 0, "rsi_neutral"))

    if macd > macd_signal:
        contributions.append((0.5, 50, "macd_bullish"))
    else:
        contributions.append((0.5, -50, "macd_bearish"))

    total_weight = sum(w for w, _, _ in contributions)
    final_score = sum(w * s for w, s, _ in contributions) / total_weight if total_weight else 0
    reasons = [key for _, _, key in contributions]
    return {"score": round(final_score, 1), "reasons": reasons, "rsi": round(rsi, 1),
            "price": price, "sma20": sma20, "sma50": sma50, "sma200": sma200}


def calculate_support_resistance(df: pd.DataFrame):
    """المقاومة = أعلى قمة في آخر 6 أشهر، الدعم = أدنى قاع في نفس الفترة."""
    resistance = float(df["High"].max())
    support = float(df["Low"].min())
    return resistance, support


# =====================================================================
# 6) التحليل المالي الأساسي
# =====================================================================
def get_row_series(df: pd.DataFrame, possible_names):
    if df is None or df.empty:
        return None
    for name in possible_names:
        if name in df.index:
            row = df.loc[name].dropna()
            if not row.empty:
                return row
    return None


def compute_fundamentals(stock: yf.Ticker, info: dict) -> dict:
    ratios = {
        "pe_ratio": info.get("trailingPE"),
        "pb_ratio": info.get("priceToBook"),
        "dividend_yield": info.get("dividendYield"),
        "market_cap": info.get("marketCap"),
        "currency": info.get("currency", "USD"),
        "sector": info.get("sector"),
        "roe": None, "net_margin": None, "debt_equity": None,
        "current_ratio": None, "revenue_growth": None,
    }
    try:
        income = stock.financials
        balance = stock.balance_sheet
    except Exception:
        income, balance = None, None

    revenue_row = get_row_series(income, ["Total Revenue", "TotalRevenue"])
    net_income_row = get_row_series(income, ["Net Income", "NetIncome", "Net Income Common Stockholders"])
    equity_row = get_row_series(balance, ["Total Stockholder Equity", "Stockholders Equity",
                                           "Total Equity Gross Minority Interest"])
    liab_row = get_row_series(balance, ["Total Liab", "Total Liabilities Net Minority Interest",
                                         "Total Liabilities"])
    curr_assets_row = get_row_series(balance, ["Total Current Assets", "Current Assets"])
    curr_liab_row = get_row_series(balance, ["Total Current Liabilities", "Current Liabilities"])

    if revenue_row is not None and net_income_row is not None:
        try:
            ratios["net_margin"] = float(net_income_row.iloc[0]) / float(revenue_row.iloc[0]) * 100
        except (ZeroDivisionError, IndexError):
            pass
    if revenue_row is not None and len(revenue_row) >= 2:
        try:
            prev, curr = float(revenue_row.iloc[1]), float(revenue_row.iloc[0])
            if prev != 0:
                ratios["revenue_growth"] = (curr - prev) / abs(prev) * 100
        except IndexError:
            pass
    if net_income_row is not None and equity_row is not None:
        try:
            equity_val = float(equity_row.iloc[0])
            if equity_val:
                ratios["roe"] = float(net_income_row.iloc[0]) / equity_val * 100
        except (ZeroDivisionError, IndexError):
            pass
    if liab_row is not None and equity_row is not None:
        try:
            equity_val = float(equity_row.iloc[0])
            if equity_val:
                ratios["debt_equity"] = float(liab_row.iloc[0]) / equity_val
        except (ZeroDivisionError, IndexError):
            pass
    if curr_assets_row is not None and curr_liab_row is not None:
        try:
            liab_val = float(curr_liab_row.iloc[0])
            if liab_val:
                ratios["current_ratio"] = float(curr_assets_row.iloc[0]) / liab_val
        except (ZeroDivisionError, IndexError):
            pass

    return ratios


def score_fundamentals(ratios: dict, is_fund: bool) -> dict:
    if is_fund:
        return {"score": 0, "reasons": [], "usable": False}

    contributions = []
    pe = ratios.get("pe_ratio")
    if pe is not None and not np.isnan(pe):
        if pe < 0:
            contributions.append((1.0, -80, "pe_negative"))
        elif pe < 15:
            contributions.append((1.0, 80, "pe_cheap"))
        elif pe <= 25:
            contributions.append((1.0, 0, "pe_fair"))
        else:
            contributions.append((1.0, -60, "pe_expensive"))

    roe = ratios.get("roe")
    if roe is not None:
        if roe > 15:
            contributions.append((0.8, 80, "roe_strong"))
        elif roe > 5:
            contributions.append((0.8, 20, "roe_ok"))
        else:
            contributions.append((0.8, -60, "roe_weak"))

    de = ratios.get("debt_equity")
    if de is not None:
        if de < 0.5:
            contributions.append((0.6, 60, "debt_low"))
        elif de <= 1.5:
            contributions.append((0.6, 0, "debt_moderate"))
        else:
            contributions.append((0.6, -70, "debt_high"))

    margin = ratios.get("net_margin")
    if margin is not None:
        if margin > 15:
            contributions.append((0.6, 60, "margin_strong"))
        elif margin >= 0:
            contributions.append((0.6, 10, "margin_thin"))
        else:
            contributions.append((0.6, -80, "margin_negative"))

    growth = ratios.get("revenue_growth")
    if growth is not None:
        if growth > 10:
            contributions.append((0.5, 60, "growth_strong"))
        elif growth >= 0:
            contributions.append((0.5, 10, "growth_slow"))
        else:
            contributions.append((0.5, -60, "growth_negative"))

    if not contributions:
        return {"score": 0, "reasons": [], "usable": False}

    total_weight = sum(w for w, _, _ in contributions)
    final_score = sum(w * s for w, s, _ in contributions) / total_weight
    reasons = [key for _, _, key in contributions]
    return {"score": round(final_score, 1), "reasons": reasons, "usable": True}


# =====================================================================
# 7) مقارنة القطاع
# =====================================================================
# ملاحظة: هذه القوائم إرشادية لأبرز الشركات في كل قطاع/سوق، ويُنصح بمراجعتها
# وتحديثها دورياً لأن تركيبة الأسواق وتصنيفات القطاعات قد تتغير.
SECTOR_PEERS = {
    "market_us": {
        "Technology": ["AAPL", "MSFT", "GOOGL", "NVDA", "META", "ORCL"],
        "Financial Services": ["JPM", "BAC", "WFC", "GS", "MS"],
        "Healthcare": ["JNJ", "PFE", "UNH", "MRK", "ABBV"],
        "Energy": ["XOM", "CVX", "COP", "SLB"],
        "Consumer Cyclical": ["AMZN", "TSLA", "HD", "NKE", "MCD"],
        "Consumer Defensive": ["PG", "KO", "WMT", "PEP", "COST"],
        "Communication Services": ["GOOGL", "META", "NFLX", "DIS", "CMCSA"],
        "Industrials": ["BA", "CAT", "GE", "HON", "UPS"],
        "Utilities": ["NEE", "DUK", "SO"],
        "Real Estate": ["AMT", "PLD", "SPG"],
        "Basic Materials": ["LIN", "SHW", "FCX"],
    },
    "market_egx": {
        "Financial Services": ["COMI.CA", "HRHO.CA"],
        "Real Estate": ["TMGH.CA", "PHDC.CA", "OCDI.CA", "HELI.CA"],
        "Industrials": ["SWDY.CA", "ORAS.CA"],
        "Basic Materials": ["ABUK.CA", "SKPC.CA", "MFPC.CA", "AMOC.CA"],
        "Consumer Defensive": ["EAST.CA", "JUFO.CA"],
        "Communication Services": ["ETEL.CA"],
        "Technology": ["EFIH.CA"],
        "Consumer Cyclical": ["ORWE.CA", "RAYA.CA"],
        "Healthcare": ["ISPH.CA"],
    },
    "market_lse": {
        "Financial Services": ["HSBA.L", "BARC.L", "LLOY.L", "PRU.L"],
        "Energy": ["BP.L", "SHEL.L"],
        "Healthcare": ["GSK.L", "AZN.L"],
        "Consumer Defensive": ["ULVR.L", "DGE.L", "BATS.L", "TSCO.L"],
        "Communication Services": ["VOD.L"],
        "Basic Materials": ["RIO.L", "GLEN.L", "AAL.L"],
        "Industrials": ["RR.L", "BA.L"],
        "Utilities": ["NG.L", "SSE.L"],
        "Real Estate": ["LAND.L", "BLND.L"],
    },
}


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_peer_metrics(ticker: str):
    try:
        session = create_secure_session()
        t = yf.Ticker(ticker, session=session)
        info = t.info or {}
        hist = t.history(period="3mo")
        change_pct = None
        if not hist.empty and len(hist) > 1:
            first_close = float(hist["Close"].iloc[0])
            if first_close:
                change_pct = (float(hist["Close"].iloc[-1]) - first_close) / first_close * 100
        return {
            "ticker": ticker,
            "name": info.get("shortName", ticker),
            "pe": info.get("trailingPE"),
            "price_change_3m": change_pct,
        }
    except Exception:
        return None


def get_sector_comparison(ticker: str, sector: str, market_key: str, limit: int = 5):
    peers_dict = SECTOR_PEERS.get(market_key, {})
    peer_list = peers_dict.get(sector, [])
    peer_list = [p for p in peer_list if p.upper() != ticker.upper()][:limit]
    if not peer_list:
        return None
    metrics = [fetch_peer_metrics(p) for p in peer_list]
    metrics = [m for m in metrics if m is not None]
    return metrics if metrics else None


def score_sector(stock_pe, stock_change_3m, peer_metrics):
    if not peer_metrics:
        return {"score": 0, "reasons": [], "usable": False, "avg_pe": None, "avg_change": None}

    pe_values = [m["pe"] for m in peer_metrics if m.get("pe") and m["pe"] > 0]
    change_values = [m["price_change_3m"] for m in peer_metrics if m.get("price_change_3m") is not None]

    contributions = []
    avg_pe = sum(pe_values) / len(pe_values) if pe_values else None
    avg_change = sum(change_values) / len(change_values) if change_values else None

    if avg_pe and stock_pe:
        if stock_pe < avg_pe * 0.85:
            contributions.append((0.8, 60, "sector_pe_cheap"))
        elif stock_pe > avg_pe * 1.15:
            contributions.append((0.8, -50, "sector_pe_expensive"))
        else:
            contributions.append((0.8, 0, "sector_pe_inline"))

    if avg_change is not None and stock_change_3m is not None:
        if stock_change_3m > avg_change + 3:
            contributions.append((0.6, 60, "sector_outperform"))
        elif stock_change_3m < avg_change - 3:
            contributions.append((0.6, -50, "sector_underperform"))
        else:
            contributions.append((0.6, 0, "sector_inline_performance"))

    if not contributions:
        return {"score": 0, "reasons": [], "usable": False, "avg_pe": avg_pe, "avg_change": avg_change}

    total_weight = sum(w for w, _, _ in contributions)
    final_score = sum(w * s for w, s, _ in contributions) / total_weight
    reasons = [k for _, _, k in contributions]
    return {"score": round(final_score, 1), "reasons": reasons, "usable": True,
            "avg_pe": avg_pe, "avg_change": avg_change}


# =====================================================================
# 8) تحليل مشاعر الأخبار
# =====================================================================
POS_WORDS = ["growth", "profit", "profits", "surge", "soar", "beat", "upgrade", "bullish",
             "record", "rally", "gain", "expansion", "dividend increase",
             "أرباح", "نمو", "صعود", "ارتفاع", "توسع", "تفوق", "قفزة", "مكاسب"]
NEG_WORDS = ["loss", "losses", "drop", "decline", "fall", "downgrade", "bearish", "lawsuit",
             "fraud", "debt crisis", "recall", "layoff", "cut", "miss", "plunge",
             "خسائر", "تراجع", "هبوط", "انخفاض", "أزمة", "ديون", "دعوى قضائية", "تسريح"]


def extract_news_title(item: dict) -> str:
    if item.get("title"):
        return item["title"]
    content = item.get("content")
    if isinstance(content, dict):
        return content.get("title", "") or ""
    return ""


def extract_news_link(item: dict) -> str:
    if item.get("link"):
        return item["link"]
    content = item.get("content")
    if isinstance(content, dict):
        url_field = content.get("clickThroughUrl") or content.get("canonicalUrl")
        if isinstance(url_field, dict):
            return url_field.get("url", "") or ""
    return ""


def analyze_news_sentiment(news_list) -> dict:
    if not news_list:
        return {"score": 0, "headlines": [], "usable": False}
    headlines = []
    total = 0
    for item in news_list[:8]:
        title = extract_news_title(item)
        if not title:
            continue
        link = extract_news_link(item)
        title_lower = title.lower()
        item_score = 0
        for w in POS_WORDS:
            if w in title_lower:
                item_score += 1
        for w in NEG_WORDS:
            if w in title_lower:
                item_score -= 1
        total += item_score
        tag = "pos" if item_score > 0 else ("neg" if item_score < 0 else "neu")
        headlines.append({"title": title, "link": link, "tag": tag})
    if not headlines:
        return {"score": 0, "headlines": [], "usable": False}
    normalized = max(-100, min(100, total * 25))
    return {"score": normalized, "headlines": headlines, "usable": True}


# =====================================================================
# 9) التوصية النهائية (فني + مالي + قطاعي + أخبار)
# =====================================================================
def compute_final_recommendation(tech, fund, sector, news, is_fund: bool) -> dict:
    if is_fund or not fund.get("usable"):
        base_weights = {"tech": 0.55, "fund": 0.0, "sector": 0.15, "news": 0.30}
    else:
        base_weights = {"tech": 0.30, "fund": 0.30, "sector": 0.25, "news": 0.15}

    components = {
        "tech": (tech["score"], True),
        "fund": (fund["score"], fund.get("usable", False)),
        "sector": (sector["score"], sector.get("usable", False)),
        "news": (news["score"], news.get("usable", False)),
    }

    weighted_sum, total_weight = 0.0, 0.0
    for key, (score, usable) in components.items():
        if usable and base_weights[key] > 0:
            weighted_sum += base_weights[key] * score
            total_weight += base_weights[key]

    final_score = round(weighted_sum / total_weight, 1) if total_weight else 0.0

    if final_score >= 25:
        action = "buy"
    elif final_score <= -25:
        action = "sell"
    else:
        action = "hold"

    abs_score = abs(final_score)
    confidence = "high" if abs_score >= 55 else ("medium" if abs_score >= 25 else "low")

    return {
        "action": action, "confidence": confidence, "final_score": final_score,
        "tech_score": tech["score"], "fund_score": fund["score"] if fund.get("usable") else None,
        "sector_score": sector["score"] if sector.get("usable") else None,
        "news_score": news["score"] if news.get("usable") else None,
    }


# =====================================================================
# 10) تشغيل التحليل الكامل
# =====================================================================
@st.cache_data(ttl=300, show_spinner=False)
def run_full_analysis(ticker: str, market_key: str):
    session = create_secure_session()
    stock = yf.Ticker(ticker, session=session)

    df = stock.history(period="6mo")
    if df is None or df.empty or len(df) < 5:
        return None
    df = add_technical_indicators(df)

    try:
        info = stock.info or {}
    except Exception:
        info = {}

    quote_type = str(info.get("quoteType", "")).upper()
    is_fund = quote_type in ("ETF", "MUTUALFUND", "INDEX")

    tech_result = score_technicals(df)
    resistance, support = calculate_support_resistance(df)

    # ---- أعلى/أقل سعر يومي وسنوي (52 أسبوع) — من info مع احتياط من history ----
    day_high = info.get("dayHigh")
    day_low = info.get("dayLow")
    if day_high is None or day_low is None:
        day_high = float(df["High"].iloc[-1])
        day_low = float(df["Low"].iloc[-1])

    year_high = info.get("fiftyTwoWeekHigh")
    year_low = info.get("fiftyTwoWeekLow")
    if year_high is None or year_low is None:
        try:
            df_1y = stock.history(period="1y")
            if df_1y is not None and not df_1y.empty:
                year_high = float(df_1y["High"].max())
                year_low = float(df_1y["Low"].min())
        except Exception:
            pass
        if year_high is None or year_low is None:
            # احتياط أخير: أعلى/أقل قيمة متاحة في نطاق 6 أشهر فقط (قد لا تمثل 52 أسبوعاً كاملة)
            year_high = float(df["High"].max())
            year_low = float(df["Low"].min())

    ratios = compute_fundamentals(stock, info)
    fund_result = score_fundamentals(ratios, is_fund)

    sector_name = ratios.get("sector")
    peer_metrics = None
    if sector_name and not is_fund:
        peer_metrics = get_sector_comparison(ticker, sector_name, market_key)
    change_3m = None
    if len(df) > 1:
        hist_3m = df.tail(63)
        if len(hist_3m) > 1 and hist_3m["Close"].iloc[0]:
            change_3m = (hist_3m["Close"].iloc[-1] - hist_3m["Close"].iloc[0]) / hist_3m["Close"].iloc[0] * 100
    sector_result = score_sector(ratios.get("pe_ratio"), change_3m, peer_metrics)

    try:
        news_list = stock.news
    except Exception:
        news_list = []
    news_result = analyze_news_sentiment(news_list)

    recommendation = compute_final_recommendation(tech_result, fund_result, sector_result, news_result, is_fund)

    return {
        "df": df, "info": info, "quote_type": quote_type, "is_fund": is_fund,
        "tech": tech_result, "ratios": ratios, "fund": fund_result,
        "sector": sector_result, "sector_name": sector_name, "peer_metrics": peer_metrics,
        "news": news_result, "recommendation": recommendation,
        "resistance": resistance, "support": support,
        "day_high": day_high, "day_low": day_low,
        "year_high": year_high, "year_low": year_low,
    }


# =====================================================================
# 11) فحص التنبيهات لقائمة المتابعة
# =====================================================================
def check_watchlist_alerts(username: str):
    items = get_watchlist(username)
    results = []
    user_email = get_user_email(username)
    for item in items:
        ticker = item["ticker"]
        try:
            session = create_secure_session()
            stock = yf.Ticker(ticker, session=session)
            df = stock.history(period="6mo")
            if df is None or df.empty:
                results.append({**item, "status": "error"})
                continue
            resistance, support = calculate_support_resistance(df)
            price = float(df["Close"].iloc[-1])
            threshold = item["threshold"] / 100.0

            dist_to_resistance = (resistance - price) / resistance if resistance else 1
            dist_to_support = (price - support) / support if support else 1

            alert_type = None
            if resistance and dist_to_resistance <= threshold:
                alert_type = "resistance"
            elif support and dist_to_support <= threshold:
                alert_type = "support"

            if alert_type and item["email_enabled"] and user_email:
                subject = f"[Smart Investment Advisor] Alert: {ticker}"
                if alert_type == "resistance":
                    body = (f"{ticker} price ({price:.2f}) is near resistance/peak level "
                            f"({resistance:.2f}). Distance: {dist_to_resistance*100:.2f}%.")
                else:
                    body = (f"{ticker} price ({price:.2f}) is near support/bottom level "
                            f"({support:.2f}). Distance: {dist_to_support*100:.2f}%.")
                sent, msg = send_alert_email(user_email, subject, body)
            else:
                sent, msg = False, "not_triggered_or_disabled"

            results.append({
                **item, "status": "ok", "price": price, "resistance": resistance, "support": support,
                "dist_to_resistance": dist_to_resistance * 100, "dist_to_support": dist_to_support * 100,
                "alert_type": alert_type, "email_sent": sent, "email_msg": msg,
            })
        except Exception:
            results.append({**item, "status": "error"})
    return results


# =====================================================================
# 12) واجهة المستخدم
# =====================================================================
if "auth_user" not in st.session_state:
    st.session_state.auth_user = None
if "lang" not in st.session_state:
    st.session_state.lang = "العربية"
if "page" not in st.session_state:
    st.session_state.page = "analysis"

with st.sidebar:
    selected_lang_name = st.selectbox("اللغة / Language", ["العربية", "English"],
                                       index=["العربية", "English"].index(st.session_state.lang))
    st.session_state.lang = selected_lang_name

ln = LANG_DICT[selected_lang_name]

if selected_lang_name == "العربية":
    st.markdown("<style>.block-container {direction: rtl; text-align: right;}</style>",
                unsafe_allow_html=True)

st.title(ln["app_title"])
st.caption(ln["app_subtitle"])
st.markdown("---")

# ---------------------------------------------------------------
# شاشة تسجيل الدخول / إنشاء حساب
# ---------------------------------------------------------------
if not st.session_state.auth_user:
    tab_login, tab_signup = st.tabs([ln["login_title"], ln["signup_title"]])

    with tab_login:
        with st.form("login_form"):
            login_username = st.text_input(ln["username"], key="login_username")
            login_password = st.text_input(ln["password"], type="password", key="login_password")
            login_submit = st.form_submit_button(ln["btn_login"], type="primary")
        if login_submit:
            ok, email = authenticate_user(login_username, login_password)
            if ok:
                st.session_state.auth_user = login_username.strip()
                st.rerun()
            else:
                st.error(ln["err_login_failed"])

    with tab_signup:
        with st.form("signup_form"):
            su_username = st.text_input(ln["username"], key="su_username")
            su_email = st.text_input(ln["email"], key="su_email")
            su_password = st.text_input(ln["password"], type="password", key="su_password")
            su_confirm = st.text_input(ln["confirm_password"], type="password", key="su_confirm")
            signup_submit = st.form_submit_button(ln["btn_signup"], type="primary")
        if signup_submit:
            if su_password != su_confirm:
                st.error(ln["err_password_mismatch"])
            else:
                ok, code = register_user(su_username, su_password, su_email)
                if ok:
                    st.success(ln["signup_success"])
                else:
                    st.error(ln.get(code, code))

# ---------------------------------------------------------------
# التطبيق بعد تسجيل الدخول
# ---------------------------------------------------------------
else:
    with st.sidebar:
        st.write(ln["welcome"].format(name=st.session_state.auth_user))
        if st.button(ln["logout"], use_container_width=True):
            st.session_state.auth_user = None
            st.rerun()
        st.markdown("---")
        page = st.radio("📍", [ln["nav_analysis"], ln["nav_watchlist"], ln["nav_account"]],
                         label_visibility="collapsed")

    username = st.session_state.auth_user

    # ============================= صفحة التحليل =============================
    if page == ln["nav_analysis"]:
        with st.sidebar:
            st.markdown("---")
            market_key = st.radio(ln["select_market"],
                                   options=["market_us", "market_egx", "market_lse"],
                                   format_func=lambda k: ln[k])
            raw_ticker = st.text_input(ln["enter_ticker"], value="AAPL", help=ln["ticker_help"])
            analyze_clicked = st.button(ln["btn_analyze"], type="primary", use_container_width=True)

        if analyze_clicked:
            if not raw_ticker.strip():
                st.warning(ln["warn_empty_ticker"])
            else:
                final_ticker = normalize_ticker(raw_ticker, market_key)
                with st.spinner(ln["loading"]):
                    result = run_full_analysis(final_ticker, market_key)
                st.session_state["last_result"] = result
                st.session_state["last_ticker"] = final_ticker
                st.session_state["last_market_key"] = market_key

        result = st.session_state.get("last_result")
        final_ticker = st.session_state.get("last_ticker")

        if analyze_clicked and result is None:
            st.error(ln["error_fetch"])

        if result is not None and final_ticker:
            df = result["df"]
            info = result["info"]
            tech = result["tech"]
            ratios = result["ratios"]
            fund = result["fund"]
            sector = result["sector"]
            news = result["news"]
            rec = result["recommendation"]
            currency = ratios.get("currency", "USD")
            resistance, support = result["resistance"], result["support"]

            asset_label = (ln["asset_type_fund"] if result["is_fund"]
                            else ln["asset_type_equity"] if result["quote_type"] == "EQUITY"
                            else ln["asset_type_other"])
            st.subheader(f"{final_ticker} — {info.get('shortName', '')} · {asset_label}")

            last_close = float(df["Close"].iloc[-1])
            prev_close = float(df["Close"].iloc[-2]) if len(df) > 1 else last_close
            day_change_pct = ((last_close - prev_close) / prev_close * 100) if prev_close else 0

            c1, c2, c3, c4 = st.columns(4)
            c1.metric(ln["price"], f"{last_close:,.2f} {currency}")
            c2.metric(ln["day_change"], f"{day_change_pct:+.2f}%")
            mcap = ratios.get("market_cap")
            c3.metric(ln["market_cap"], f"{mcap:,.0f}" if mcap else "—")
            c4.metric(ln["currency"], currency)

            # ---------- أعلى/أقل سعر يومي وسنوي (52 أسبوع) ----------
            day_high, day_low = result.get("day_high"), result.get("day_low")
            year_high, year_low = result.get("year_high"), result.get("year_low")
            rc_d1, rc_d2 = st.columns(2)
            if day_high is not None and day_low is not None:
                rc_d1.metric(ln["day_range"], f"{day_low:,.2f} — {day_high:,.2f} {currency}")
            if year_high is not None and year_low is not None:
                rc_d2.metric(ln["year_range"], f"{year_low:,.2f} — {year_high:,.2f} {currency}")

            # ---------- مستويات الدعم والمقاومة ----------
            st.markdown(f"### {ln['levels_title']}")
            dist_res = (resistance - last_close) / resistance * 100 if resistance else None
            dist_sup = (last_close - support) / support * 100 if support else None
            lc1, lc2, lc3, lc4 = st.columns(4)
            lc1.metric(ln["resistance"], f"{resistance:,.2f} {currency}")
            lc2.metric(ln["dist_to_resistance"], f"{dist_res:.2f}%" if dist_res is not None else "—")
            lc3.metric(ln["support"], f"{support:,.2f} {currency}")
            lc4.metric(ln["dist_to_support"], f"{dist_sup:.2f}%" if dist_sup is not None else "—")

            # ---------- الرسم الفني ----------
            st.markdown(f"### {ln['chart_title']}")
            rec_marker_color = {"buy": "#1e8e3e", "sell": "#d93025", "hold": "#f29900"}[rec["action"]]
            rec_marker_symbol = {"buy": "triangle-up", "sell": "triangle-down", "hold": "circle"}[rec["action"]]

            fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                                 row_heights=[0.55, 0.2, 0.25],
                                 subplot_titles=(ln["price_panel"], ln["rsi_panel"], ln["macd_panel"]))

            fig.add_trace(go.Candlestick(x=df.index, open=df["Open"], high=df["High"],
                                          low=df["Low"], close=df["Close"], name=final_ticker,
                                          showlegend=False), row=1, col=1)
            fig.add_trace(go.Scatter(x=df.index, y=df["SMA_20"], name="SMA 20",
                                      line=dict(width=1.3)), row=1, col=1)
            fig.add_trace(go.Scatter(x=df.index, y=df["SMA_50"], name="SMA 50",
                                      line=dict(width=1.3)), row=1, col=1)
            fig.add_trace(go.Scatter(x=df.index, y=df["SMA_200"], name="SMA 200",
                                      line=dict(width=1.3, dash="dot")), row=1, col=1)
            fig.add_hline(y=resistance, line_dash="dash", line_color="#d93025",
                          annotation_text=ln["resistance"], row=1, col=1)
            fig.add_hline(y=support, line_dash="dash", line_color="#1e8e3e",
                          annotation_text=ln["support"], row=1, col=1)

            last_x = df.index[-1]
            last_y = float(df["High"].iloc[-1])
            fig.add_trace(go.Scatter(
                x=[last_x], y=[last_y * 1.02], mode="markers+text",
                marker=dict(symbol=rec_marker_symbol, size=16, color=rec_marker_color,
                            line=dict(width=1, color="white")),
                text=[ln[rec["action"]]], textposition="top center",
                textfont=dict(color=rec_marker_color, size=12),
                name=ln[rec["action"]], showlegend=False,
            ), row=1, col=1)

            fig.add_hrect(y0=70, y1=100, fillcolor="red", opacity=0.06, line_width=0, row=2, col=1)
            fig.add_hrect(y0=0, y1=30, fillcolor="green", opacity=0.06, line_width=0, row=2, col=1)
            fig.add_trace(go.Scatter(x=df.index, y=df["RSI_14"], name="RSI 14",
                                      line=dict(color="#8e44ad")), row=2, col=1)
            fig.add_hline(y=70, line_dash="dash", line_color="red", row=2, col=1)
            fig.add_hline(y=30, line_dash="dash", line_color="green", row=2, col=1)

            fig.add_trace(go.Bar(x=df.index, y=df["MACD_HIST"], name="Histogram",
                                  marker_color="gray", opacity=0.5), row=3, col=1)
            fig.add_trace(go.Scatter(x=df.index, y=df["MACD"], name="MACD",
                                      line=dict(color="#2980b9")), row=3, col=1)
            fig.add_trace(go.Scatter(x=df.index, y=df["MACD_SIGNAL"], name="Signal",
                                      line=dict(color="#e67e22")), row=3, col=1)

            fig.update_layout(
                height=800, xaxis_rangeslider_visible=False,
                legend=dict(orientation="h", y=1.06), margin=dict(t=70, b=20),
                title=dict(text=f"{final_ticker} · {ln[rec['action']]} · {ln['final_axis']}: {rec['final_score']:+.1f}",
                           font=dict(size=15, color=rec_marker_color), x=0.01),
            )
            fig.update_yaxes(title_text=currency, row=1, col=1)
            fig.update_yaxes(title_text="RSI", range=[0, 100], row=2, col=1)
            fig.update_yaxes(title_text="MACD", row=3, col=1)
            st.plotly_chart(fig, use_container_width=True)

            # ---------- التحليل المالي الأساسي ----------
            st.markdown(f"### {ln['fund_section']}")
            if not fund.get("usable"):
                st.info(ln["fund_not_available"])
            else:
                def fmt_pct(v): return f"{v:.2f}%" if v is not None else "—"
                def fmt_num(v): return f"{v:.2f}" if v is not None else "—"

                fcols = st.columns(4)
                fcols[0].metric(ln["pe"], fmt_num(ratios.get("pe_ratio")))
                fcols[1].metric(ln["pb"], fmt_num(ratios.get("pb_ratio")))
                fcols[2].metric(ln["roe"], fmt_pct(ratios.get("roe")))
                fcols[3].metric(ln["net_margin"], fmt_pct(ratios.get("net_margin")))
                fcols2 = st.columns(4)
                fcols2[0].metric(ln["debt_equity"], fmt_num(ratios.get("debt_equity")))
                fcols2[1].metric(ln["current_ratio"], fmt_num(ratios.get("current_ratio")))
                fcols2[2].metric(ln["revenue_growth"], fmt_pct(ratios.get("revenue_growth")))
                dy = ratios.get("dividend_yield")
                fcols2[3].metric(ln["dividend_yield"], fmt_pct(dy * 100) if dy else "—")

            # ---------- مقارنة القطاع ----------
            st.markdown(f"### {ln['sector_section']}")
            if result.get("sector_name"):
                st.caption(f"{ln['sector_label']}: {result['sector_name']}")
            if not sector.get("usable") or not result.get("peer_metrics"):
                st.info(ln["sector_not_available"])
            else:
                peer_rows = []
                for m in result["peer_metrics"]:
                    peer_rows.append({
                        ln["sector_col_ticker"]: m["ticker"],
                        ln["sector_col_name"]: m.get("name", m["ticker"]),
                        ln["sector_col_pe"]: f"{m['pe']:.2f}" if m.get("pe") else "—",
                        ln["sector_col_change"]: f"{m['price_change_3m']:+.2f}%" if m.get("price_change_3m") is not None else "—",
                    })
                st.dataframe(pd.DataFrame(peer_rows), use_container_width=True, hide_index=True)
                sc1, sc2 = st.columns(2)
                if sector.get("avg_pe"):
                    sc1.metric(ln["sector_avg_pe"], f"{sector['avg_pe']:.2f}")
                if sector.get("avg_change") is not None:
                    sc2.metric(ln["sector_avg_change"], f"{sector['avg_change']:+.2f}%")

            # ---------- الأخبار ----------
            st.markdown(f"### {ln['news_section']}")
            if not news.get("usable"):
                st.info(ln["no_news"])
            else:
                tag_label = {"pos": ln["sent_pos"], "neg": ln["sent_neg"], "neu": ln["sent_neu"]}
                tag_color = {"pos": "green", "neg": "red", "neu": "gray"}
                for item in news["headlines"]:
                    color, label = tag_color[item["tag"]], tag_label[item["tag"]]
                    if item["link"]:
                        st.markdown(f"- :{color}[{label}] — [{item['title']}]({item['link']})")
                    else:
                        st.markdown(f"- :{color}[{label}] — {item['title']}")

            # ---------- التوصية النهائية ----------
            st.markdown("---")
            st.markdown(f"## {ln['final_rec']}")
            action_key, color = rec["action"], {"buy": "green", "sell": "red", "hold": "orange"}[rec["action"]]
            conf_key = f"conf_{rec['confidence']}"

            rc1, rc2 = st.columns([2, 1])
            with rc1:
                st.markdown(f"### :{color}[{ln[action_key]}]")
                st.write(f"**{ln['confidence']}:** {ln[conf_key]}")
                st.write(f"**{ln['final_axis']}:** {rec['final_score']:+.1f} / 100")
            with rc2:
                st.metric(ln["tech_axis"], f"{rec['tech_score']:+.1f}")
                if rec["fund_score"] is not None:
                    st.metric(ln["fund_axis"], f"{rec['fund_score']:+.1f}")
                if rec["sector_score"] is not None:
                    st.metric(ln["sector_axis"], f"{rec['sector_score']:+.1f}")
                if rec["news_score"] is not None:
                    st.metric(ln["news_axis"], f"{rec['news_score']:+.1f}")

            st.markdown(f"**{ln['reasons_title']}**")
            all_reason_keys = (tech["reasons"] + (fund["reasons"] if fund.get("usable") else [])
                                + (sector["reasons"] if sector.get("usable") else []))
            for key in all_reason_keys:
                st.markdown(f"- {REASON_TEXT.get(key, {}).get(selected_lang_name, key)}")

            # ---------- حاسبة تكاليف الشراء/البيع/الاحتفاظ ----------
            st.markdown("---")
            st.markdown(f"### {ln['cost_calc_title']}")
            st.caption(ln["cost_calc_note"])

            current_market_key = st.session_state.get("last_market_key", "market_us")
            tax_status = "egypt_treaty"
            if current_market_key == "market_us":
                tax_status_label = st.selectbox(
                    ln["tax_residency_label"],
                    options=["egypt_treaty", "egypt_no_treaty", "us_person"],
                    format_func=lambda k: ln[f"tax_status_{k}"],
                    key="tax_status_selector",
                )
                tax_status = tax_status_label

            with st.form("cost_calc_form"):
                cc1, cc2, cc3 = st.columns(3)
                cc_qty = cc1.number_input(ln["shares_qty"], min_value=1.0, value=100.0, step=1.0)
                cc_commission_pct = cc2.number_input(ln["commission_pct"], min_value=0.0, value=0.15,
                                                       step=0.01, format="%.2f")
                cc_min_commission = cc3.number_input(ln["min_commission"], min_value=0.0, value=5.0, step=0.5)
                cc4, cc5 = st.columns(2)
                cc_vat_pct = cc4.number_input(ln["vat_pct"], min_value=0.0, value=14.0, step=1.0)
                cc_custody_pct = cc5.number_input(ln["custody_pct_annual"], min_value=0.0, value=0.0, step=0.05,
                                                    format="%.2f")
                cost_submit = st.form_submit_button("🧮")
            if cost_submit:
                costs = calculate_trade_costs(cc_qty, last_close, cc_commission_pct,
                                               cc_min_commission, cc_vat_pct, cc_custody_pct)
                cr1, cr2, cr3, cr4 = st.columns(4)
                cr1.metric(ln["buy_cost_total"], f"{costs['buy_cost_total']:,.2f} {currency}")
                cr2.metric(ln["sell_proceeds_total"], f"{costs['sell_proceeds_total']:,.2f} {currency}")
                cr3.metric(ln["breakeven_price"], f"{costs['breakeven_price']:,.2f} {currency}")
                cr4.metric(ln["roundtrip_cost_pct"], f"{costs['roundtrip_cost_pct']:.2f}%")
                if cc_custody_pct > 0:
                    st.caption(f"{ln['custody_annual_cost']}: {costs['custody_annual_cost']:,.2f} {currency}")
                if dist_res is not None and costs["roundtrip_cost_pct"] > dist_res * 0.5:
                    st.warning(ln["cost_warning"])

                # ---------- الوضع الضريبي المتوقع ----------
                st.markdown(f"#### {ln['tax_section_title']}")
                tax_info = get_tax_info(current_market_key, tax_status)
                div_yield = ratios.get("dividend_yield")

                tx1, tx2 = st.columns(2)
                wht_pct = tax_info["dividend_wht_pct"]
                tx1.metric(ln["dividend_wht_label"], f"{wht_pct:.0f}%" if wht_pct is not None else "—")

                if div_yield and wht_pct is not None:
                    annual_dividend = cc_qty * last_close * div_yield
                    dividend_tax = annual_dividend * (wht_pct / 100.0)
                    net_dividend = annual_dividend - dividend_tax
                    tx2.metric(ln["net_annual_dividend"], f"{net_dividend:,.2f} {currency}")
                    st.caption(f"{ln['estimated_annual_dividend']}: {annual_dividend:,.2f} {currency} · "
                               f"{ln['estimated_dividend_tax']}: {dividend_tax:,.2f} {currency}")
                elif not div_yield:
                    st.caption(ln["no_dividend_data"])

                if tax_info["div_note"]:
                    st.markdown(f"- {ln[tax_info['div_note']]}")
                if tax_info["cg_note"]:
                    st.markdown(f"**{ln['capital_gains_label']}:** {ln[tax_info['cg_note']]}")
                st.caption(ln["tax_disclaimer"])

            # ---------- حاسبة مصاريف التحويل البنكي ----------
            st.markdown(f"### {ln['transfer_calc_title']}")
            st.caption(ln["transfer_calc_note"])
            with st.form("transfer_calc_form"):
                tc1, tc2, tc3 = st.columns(3)
                tc_amount = tc1.number_input(ln["transfer_amount"], min_value=0.0, value=1000.0, step=50.0)
                tc_flat_fee = tc2.number_input(ln["transfer_flat_fee"], min_value=0.0, value=25.0, step=1.0)
                tc_fx_margin = tc3.number_input(ln["transfer_fx_margin_pct"], min_value=0.0, value=1.5,
                                                  step=0.1, format="%.1f")
                transfer_submit = st.form_submit_button("💱")
            if transfer_submit:
                transfer_res = calculate_transfer_cost(tc_amount, tc_flat_fee, tc_fx_margin)
                tr1, tr2 = st.columns(2)
                tr1.metric(ln["transfer_total_cost"], f"{transfer_res['total_cost']:,.2f}")
                tr2.metric(ln["transfer_net_amount"], f"{transfer_res['net_amount']:,.2f}")

            # ---------- تنفيذ الصفقة (شبه آلي) ----------
            st.markdown("---")
            st.markdown(f"### {ln['execute_section_title']}")
            st.caption(ln["execute_section_note"])
            platforms = EXECUTION_PLATFORMS.get(st.session_state.get("last_market_key", "market_us"), [])
            if platforms:
                ex_cols = st.columns(len(platforms))
                for col, (platform_name, platform_url) in zip(ex_cols, platforms):
                    col.link_button(ln["execute_btn_label"].format(platform=platform_name), platform_url)
            st.caption(ln["execute_disclaimer"])

            # ---------- إضافة لقائمة المتابعة ----------
            st.markdown("---")
            st.markdown(f"### {ln['add_watchlist_title']}")
            with st.form("add_watchlist_form"):
                wc1, wc2 = st.columns(2)
                threshold_pct = wc1.slider(ln["alert_threshold"], min_value=1, max_value=15, value=3)
                email_enabled = wc2.checkbox(ln["email_alerts_toggle"], value=True)
                add_submit = st.form_submit_button(ln["btn_add_watchlist"], type="primary")
            if add_submit:
                add_to_watchlist(username, final_ticker, st.session_state["last_market_key"],
                                  threshold_pct, email_enabled)
                st.success(ln["added_to_watchlist"].format(ticker=final_ticker))

    # ============================= صفحة قائمة المتابعة =============================
    elif page == ln["nav_watchlist"]:
        st.markdown(f"## {ln['watchlist_title']}")
        items = get_watchlist(username)
        if not items:
            st.info(ln["watchlist_empty"])
        else:
            check_now = st.button(ln["btn_check_alerts"], type="primary")
            if check_now or "watchlist_results" not in st.session_state:
                with st.spinner(ln["loading"]):
                    st.session_state["watchlist_results"] = check_watchlist_alerts(username)

            if not get_smtp_config():
                st.info(ln["email_not_configured"])

            for res in st.session_state.get("watchlist_results", []):
                if res.get("status") != "ok":
                    st.warning(f"{res['ticker']}: {ln['error_fetch']}")
                    continue
                with st.container(border=True):
                    top_cols = st.columns([2, 1, 1, 1, 1])
                    top_cols[0].markdown(f"**{res['ticker']}** ({ln[res['market_key']]})")
                    top_cols[1].metric(ln["price"], f"{res['price']:.2f}")
                    top_cols[2].metric(ln["resistance"], f"{res['resistance']:.2f}")
                    top_cols[3].metric(ln["support"], f"{res['support']:.2f}")
                    if top_cols[4].button(ln["btn_remove"], key=f"rm_{res['ticker']}"):
                        remove_from_watchlist(username, res["ticker"])
                        st.rerun()

                    if res["alert_type"] == "resistance":
                        st.warning(ln["alert_near_resistance"] +
                                   f" ({ln['dist_to_resistance']}: {res['dist_to_resistance']:.2f}%)")
                    elif res["alert_type"] == "support":
                        st.warning(ln["alert_near_support"] +
                                   f" ({ln['dist_to_support']}: {res['dist_to_support']:.2f}%)")
                    else:
                        st.caption(ln["no_alert"])

                    if res.get("email_sent"):
                        st.caption(ln["email_sent"])
                    elif res["alert_type"] and res["email_enabled"] and get_smtp_config():
                        st.caption(ln["email_failed"])

    # ============================= صفحة الحساب =============================
    elif page == ln["nav_account"]:
        st.markdown(f"## {ln['account_title']}")
        st.write(f"**{ln['account_username']}:** {username}")
        st.write(f"**{ln['account_email']}:** {get_user_email(username)}")

st.markdown("---")
st.caption(ln["disclaimer"])
