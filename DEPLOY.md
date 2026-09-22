# راهنمای انتشار روی سرور (Production)

## ۱. پیش‌نیازها

- سرور لینوکس با Docker و Docker Compose
- دامنه/زیردامنه برای ویجت، مثل `widget.nikravan.org` با گواهی HTTPS (Certbot / Nginx Proxy Manager)
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
    server_name widget.nikravan.org;

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

1. وارد `/admin` شوید (سوپر ادمین) و رمزها را از «تغییر رمز» عوض کنید.
2. کراول اولیه خودکار اجرا می‌شود؛ وضعیت آن را از داشبورد نیک‌روان (بخش کراول) ببینید.
3. اکسل مشاوران را از پنل آپلود کنید تا دیتای واقعی جای seed بنشیند.
4. کد embed سایت مشتری را با `data-origin="https://widget.nikravan.org"` به‌روز کنید.

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
