# Deploying on TrueNAS (25.10)

The stack is three containers: the app (`backend`), its database (`postgres`), and an HTTPS
front door (`caddy`). The real compose file and its secrets live on a dataset; the TrueNAS
custom app is a three-line stub that points at it.

There are two ways to install. **Option A** is a single YAML pasted into TrueNAS that serves
the app on port 8443 of the NAS's own address. **Option B** (the numbered sections below)
keeps the compose file on a dataset instead.

## Option A: one pasted YAML

1. Create a dataset for the app on a data pool (Datasets > select the pool > Add Dataset),
   for example `trading` on a pool named `thor`, so it is mounted at `/mnt/thor/trading`.
   It must be a dataset on a data pool, not a folder: a plain `mkdir` under `/mnt` where no
   pool is mounted writes to the boot drive, which TrueNAS does not snapshot and which a
   TrueNAS update replaces. Check it before going on:

   ```sh
   df -h /mnt/thor/trading    # the Filesystem column must read thor/trading, not boot-pool/...
   ```

   If your pool has another name, use its path below and in `deploy/truenas-app.yaml`
   instead of `/mnt/thor/trading`. Then create the folders and the settings file
   (System > Shell):

   ```sh
   mkdir -p /mnt/thor/trading/config /mnt/thor/trading/postgres /mnt/thor/trading/caddy
   chown 568:568 /mnt/thor/trading/config
   nano /mnt/thor/trading/.env      # paste deploy/.env.example and fill it in
   chmod 600 /mnt/thor/trading/.env
   ```

   In this option only these `.env` lines are used: `APP_PASSWORD`, `SECRET_KEY`,
   `POSTGRES_PASSWORD`, `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_CALLBACK_URL`.

2. Open `deploy/truenas-app.yaml` and adjust the marked values: the NAS's LAN address,
   which appears twice (`192.168.7.80` as shipped), and the dataset path if it is not
   `/mnt/thor/trading`. If port 8443 is taken on the NAS, change the left side of
   `'8443:443'`.

3. Give the NAS access to the image (section 3 below), then Apps > Discover Apps > menu next
   to Custom App > Install via YAML, name `trading`, paste the file, Save.

4. Open `https://192.168.7.80:8443` from another device and continue at section 5 below.

## Option B: compose file on a dataset

## 1. Datasets

Create a dataset for the app, for example `tank/apps/trading`, then these folders inside it
(Shell, or System > Shell in the TrueNAS UI):

```sh
cd /mnt/tank/apps/trading
mkdir -p stack data/config data/postgres data/caddy
chown 568:568 data/config
```

`568` is TrueNAS's built-in `apps` user, which the app runs as. Postgres and Caddy manage
ownership of their own folders. Enabling encryption on the dataset is recommended, and a
periodic snapshot task is worth adding (Data Protection > Periodic Snapshot Tasks).

## 2. Stack files

Copy `deploy/compose.yaml` and `deploy/Caddyfile` from this repository into
`/mnt/tank/apps/trading/stack/`, and copy `deploy/.env.example` there as `.env`. Then:

```sh
cd /mnt/tank/apps/trading/stack
chmod 600 .env
openssl rand -hex 32   # use for SECRET_KEY
openssl rand -hex 24   # use for POSTGRES_PASSWORD
```

Edit `.env`:

| Setting | Value |
|---|---|
| `DATA_ROOT` | `/mnt/tank/apps/trading/data` |
| `NAS_HOST` | The NAS's LAN IP or hostname, exactly as typed in the browser |
| `HTTPS_PORT` | `8443` unless that port is taken |
| `APP_PASSWORD` | A password of at least 12 characters for this app's sign-in page |
| `SECRET_KEY`, `POSTGRES_PASSWORD` | The two generated values |
| `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET` | From the app's details in the Schwab developer portal |
| `SCHWAB_CALLBACK_URL` | `https://127.0.0.1:8182`, matching the Schwab app exactly |
| `IMAGE_TAG` | `main`: the image built from the main branch |

The Schwab key and secret exist only in this file. They are never entered in the TrueNAS UI
and never committed.

## 3. Image access

GitHub Actions builds the image and publishes it to `ghcr.io/pedroserano/trading-backend`.
Packages start out private, so the NAS needs permission to pull it. Either:

- **Sign the NAS in to the registry.** On GitHub, create a classic personal access token with
  only the `read:packages` scope. In TrueNAS: Apps > Configuration > Sign-in to a Docker
  registry, with registry `ghcr.io`, your GitHub username, and the token as the password.
