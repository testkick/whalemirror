# WhaleMirror

A deployable console that finds consensus bets among Polymarket's top-performing
wallets and lets you mirror them — manually per signal, or automatically with
hard spend caps. FastAPI backend, static frontend, SQLite state. Deploys to
Railway in a couple of minutes, or anywhere Docker runs.

## How it works

1. Every sweep pulls the 30d and all-time profit leaderboards, filters out
   market-maker profiles (ROI floor), and keeps consistent performers.
2. Each whale's open positions are pulled and conviction-filtered.
3. Positions are aggregated by (market, outcome). A signal requires 3+ distinct
   whales on the same side with ≥75% dollar dominance over the opposing side.
4. Signals are scored (breadth × dominance × whale quality × capital) and shown
   in the console with a depth gauge: whale average entry vs current price.
5. Mirroring buys the signal's outcome token via the CLOB API using your
   Polymarket credentials — or simulates it in dry-run mode (the default).

## Guardrails

- **Dry run is on by default** and cannot be turned off until credentials exist.
- **Per-trade cap** and **rolling daily cap** (live orders only).
- **Slippage guard**: skips if the live price moved past the signal price.
- **One auto-mirror per signal**, ever. Manual mirrors always confirm first.
- Private key is **encrypted at rest** (Fernet, keyed from `APP_SECRET`) and is
  **never returned to the browser** after saving.

## Deploy on Railway

1. **Push this repo to GitHub** (private repo recommended).

2. **Create the service**: Railway dashboard → New Project → Deploy from GitHub
   repo → pick this repo. Railway detects the Dockerfile and `railway.json`
   automatically.

3. **Set environment variables** (service → Variables):

   | Variable | Value |
   |---|---|
   | `APP_PASSWORD` | Console login — long and random (`openssl rand -base64 32`) |
   | `APP_SECRET` | Encrypts the trading key at rest — 32+ random chars |
   | `DB_PATH` | `/data/whalemirror.db` |

4. **Attach a volume** (service → right-click / Settings → Volumes → New
   Volume), mount path `/data`. Without this, signals, activity history, and
   saved credentials are wiped on every redeploy.

5. **Expose it**: service → Settings → Networking → Generate Domain. Railway
   terminates HTTPS for you — no reverse proxy needed.

Open the generated `*.up.railway.app` URL, sign in with `APP_PASSWORD`, and hit
**Run sweep**. Pushing to the GitHub repo redeploys automatically.

Notes:
- The app binds to Railway's injected `PORT` automatically.
- `/healthz` is the health check endpoint (configured in `railway.json`).
- Session cookies are `Secure` by default; that's correct behind Railway's
  HTTPS. For plain-HTTP local dev set `COOKIE_SECURE=false`.

## Run locally

```bash
cp .env.example .env   # set APP_PASSWORD and APP_SECRET
pip install -r requirements.txt
COOKIE_SECURE=false DB_PATH=./whalemirror.db \
  APP_PASSWORD=dev APP_SECRET=dev-secret-32-characters-minimum \
  uvicorn app.main:app --reload
# → http://localhost:8000
```

Or with Docker Compose (see `docker-compose.yml`, which also includes an
optional Caddy service for self-hosting on a VM such as OCI — steps in
`docs/OCI.md` if you go that route).

## Going live (real orders)

1. Settings → Trading credentials: paste your Polymarket private key
   (Polymarket → Settings → Export Private Key), your wallet address, and the
   signature type (1 for email accounts, 2 for browser wallets).
2. Set per-trade size and daily cap to numbers you can genuinely afford to lose.
3. Turn off dry run. The mode badge flips to LIVE.
4. Optionally enable auto-mirror with a score floor.

**Strong recommendation:** use a dedicated Polymarket account funded only with
your mirroring budget, not your main wallet. The key on this server can trade
everything the account holds. Keep dry run on for a few sweep cycles first and
sanity-check the activity log.

## Operational notes

- The leaderboard endpoint's parameter names have changed over the API's
  lifetime; `app/consensus.py` tries three known variants. If a sweep fails
  with a leaderboard error, check https://docs.polymarket.com and adjust the
  variant list — one line.
- Rotating `APP_SECRET` invalidates stored credentials (by design) — re-enter
  them after rotation.
- Railway logs (service → Deployments → View logs) show sweep errors; order
  failures also land in the Activity tab with full detail.
- Sweeps make one API call per whale with a polite delay, so a 50-whale sweep
  takes a minute or two. The console shows live progress.

## Disclaimer

Signals are derived from public on-chain data and are informational. Mirroring
places real orders when dry run is off. This is not financial advice; past
whale performance does not predict future results.
