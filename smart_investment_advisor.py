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
تطبيق Streamlit متكامل لتحليل الأسهم والصناديق في بورصات مصر وأمريكا ولندن
"""

st.set_page_config(page_title="Smart Investment Advisor", page_icon="📈", layout="wide")

# =====================================================================
# 0) قاعدة البيانات (Supabase / PostgreSQL)
# =====================================================================
@st.cache_resource
def get_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    options = ClientOptions(schema="public")
    return create_client(url, key, options=options)

# =====================================================================
# 1) الأمان والمصادقة
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
# 2) البريد الإلكتروني
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
        "rsi_panel": "مؤشار القوة النسبية RSI",
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
        "fund_not_available": "⚠️ لا تتوفر بيانات قوائم مالية تفصيلية لهذه الأداة، سيعتمد التحليل بشكل أكبر على الجانب الفني والأخبار.",
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