- **Or make the package public.** On GitHub: your profile > Packages > `trading-backend` >
  Package settings > Change visibility. The image contains the app's code and no secrets.

## 4. Install the custom app

Apps > Discover Apps > the menu next to Custom App > Install via YAML. Name it `trading` and
paste:

```yaml
include:
  - path: /mnt/tank/apps/trading/stack/compose.yaml
services: {}
```

Save. All three containers should reach Running within a minute.

## 5. First sign-in

1. Open `https://<NAS_HOST>:8443`. The browser warns about the certificate, because it comes
   from Caddy's own local authority. Proceed past the warning for now. To remove it
   permanently, install `data/caddy/caddy/pki/authorities/local/root.crt` as a trusted root
   on each device (in Option A: `caddy/caddy/pki/authorities/local/root.crt` in the app's
   dataset). On an iPhone: send the file to the phone (AirDrop or email), open it, install
   the profile under Settings > Profile Downloaded, then turn it on under Settings > General >
   About > Certificate Trust Settings. Until that last switch is on, an icon added to the
   Home Screen is a plain letter, because the phone cannot fetch the app's icon. With
   Tailscale (see "Tailscale" below) none of this is needed.
2. Sign in with `APP_PASSWORD`.
3. Under "Connect to Schwab", follow the four steps on the page. On Schwab's consent screen,
   tick only the account the app should trade.
4. The page should then show "Schwab connected", the days until the sign-in expires, and one
   row with that account's balances.

That is the phase 0 exit test.

## Updating

The backend image is tagged `main` and rebuilt on every push. The app's YAML sets
`pull_policy: always`, so stopping and starting the app fetches the newest build:
Apps > trading > Stop, then Start. (Before that line was added, a restart kept the image
already on the NAS.)

To check which build is running, compare the image ID before and after, in the TrueNAS shell:

```sh
docker image inspect ghcr.io/pedroserano/trading-backend:main --format '{{.Id}} {{.Created}}'
```

If `compose.yaml` or the `Caddyfile` changed, replace them in `/mnt/tank/apps/trading/stack`
first.

## Troubleshooting

- **App will not start, log says a setting is missing:** the named variable is empty in `.env`.
- **"Database unreachable" on the status page:** check the `postgres` container's log. A
  changed `POSTGRES_PASSWORD` after first start is the usual cause; the database keeps the
  original one.
- **"Stored Schwab token cannot be decrypted":** `SECRET_KEY` changed. Sign in to Schwab again.
- **Schwab rejects the sign-in:** the pasted address was older than about 30 seconds, was not
  the complete address, or `SCHWAB_CALLBACK_URL` differs from the app's registered callback.
- **Image pull fails:** see step 3.

## Optional settings (phase 1)

These go in the same `.env` file. After changing `.env`, stop and start the app in TrueNAS
so the container picks them up.

