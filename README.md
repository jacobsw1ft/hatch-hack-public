# Hatch Hack — a tiny, self-hosted controller for the Hatch Restore

A ~38 KB web page that controls a **Hatch Restore 3** smart sunrise-alarm / sound
machine / bedside lamp — alarms, night-light, and looping nighttime sounds —
without the official app. It runs from a static website, talks to the same cloud
API and device that the Hatch app uses, and needs nothing running at alarm time.

Once deployed, it lives at your own static host (PIN-gated).

> **Unofficial & unaffiliated.** This project is not endorsed by Hatch. It talks
> to Hatch's private cloud/IoT API, reverse-engineered from the app's own
> traffic. It can break at any time if Hatch changes their backend. Use at your
> own risk. No warranty. See [Disclaimer](#disclaimer).

---

## Why build this? (vs. the official Hatch app)

The Hatch app is fine, but it's a heavy, account-locked mobile app for what is,
underneath, a handful of simple HTTP + MQTT calls. This project is the opposite:
a single static page you can open anywhere.

| | Official Hatch app | This project |
|---|---|---|
| **Install size** | **~500 MB** 😳 | **~38 KB** single HTML page (~13,000× smaller) |
| **Platform** | iOS / Android app | Any browser — phone, laptop, tablet |
| **Where it runs** | Your phone | Your own website (static host) + a tiny serverless backend |
| **Who can use it** | Whoever has the app + your login | Anyone you give the PIN to (e.g. a partner), no app install, no shared password |
| **Auth exposure** | Your Hatch password lives in the app | Password lives only in a server env var; the page only ever sees a 4-digit PIN |
| **Alarm control** | Full | Full — time, sunrise lead, days, color, sound, volume |
| **Night-light** | Full | One-tap warm-white bedside lamp (2700 K, 100%) |
| **Nighttime sounds** | Full | Independent play/stop of looping ambient/sleep sounds |
| **Startup / bloat** | Splash screens, upsells, account nags | Loads instantly, no tracking, no upsell |
| **Customizable** | No | It's ~600 lines of HTML/JS + ~400 lines of Python — change anything |

Half a gigabyte for an alarm clock is genuinely wild. The entire functional
surface we care about fits in a page smaller than a single app icon.

**What we give up:** premium/paid content (we only use the free sound/color
library, which is plenty), and the polish of a native app. Everything the device
can physically do, it can still do — we just drive it directly.

---

## What it does

- **Alarms** — create, edit, enable/disable, and delete. Full control of wake
  time, sunrise "lead" (how many minutes before wake the light ramps up),
  repeat days, sunrise color, alarm sound (with in-browser preview), and volume.
- **Night-light** — a one-tap warm-white (2700 K) bedside lamp at full brightness,
  toggled in real time.
- **Nighttime sounds** — pick any ambient/sleep sound and play it on a loop in the
  moment (rain, ocean, fans, white/brown/pink noise, campfire, crickets…),
  independent of the light. Live-swap the sound or volume while it's playing.
- **PIN-gated** — the public page is protected by a server-checked 4-digit PIN
  (numeric keypad on mobile). The Hatch password is never in the page.
- **Soothing UI** — a fixed night-sky theme (royal blue → sky blue, stars, a
  crescent moon), built to feel native on iPhone Safari and on a laptop.

---

## How it works (architecture)

```
Browser (static page on GitHub Pages)  ──PIN──▶  Serverless backend (Flask on Vercel)
        │                                              │
        │  reads the free sound/color catalog          │  Hatch REST API  (login, list/save alarms)
        ▼  directly from Hatch's public CMS            ▼  AWS IoT (MQTT over WebSocket) for real-time light/sound
   Contentful GraphQL                            data.hatchbaby.com  +  AWS IoT Device Shadow
```

Two moving parts:

1. **Frontend** (`hatch-app.html`) — a single static page. Handles the PIN gate,
   the UI, and reads the **free sound/color catalog directly from Hatch's public
   Contentful CMS** (a public read token that ships in the Hatch app itself, so
   it's not a secret). All device actions go through the backend.

2. **Backend** (`api/`) — a tiny **Flask app on Vercel** (Python serverless). It
   holds the Hatch credentials (as env vars) and does the two things a browser
   can't safely do:
   - **Alarms (persistent):** logs into the Hatch REST API and creates/edits/
     deletes routines. These live on Hatch's servers and fire even with
     everything off — exactly like the app.
   - **Real-time light & sound:** the device is an **AWS IoT thing**. To change
     the light or play a sound *right now*, the backend mints temporary AWS
     credentials (Cognito), **SigV4-presigns a WebSocket URL**, connects to AWS
     IoT over MQTT, and publishes a **device-shadow** update. Fire-and-forget.

The backend caches the Hatch auth token (6 h, in-memory on the warm instance) so
a burst of actions doesn't re-login every time and trip Hatch's rate limit.

### Key API facts we learned

- **Auth:** `POST https://data.hatchbaby.com/public/v1/login` → returns a token
  used as the `X-HatchBaby-Auth` header on every subsequent REST call.
- **Alarms are "routines":**
  - List: `GET service/app/routine/v3/fetch?macAddress=…&types=alarm`
  - Create/edit: `POST service/app/routine/v2/createOrEdit` (id `-1` = create)
  - Delete: send the routine back with `active: false` via
    `POST service/app/routine/v1/bulkCreateOrEdit` (or a minimal `editMultiple`
    delta)
  - After a write you must **confirm the data version**:
    `POST service/app/v2/dataVersion`
- **Real-time control:** AWS IoT device shadow at
  `$aws/things/<thingName>/shadow/update`, published over
  **MQTT-over-WebSocket** with a SigV4-presigned URL (service
  `iotdevicegateway`). The plain HTTPS shadow API is blocked (403) — MQTT is the
  way in.
- **Light and sound are independent facets** of the shadow's `current` object.
  Turning one on/off should touch only that facet — never send `playing:"none"`
  to turn the light off, or it stops the sound too.
- **Sounds loop natively:** publish the sound with `until:"indefinite"`,
  `duration:0` and the device plays it continuously until you stop it. No
  client-side looping needed.
- **Catalog:** the free sound/color library is in **Contentful** (space
  `hlsdh3zwyrtx`), grouped by `theme` (e.g. "Alarm Sounds", "Ambient & Sleep
  Sounds"). Each sound has a `.wav` URL used for both browser preview and the
  alarm payload.

### Device quirks worth knowing

- **No overlapping alarms on the same day** — the firmware rejects two alarms
  whose windows overlap (`DontOverlapSteps`).
- **Create is always enabled** — a freshly created alarm comes back enabled even
  if you asked for it off; toggle it off afterward.
- **`leadMinutes: 0`** means no sunrise ramp (alarm fires immediately at the set
  time). Beware the classic bug: `0 or 20` in Python evaluates to `20`.
- **Snooze rules aren't configurable** — snooze behavior is firmware-locked and
  appears nowhere in the API or CMS.

---

## What we learned from the mitmproxy (MITM) capture

The alarm-writing endpoints were the hard part. We first guessed at
`editMultiple` and hit validation errors (`HV000028`) and silent no-ops — the app
clearly did something different. Rather than keep guessing, we ran a
**man-in-the-middle proxy** ([mitmproxy](https://mitmproxy.org/)) and watched the
Hatch app create a real alarm.

**What the capture taught us:**

1. **The real create endpoint is `routine/v2/createOrEdit`** with `id: -1` and a
   specific two-step routine shape (a "Sunrise" light-ramp step + an "Alarm"
   sound step), *not* the `editMultiple` we'd been fighting.
2. **The exact payload structure** — color/sound objects with `id`,
   `contentfulId`, `i` (intensity), `v` (volume), `duration`, `until`, etc. —
   copied verbatim from a known-good request.
3. **Deletes are just `active: false`** on the full routine via
   `bulkCreateOrEdit`, followed by the `dataVersion` confirm.
4. **No certificate pinning** on these calls — the app's traffic was readable
   through the proxy, which is what made this possible.
5. **Hatch+ premium is not required** for alarms or the free sound/color library.

**Practical notes if you replicate the capture:**

- On iOS: install the mitmproxy CA cert *and* trust it under
  Settings → General → About → Certificate Trust Settings. Then set the phone's
  Wi-Fi HTTP proxy to your computer running `mitmdump`.
- Visit `http://mitm.it` (plain HTTP) to get the cert — `https://` won't serve it
  until the proxy is trusted.
- **Only capture the request you need** (the alarm create/edit body). The login
  request contains your password — do **not** log or share it. Our capture addon
  explicitly skipped the login body.

That one 90-second capture replaced days of guesswork. It's the single most
useful technique in this whole project.

---

## Replicate it yourself

You'll need: a Hatch Restore (this was built/tested on a **Restore 3**,
product code `restoreV5`), your Hatch email/password, and free accounts on
Vercel + GitHub.

### 1. Backend (Vercel)

1. Copy the `api/` folder, `pyproject.toml`, and `vercel.json` into a repo.
2. Deploy to Vercel (Python serverless). The entrypoint is set in
   `pyproject.toml`: `[tool.vercel] entrypoint = "api.index:app"`.
3. Set environment variables in the Vercel project:
   - `HATCH_EMAIL` — your Hatch login email
   - `HATCH_PASSWORD` — your Hatch password
   - `HATCH_PIN` — the 4-digit PIN that protects the page
   - *(optional)* `FRONTEND_ORIGIN` — your site's origin to lock CORS (defaults `*`)
4. Turn **off** Vercel's Deployment Protection for the production URL (or the
   page can't call it).

The backend auto-discovers your device (MAC / thing name) from your account, so
nothing device-specific is hardcoded.

### 2. Frontend (any static host)

1. Edit `hatch-app.html` and set `API_BASE` to your Vercel URL.
2. Host the file anywhere static — GitHub Pages, Netlify, S3, Cloudflare Pages.
   (e.g. `yourdomain.com/hatch-hack/` via GitHub Pages.)
3. Open it, enter your PIN, and go.

### 3. (If you're adapting to a different device)

Confirm your product code and endpoints with a mitmproxy capture as described
above. The `hatch-rest-api` Python library is an excellent protocol reference for
other Restore models.

### Repo layout

```
api/
  index.py        Flask entrypoint (routes: /api/verify, /api/alarms, /api/nightlight, /api/sound)
  _lib.py         all backend logic: auth, REST alarms, presigned-MQTT light/sound
hatch-app.html    the entire frontend (single page)
catalog.html      standalone catalog browser (preview all free sounds/colors)
pyproject.toml    deps + Vercel entrypoint
vercel.json       function config (maxDuration)
*.py              dev/exploration scripts (not deployed)
```

---

## Security notes

- **Credentials never touch the frontend or the repo.** `HATCH_EMAIL` /
  `HATCH_PASSWORD` live only in Vercel env vars. The page only ever sends a PIN.
- Keep a real `.env` **out of git** (it's git-ignored here).
- The PIN is checked **server-side** with a constant-time compare; a wrong PIN
  can't reach any device action.
- The Contentful token is Hatch's own public read token (it ships in the app), so
  it's fine in the frontend — but it only grants read access to the public
  catalog.
- If you fork this, **rotate/replace the PIN** and don't commit your Vercel URL
  if you'd rather keep it private (the PIN protects it either way).

---

## Disclaimer

This is a personal, educational project. It is **not affiliated with, endorsed
by, or supported by Hatch.** It uses Hatch's private cloud and IoT APIs, which
are undocumented and may change or break without notice. You are responsible for
complying with Hatch's Terms of Service and for anything you do with your own
account and device. Provided as-is, with no warranty.

---

*Built as an IoT learning project: browser → serverless → Hatch REST + AWS IoT
MQTT. From "how does this thing even talk" to a live, shippable controller.*
