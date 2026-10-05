# Production Deployment Guide

## 1. Prerequisites

- A Linux server with Docker and Docker Compose
- A domain or subdomain for the widget, e.g. `widget.nikravan.org`, with a valid HTTPS
  certificate (Certbot / Nginx Proxy Manager)
- A `.env` file based on `.env.example` — **you must change all of these:**
  - `SECRET_KEY`: generate with `python -c "import secrets; print(secrets.token_hex(32))"`
  - `SUPER_ADMIN_PASSWORD` and `ADMIN_PANEL_PASSWORD`: long random passwords
  - `COOKIE_SECURE=1` (the site runs behind HTTPS)
  - `CORS_ORIGINS`: exactly the domain(s) where the widget will be embedded — never `*`

## 2. Run

```bash
docker compose up -d --build
docker compose logs -f widget          # check logs
curl http://127.0.0.1:8000/api/chat/health   # must return {"status":"ok"}
```

## 3. Nginx (reverse proxy)

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

## 4. After deploying

1. Sign in to `/admin` as the super admin and change both passwords from **Change password**.
2. The initial crawl runs automatically; monitor it from the tenant dashboard
   (Availability crawl section).
3. Upload the real consultant Excel file from the panel so production data replaces the seed data.
4. Update the embed snippet on tenant sites with
   `data-origin="https://widget.nikravan.org"`.

## 5. Maintenance

- **Backups:** back up the `data/` folder (all three `.db` files) daily.
- **Updates:** `git pull && docker compose up -d --build`
- Expired sessions are cleaned automatically every hour, and messages older than
  30 days are purged.

## 6. Security checklist

- [ ] `SECRET_KEY` is random and secret
- [ ] `COOKIE_SECURE=1` with HTTPS
- [ ] `CORS_ORIGINS` limited to real domains (no `*`)
- [ ] Strong passwords (8+ characters) for both roles
- [ ] `allowed_domains` configured for each tenant in the super-admin panel
- [ ] `.env` is not tracked by git (it is in `.gitignore`)
