# Personal Web Monitor

Self-hosted page change detector. FastAPI + SQLite + APScheduler. Polls URLs with httpx, diffs normalized text, and sends Telegram alerts.

Keep `--workers 1`. The scheduler runs inside the app process.

## Local

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 and sign in with `DASHBOARD_USER` / `DASHBOARD_PASSWORD`.

Telegram: create a bot with @BotFather, put the token in `.env`, and set `TELEGRAM_CHAT_ID` from @userinfobot. Message the bot once before alerts will deliver.

The first successful check stores a baseline and does not alert. Later checks alert on the selected trigger.

## Server

```bash
sudo mkdir -p /var/www/webmon
sudo chown "$USER":www-data /var/www/webmon
git clone git@github.com:MaxHuiskes/webmon.git /var/www/webmon
cd /var/www/webmon
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env before the chown below
sudo chown -R www-data:www-data /var/www/webmon
sudo cp deploy/webmon.service /etc/systemd/system/webmon.service
sudo systemctl daemon-reload
sudo systemctl enable --now webmon
```

Nginx:

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp deploy/webmon.nginx.conf /etc/nginx/sites-available/webmon.conf
sudo ln -s /etc/nginx/sites-available/webmon.conf /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d monitor.yourdomain.com
sudo ufw allow 'Nginx Full'
```

Replace `monitor.yourdomain.com` in the Nginx file or `Caddyfile` before reloading the proxy. Uvicorn stays on `127.0.0.1:8001`.
