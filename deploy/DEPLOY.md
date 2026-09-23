# Deploying to EC2

The requirement: **once this is live, the only way to see a dashboard is to log
in.** One Django project makes that much easier to guarantee than it was with
three services — there is no second port to leave open by accident.

```
        internet
            │
            │  security group: 443 only              ← layer 1
            ▼
   ┌────────────────────┐
   │ nginx  :443 (TLS)  │                            ← layer 2
   └─────────┬──────────┘
             │ 127.0.0.1:8080
             ▼
   ┌──────────────────────────────────┐
   │ excellar.service (gunicorn)      │              ← layer 3
   │   every view @login_required     │
   │   /d/nav/  ·  /d/callspread/     │
   └──────────────────────────────────┘
             ▲                    ▲
   callspread-poll.service   excellar-scheduler.service
   (STS → data/)             (Huey → NAV snapshots)
   no ports                  no ports
```

| # | Layer | What it stops | Verify with |
|---|-------|---------------|-------------|
| 1 | Security group allows only 443 (+22 from your IP) | Reaching the app on any other port | `nmap` from off-box |
| 2 | nginx terminates TLS | Credentials read off the wire | `curl -I http://host` → 301 |
| 3 | Every view requires a session | A logged-out user opening a dashboard URL | `curl -sI https://host/d/nav/` → 302 |

The background services listen on **nothing** — they only read APIs and write to
`data/`. That is the structural win over the previous setup: there is no
dashboard port that could be exposed.

---

## 1. Instance and security group

Ubuntu 22.04+, t3.small or larger. Inbound rules — **only these**:

| Type | Port | Source |
|------|------|--------|
| HTTPS | 443 | 0.0.0.0/0 |
| HTTP | 80 | 0.0.0.0/0 — needed for the certbot challenge and the redirect |
| SSH | 22 | your IP only, never 0.0.0.0/0 |

Do not add 8080. Nothing else listens.

## 2. System packages

```bash
sudo apt update
sudo apt install -y nginx python3-venv python3-pip postgresql redis certbot python3-certbot-nginx
sudo useradd --system --home /opt/excellar excellar
```

Redis is only for the NAV scheduler. Postgres is optional — see step 4.

## 3. The application

```bash
sudo mkdir -p /opt/excellar && sudo chown excellar:excellar /opt/excellar
# copy this project to /opt/excellar, then:
sudo -u excellar bash -c '
  cd /opt/excellar
  python3 -m venv .venv
  .venv/bin/pip install -r requirements-prod.txt
'
```

## 4. Configuration

Write `/opt/excellar/.env`:

```ini
DEBUG=false
SECRET_KEY=<generate below>
ALLOWED_HOSTS=dash.example.com
CSRF_TRUSTED_ORIGINS=https://dash.example.com

# Keep the NAV history you already have by pointing at the existing database.
# Omit entirely to use SQLite at data/excellar.sqlite3.
DATABASE_URL=postgres://navdash:pass@localhost:5432/navdash

REDIS_URL=redis://localhost:6379/0

# CallSpread — without these it serves the demo book.
STS_CLIENT_ID=...
STS_CLIENT_SECRET=...

# NAV custody / exchanges / RPCs — copy from the old navdash config/.env
BITGO_ACCESS_TOKEN=...
```

```bash
.venv/bin/python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
```

`SECRET_KEY` is the login. Anyone holding it can mint a session cookie for any
account, so treat it like a password and rotate it if exposed (rotating signs
everyone out, which is the point).

Copy `config/config.yaml` and, if you use the Google Sheets export, the
`credentialsNAV.json` / `token_summary.pickle` files referenced by `NAV_REPORT`.

```bash
sudo -u excellar bash -c '
  cd /opt/excellar
  .venv/bin/python manage.py migrate
  .venv/bin/python manage.py collectstatic --noinput
  .venv/bin/python manage.py seed_dashboards
  .venv/bin/python manage.py createsuperuser
'
```

`migrate` runs the startup checks. A leftover dev `SECRET_KEY` or
`ALLOWED_HOSTS=*` stops it here with a `portal.E00x` error rather than starting
up insecure.

If you are moving CallSpread's history across, copy it before first start:

```bash
sudo -u excellar mkdir -p /opt/excellar/data/callspread
sudo -u excellar cp history.jsonl /opt/excellar/data/callspread/history.jsonl
```

## 5. Services

```bash
sudo cp deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now excellar callspread-poll excellar-scheduler
systemctl status excellar --no-pager
```

Skip `excellar-scheduler` if you are not running NAV's periodic collection, and
`callspread-poll` if you have no STS credentials. The web app runs either way.

## 6. nginx and TLS

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/excellar
sudo sed -i 's/dash.example.com/YOUR.DOMAIN/g' /etc/nginx/sites-available/excellar
sudo ln -sf /etc/nginx/sites-available/excellar /etc/nginx/sites-enabled/excellar
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d YOUR.DOMAIN
```

## 7. Prove it

```bash
# On the box: is anything listening beyond loopback?
sudo ss -tlnp | grep -v '127.0.0.1\|::1'
#   expect only nginx on :80 and :443

sudo -u excellar /opt/excellar/.venv/bin/python /opt/excellar/manage.py check_exposure
#   expect "no separate port to bypass" for both dashboards
```

```bash
# From your laptop, NOT the instance.
nmap -Pn -p 22,80,443,8080 YOUR.DOMAIN
#   expect 8080 filtered/closed

curl -sI https://YOUR.DOMAIN/d/nav/                 | head -1   # 302
curl -sI https://YOUR.DOMAIN/d/callspread/          | head -1   # 302
curl -sI https://YOUR.DOMAIN/d/callspread/api/state | head -1   # 302
curl -sI https://YOUR.DOMAIN/api/nav/snapshots/     | head -1   # 403
curl -sI http://YOUR.DOMAIN/                        | head -1   # 301 to https
```

A `200` on any of those is a bypass — stop and fix it before handing out
accounts.

## 8. Accounts

Sign in at `https://YOUR.DOMAIN`, then create the team at `/users/`. Every user
sees every dashboard; `is_staff` additionally grants user management and admin.

---

## Things that will bite you

**Enable the services** or nothing comes back after a reboot. Verify with
`sudo reboot` once, before people depend on it.

**Back up the database.** With SQLite that is `data/excellar.sqlite3` — it holds
every account. With Postgres, `pg_dump`. Also back up
`data/callspread/history.jsonl`; it is not in the database.

**Two dashboards, two data paths.** NAV reads from the database, so if its
scheduler is not running the numbers go stale silently. CallSpread reads
`data/callspread/latest.json`, and the page shows how long ago it was written —
if that climbs, `callspread-poll` has stopped. Check both:

```bash
systemctl status callspread-poll excellar-scheduler --no-pager
curl -s https://YOUR.DOMAIN/d/callspread/api/health   # needs a session
```

**Timezone.** Run the instance in UTC (the default). CallSpread's `openedAt`
renders from the host's local zone — a quirk inherited from the Node original.

**One process serves both dashboards now.** That is simpler, but it also means a
restart interrupts both. `Restart=always` covers crashes; deploys are a
deliberate blip.