| Setting | Purpose |
|---|---|
| `APP_URL` | The address you open the app at, e.g. `https://192.168.7.80:8443`. Used as the link in notifications. |
| `PUSHOVER_USER_KEY`, `PUSHOVER_APP_TOKEN` | Turn on notifications. The user key is on the Pushover dashboard; the token comes from creating an application there. |
| `REMINDER_WEEKDAY`, `REMINDER_HOUR` | When the weekly Schwab sign-in reminder goes out, US Eastern. Default Sunday 18:00. |
| `WATCHLIST` | Starting watchlist, e.g. `SPY,QQQ,IWM`. Used only until the watchlist is edited on the Settings page, which also imports thinkorswim exports. |
| `DEMO_MODE` | `true` shows a simulated account. Leave `false` for real use. |
| `SIM_START_CASH` | Starting cash of the simulated account. Default 100000. Resetting the simulated account from the Activity page can set a different amount. |
| `SIM_COMMISSION` | Commission per contract for the simulated account. Default 0.45. |
| `LIVE_TRADING` | `true` lets the app send live orders to Schwab. Live orders also need Settings, Live trading, Schwab orders set to Manual only or Manual and automatic. Leave unset (off) to keep the app preview-only for the Schwab account. |
| `LIVE_AUTO` | `true` lets automation (automanage and the scanner) place live orders by itself at Auto levels. It needs `LIVE_TRADING=true` and Settings, Live trading, Schwab orders set to Manual and automatic, as well. Leave unset (off) and live automation only proposes; every live order waits for your tap. |
| `FINNHUB_API_KEY` | Optional. A free key from finnhub.io lets the app look up earnings dates (date, and before the open or after the close) and the industry (for the Portfolio screen's sectors) for single stocks. Without it, only the dates you enter under Settings, Earnings dates are known, and the scanner skips stocks with no date. |
| `MAX_CONTRACTS_PER_LEG` | Hard limit on contracts per leg in any order, simulated or live. Default 10. |

## Tailscale (optional): a trusted certificate, and access away from home

If the NAS runs the TrueNAS Tailscale app, Tailscale can serve the app at the NAS's Tailscale
name with a real certificate: no certificate warning, no root certificate to install on each
device, and the Home Screen icon works on an iPhone. The address at home keeps working.

1. In the Tailscale admin console, DNS page: turn on MagicDNS and click **Enable HTTPS**. The
   NAS's full name is on the Machines page, e.g. `truenas-scale.rainbow-chickadee.ts.net`.
2. In the app's YAML (Apps > trading > Edit), add that name to the Caddy site line, then Save:

   ```
         https://192.168.7.80, https://truenas-scale.rainbow-chickadee.ts.net {
   ```

   Without it Caddy answers that name with an empty page.
3. In System > Shell, point Tailscale at the app. Use the NAS's IP address, not `localhost`:
   Caddy's certificate is for the IP, and it refuses a connection made to `localhost`.

   ```sh
   sudo docker ps --format '{{.Names}}' | grep -i tailscale    # e.g. ix-tailscale-tailscale-1
   sudo docker exec ix-tailscale-tailscale-1 tailscale serve --bg https+insecure://192.168.7.80:8443
   ```

   `https+insecure` only skips checking Caddy's own certificate on that hop inside the NAS.
   The setting is kept in the Tailscale app's data and survives restarts. To undo it:
   `tailscale serve --https=443 off`.
4. Check it from the shell; it should print `200` and a size in the thousands
   (`200 0` means step 2 has not taken effect: stop and start the app):

   ```sh
   curl -sk -o /dev/null -w '%{http_code} %{size_download}\n' \
     -H 'Host: truenas-scale.rainbow-chickadee.ts.net' https://192.168.7.80:8443/login
   ```

5. Open `https://truenas-scale.rainbow-chickadee.ts.net` on a device with Tailscale on. To
   have notification links use it, set `APP_URL` to it in `.env` and restart the app.
7. One-tap Schwab sign-in can return to this address too, from anywhere Tailscale is on:
   see the next section.

## One-tap Schwab sign-in (optional)

Instead of pasting the redirect address, Schwab can send the browser straight back to the
app. This needs a change to the Schwab app registration, which can put it back through
Schwab's approval for a few days, during which sign-in may not work. Do it at a quiet time.

1. In the Schwab developer portal, edit the app's callback URLs to
   `https://127.0.0.1:8182,https://192.168.7.80:8443/schwab/callback` (your app's address).
   Keep the first one as a fallback.
2. Wait until the app shows "Ready For Use" again.
3. In `.env`, set `SCHWAB_CALLBACK_URL=https://192.168.7.80:8443/schwab/callback`, then stop and
   start the app.
4. The Settings page now shows a single "Sign in with Schwab" button.

The browser will warn about the certificate on the way back unless Caddy's root
certificate is trusted on that device (section 5).

### Through Tailscale (works away from home, no certificate warning)

With Tailscale set up (above), use the ts.net address instead. Replace
`truenas-scale.rainbow-chickadee.ts.net` with yours.

1. In the Schwab developer portal, set the callback URLs to all three, separated by commas:
   `https://127.0.0.1:8182,https://192.168.7.80:8443/schwab/callback,https://truenas-scale.rainbow-chickadee.ts.net/schwab/callback`.
   Schwab accepts several; the app uses the one in `.env`. This edit can also put the app
   back through Schwab's approval for a few days.
2. Wait until the app shows "Ready For Use" again.
3. In `.env`, set both, then stop and start the app:
   ```
   SCHWAB_CALLBACK_URL=https://truenas-scale.rainbow-chickadee.ts.net/schwab/callback
   APP_URL=https://truenas-scale.rainbow-chickadee.ts.net
   ```
   `APP_URL` makes the sign-in reminders open the same address.
4. Start the sign-in from the ts.net address, not 192.168.7.80: the page Schwab returns to
   needs your app login for the same address. Tailscale must be on on that device.

## Trading history from the spreadsheet

The "Options P/L" spreadsheet was imported once into the app's database (the `sheet_rows`
table, in the Postgres data folder). Those rows stay there and feed the Dashboard and
Activity; trades since then come from Schwab and the app itself, so Settings no
longer offers an upload. Back up the Postgres folder to keep them.
