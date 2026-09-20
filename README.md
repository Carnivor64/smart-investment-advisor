# Smart Investment Advisor — دليل التشغيل والرفع المجاني

## 1) إنشاء قاعدة البيانات على Supabase (مطلوب قبل أي تشغيل)

التطبيق الآن يخزن الحسابات وقوائم المتابعة على قاعدة بيانات سحابية مجانية
(Supabase) بدل ملف محلي، عشان يقدر يوصلها التطبيق وأي مهمة خلفية منفصلة
لاحقًا بنفس البيانات.

1. أنشئ حسابًا مجانيًا على https://supabase.com وسجّل الدخول.
2. اضغط "New project"، اختر اسمًا وكلمة مرور لقاعدة البيانات (احفظها)، واختر
   أقرب منطقة جغرافية، ثم أنشئ المشروع (يستغرق دقيقة أو اثنتين).
3. من القائمة الجانبية اختر "SQL Editor"، الصق الكود التالي بالكامل، واضغط
   "Run":

```sql
create table if not exists users (
    username text primary key,
    password_hash text not null,
    salt text not null,
    email text not null,
    created_at timestamptz not null default now()
);

create table if not exists watchlist (
    id bigint generated always as identity primary key,
    username text not null references users(username) on delete cascade,
    ticker text not null,
    market_key text not null,
    alert_threshold_pct real not null default 3.0,
    email_alerts_enabled boolean not null default true,
    created_at timestamptz not null default now(),
    unique (username, ticker)
);
```

4. من القائمة الجانبية اختر "Project Settings" → "API". هتحتاج قيمتين:
   - **Project URL** (رابط يبدأ بـ `https://....supabase.co`)
   - **anon public key** (مفتاح طويل تحت "Project API keys")

احتفظ بهذين القيمتين، هتستخدمهم في الخطوة التالية (Secrets).

## 2) إعداد بيانات الاتصال (Secrets)

**للتشغيل المحلي:** أنشئ مجلد `.streamlit` بجانب ملف الكود، وجواه ملف
`secrets.toml` بالمحتوى التالي (لا ترفعه لـ GitHub — أضفه لملف `.gitignore`):

```toml
[supabase]
url = "https://xxxxxxxxxxxx.supabase.co"
key = "your-anon-public-key-here"

[smtp]
server = "smtp.gmail.com"
port = 587
sender_email = "your_email@gmail.com"
sender_password = "your_app_password_here"
```

قسم `[smtp]` اختياري (لتنبيهات البريد الإلكتروني)، أما `[supabase]` فمطلوب
حتى يعمل تسجيل الدخول وقائمة المتابعة أصلاً.

**على Streamlit Community Cloud:** بعد نشر التطبيق، من إعداداته اضغط
"Secrets" والصق نفس المحتوى أعلاه.

## 3) التشغيل محلياً
```bash
pip install -r requirements.txt
streamlit run smart_investment_advisor.py
```

## 4) الرفع المجاني (Streamlit Community Cloud)
1. أنشئ حساب مجاني على https://share.streamlit.io باستخدام حساب GitHub.
2. ارفع الملفات الثلاثة (`smart_investment_advisor.py`, `requirements.txt`,
   وهذا الملف) إلى مستودع (repository) جديد على GitHub.
3. من لوحة Streamlit Cloud اختر "New app"، وحدد المستودع والملف الرئيسي
   `smart_investment_advisor.py`.
4. أضف بيانات Secrets كما في الخطوة 2 قبل أو بعد أول نشر.
5. بعد النشر ستحصل على رابط عام (مثال: `https://your-app-name.streamlit.app`)
   شاركه مع أصدقائك مباشرة — مجاناً بالكامل.

## 5) ملاحظات مهمة حول التنبيهات
- التنبيهات تُفحص حالياً عند فتح صفحة "قائمة المتابعة" أو عند الضغط على زر
  "افحص التنبيهات الآن" فقط — التطبيق لا يعمل في الخلفية بشكل مستمر بذاته.
- **الخطوة التالية المخطط لها:** إضافة مهمة مجدولة مستقلة عبر GitHub Actions
  تفحص قائمة المتابعة وترسل التنبيهات تلقائياً كل فترة زمنية ثابتة، بغض النظر
  عن فتح التطبيق من عدمه — أصبحت ممكنة الآن بعد نقل البيانات لـ Supabase،
  وستُضاف في تحديث لاحق.
- حتى ذلك الحين، لضمان استلام تنبيه فعلي، يحتاج المستخدم لفتح التطبيق بشكل
  دوري، أو ضبط خدمة مجانية (مثل cron-job.org) لزيارة رابط التطبيق دورياً.

## 6) الوضع الضريبي
التطبيق يعرض تقديرًا لضريبة التوزيعات والأرباح الرأسمالية حسب السوق وحالتك
الضريبية (مبني على معاهدة الازدواج الضريبي مصر-أمريكا، وقواعد غير المقيمين
الأمريكية، وقانون الإعفاء المصري الصادر يوليو 2026). هذه معلومات عامة وليست
استشارة ضريبية شخصية — راجع محاسبًا مختصًا لوضعك الدقيق.

## 7) تنويه
هذا التطبيق أداة تحليل آلية تعليمية وليس نصيحة استثمارية أو مالية مُلزمة.
