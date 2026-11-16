# راهنمای انتشار روی سرور (Production)

## ۱. پیش‌نیازها

- سرور لینوکس با Docker و Docker Compose
- دامنه/زیردامنه برای ویجت، مثل `widget.example.com` با گواهی HTTPS (Certbot / Nginx Proxy Manager)
- فایل `.env` طبق `.env.example` — **حتماً این موارد را عوض کنید:**
  - `SECRET_KEY`: خروجی `python -c "import secrets; print(secrets.token_hex(32))"`
  - `SUPER_ADMIN_PASSWORD` و `ADMIN_PANEL_PASSWORD`: رمزهای قوی
  - `COOKIE_SECURE=1` (چون پشت HTTPS است)
  - `CORS_ORIGINS`: دقیقاً همان دامنه(ها)یی که ویجت در آن‌ها embed می‌شود

## ۲. اجرا

```bash
docker compose up -d --build
docker compose logs -f widget   # بررسی لاگ
curl http://127.0.0.1:8000/api/chat/health   # باید {"status":"ok"} بدهد
```

## ۳. Nginx (reverse proxy)

```nginx
server {
    listen 443 ssl;
    server_name widget.example.com;

    client_max_body_size 12m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## ۴. بعد از انتشار

1. ⚠️ **فوری — تخصیص پلن:** وارد `/admin/super/plans` شوید، حداقل یک پلن بسازید (برای شروع: نوع «ضعیف» با سقف نامحدود) و به **همه** مشتری‌ها تخصیص دهید. **بدون پلن، ویجت مشتری‌ها قفل است** و کاربران پیام «ظرفیت تکمیل» می‌بینند.
2. رمزها را از «تغییر رمز» عوض کنید.
3. کراول اولیه خودکار اجرا می‌شود؛ وضعیت از داشبورد مرکز نمونه قابل بررسی است.
4. اکسل مشاوران را از پنل آپلود کنید؛ برای بازبیست فایل قبلی، دکمه «دانلود آخرین فایل بارگذاری‌شده (خام)» در همان صفحه است.
5. کد embed را با `data-origin="https://widget.example.com"` به‌روز کنید (خودِ برچسب تغییری نکرده).

### فعال‌سازی LLM برای یک مشتری

1. در `/admin/super/plans` پلن جدید با نوع «متوسط» یا «پیشرفته» بسازید.
2. `base URL` (آدرس endpoint سازگار با OpenAI، مثل `https://api.example.com/v1`)، `model` و `API key` را وارد و با «تست اتصال» بررسی کنید.
3. سقف پیام و سقف معرفی را مشخص کنید (`-1` یا خالی = نامحدود).
4. در صفحه مشتری‌ها، پلن را به مستأجر تخصیص دهید (شمارنده‌ها ریست می‌شود).

## ۵. نگهداری

- بکاپ: پوشه `data/` (هر سه فایل `.db`) را روزانه بکاپ بگیرید.
- به‌روزرسانی: `git pull && docker compose up -d --build`
- سشن‌های منقضی هر ساعت خودکار پاک می‌شوند؛ پیام‌های قدیمی‌تر از ۳۰ روز هم پاک می‌شوند.

## ۶. چک‌لیست امنیتی

- [ ] `SECRET_KEY` تصادفی و محرمانه
- [ ] `COOKIE_SECURE=1` با HTTPS
- [ ] `CORS_ORIGINS` محدود به دامنه‌های واقعی (بدون `*`)
- [ ] رمزهای قوی (۸+ کاراکتر) برای هر دو نقش
- [ ] `allowed_domains` هر مشتری در پنل سوپر ادمین تنظیم شده
- [ ] فایل `.env` در git نیست (در `.gitignore` است)
