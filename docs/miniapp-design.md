# FarmWatch Telegram Mini App — design proposal

Status: proposal for review. Lives on the `miniapp` branch only.

## What a Telegram Mini App is

A Mini App is an HTTPS web page that Telegram loads inside its own in app browser when
the user taps a button in the bot. It is the "Open" experience the user remembers. The
page gets a JavaScript bridge at `window.Telegram.WebApp` that provides:

* `initData` / `initDataUnsafe`: signed data identifying the Telegram user. The raw
  `initData` string is sent to our backend and validated with HMAC SHA256 using the bot
  token as the key (constant `WebAppData`). This is how we authenticate the user.
* `themeParams` and `colorScheme`: the user's Telegram colors, so the app matches their
  light or dark theme automatically.
* `expand()`, `ready()`, `close()`, viewport info.
* `MainButton` / `BottomButton`, `BackButton`, `HapticFeedback`.

Ways to open it from the bot: a Menu button set in BotFather, a direct link
`t.me/<bot>/<app>`, an inline button, or a keyboard `web_app` button. For us the Menu
button (or an inline "Open dashboard" button) fits best.

Hard requirement: the page must be served over HTTPS with a valid certificate.

## Our case, and the one real problem

The farm data lives on the farm PC (the Bambu client plus our monitor). The existing
FastAPI app already serves all of it on `127.0.0.1:<port>`. A Mini App, however, is
loaded by Telegram on the user's phone from a public URL. So the farm PC must be
reachable over public HTTPS. That is the only genuinely new piece.

Recommended: Cloudflare Tunnel (`cloudflared`).
* Free, gives a stable HTTPS subdomain, no port forwarding, no firewall changes.
* Runs as a small service on the farm PC pointing at the local uvicorn.
* The bot's Menu button points at `https://<tunnel-host>/app`.

Alternatives considered: ngrok (URL not stable on the free tier), a cloud relay the PC
pushes to (more infra, more moving parts). Tunnel is the simplest robust option and
reuses everything we already have.

## Architecture (reuses the current app)

```
 phone (Telegram)                      farm PC
 ┌──────────────┐   HTTPS   ┌──────────────────────────────┐
 │  Mini App    │◀─────────▶│ cloudflared tunnel           │
 │  /app        │  tunnel   │   → uvicorn 127.0.0.1:<port>  │
 │  WebApp SDK  │           │   FastAPI (web/server.py)     │
 └──────────────┘           │   app.state.monitor (live)    │
        │ initData          └──────────────────────────────┘
        ▼
   POST /api/miniapp/metrics  (validate initData → check allowed_users → return metrics)
```

New backend bits (small):
* `GET /app`: serves the mobile Mini App HTML/JS (separate from the desktop panel).
* `POST /api/miniapp/metrics`: body carries `initData`; we validate the HMAC and the
  `auth_date` freshness, confirm the user id is in `telegram.allowed_users`, then return
  the same metrics the panel already builds.
* On bot start, optionally call `setChatMenuButton` so the bot shows an Open button.

No change to how the monitor works. The Mini App is a second, mobile first view of the
same `app.state.monitor` data, gated by Telegram identity.

## Mockup (mobile, themed to the user's Telegram)

```
┌──────────────────────────────┐
│ ▌FarmWatch            ⟳  live │   header, uses themeParams
├──────────────────────────────┤
│ 40 total   36 online          │   summary chips, colored
│ 20 printing  4 paused  6 done │   (printing/paused pulse)
├──────────────────────────────┤
│ ┌Printing─┐ Paused  Finished  │   segmented tabs (mobile)
│ └─20──────┘  4        6        │
├──────────────────────────────┤
│ ● AVIONIKA_1        P1S  1h05m │   one card per printer
│   ███████████░░░░░░░   73%     │   progress + percent
│   part_drone_arm.3mf          │   file (truncates)
│   250/250  ·  70/70  ·  Std   │   temps + speed (always shown)
├──────────────────────────────┤
│ ● AVIONIKA_5        X1C  0h08m │
│   ███████████████████  96%     │
│   bracket_left.3mf            │
│   220/220  ·  65/65  ·  Silent│
├──────────────────────────────┤
│ ⚠ AVIONIKA_3 (paused) X1C 55% │   paused tab: amber/red
│   █████████░░░░░░░░            │
│   ⚠ Filament on the spool may │   the HMS pause reason
│     be tangled or stuck        │
└──────────────────────────────┘
        [  ⟳ Refresh  ]              Telegram MainButton
```

Behaviour:
* Tabs switch Printing / Paused / Finished (better than stacked sections on a phone).
* Auto refresh every few seconds plus pull to refresh; MainButton also refreshes.
* Colors come from Telegram theme, so it looks native in the user's app.
* Tap a card opens a detail sheet later (temps history, controls): future phase.

## Build plan (phases, on this branch)

1. Mini App frontend: `/app` route, mobile HTML/CSS/JS, Telegram SDK, tabs, theme.
2. Auth: `initData` validation helper (HMAC SHA256, auth_date check) plus
   `allowed_users` gate; `POST /api/miniapp/metrics`.
3. Bot: set the Menu button / add an inline Open button to the tunnel URL.
4. Tunnel: document and script `cloudflared` setup on the farm PC (config + service).
5. Polish: haptics, empty/offline states, per printer detail sheet.

## Security notes

* Validate `initData` HMAC on every request; reject stale `auth_date`.
* Gate by `telegram.allowed_users` (same allow list the bot already uses).
* HTTPS is provided by the tunnel.
* The bot token (the HMAC key) already lives on the farm PC in config.json.

## Open decisions for you

1. Reachability: Cloudflare Tunnel (recommended) vs ngrok vs a cloud relay.
2. Entry point: Menu button, inline "Open dashboard" button, or both.
3. Scope of v1: read only dashboard first, or include controls (pause/resume) early.
