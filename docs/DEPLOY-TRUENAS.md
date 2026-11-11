# Deploying on TrueNAS (25.10)

The stack is three containers: the app (`backend`), its database (`postgres`), and an HTTPS
front door (`caddy`). The real compose file and its secrets live on a dataset; the TrueNAS
custom app is a three-line stub that points at it.

There are two ways to install. **Option A** is a single YAML pasted into TrueNAS that gives
the app its own LAN address, in the same style as a macvlan Homebridge install. **Option B**
(the numbered sections below) keeps the compose file on a dataset and serves the app on a
port of the NAS's own address.

## Option A: one pasted YAML with its own LAN address

1. Create the folders and the settings file (System > Shell):

   ```sh
   mkdir -p /mnt/apps/trading/config /mnt/apps/trading/postgres /mnt/apps/trading/caddy
   chown 568:568 /mnt/apps/trading/config
   nano /mnt/apps/trading/.env      # paste deploy/.env.example and fill it in
   chmod 600 /mnt/apps/trading/.env
   ```

   In this option only these `.env` lines are used: `APP_PASSWORD`, `SECRET_KEY`,
   `POSTGRES_PASSWORD`, `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_CALLBACK_URL`.

2. Find the name of the existing macvlan network: `sudo docker network ls | grep macvlan`.
   Docker permits one macvlan network per parent interface and subnet, so this app joins the
   existing one. For a TrueNAS app named `homebridge` with a network `homebridge_net`, it is
   `ix-homebridge_homebridge_net`.

3. Open `deploy/truenas-app.yaml` and adjust the marked values: the network name, and the
   app's IP address, which appears three times (`192.168.4.102` as shipped). Pick an address
   that is free and outside the router's DHCP range.

4. Give the NAS access to the image (section 3 below), then Apps > Discover Apps > menu next
   to Custom App > Install via YAML, name `trading`, paste the file, Save.

5. Open `https://192.168.4.102` from another device and continue at section 5 below.

Limits of this option: the NAS itself cannot reach the app's address (normal for macvlan),
and the app depends on the Homebridge app's network, so if Homebridge is deleted this app
must be reinstalled with its own network definition.

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
periodic snapshot task is worth adding.

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
| `IMAGE_TAG` | `latest` once the work is merged to `main`; a branch name before that |

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
   on each device.
2. Sign in with `APP_PASSWORD`.
3. Under "Connect to Schwab", follow the four steps on the page. On Schwab's consent screen,
   tick only the account the app should trade.
4. The page should then show "Schwab connected", the days until the sign-in expires, and one
   row with that account's balances.

That is the phase 0 exit test.

## Updating

```sh
cd /mnt/tank/apps/trading/stack
# replace compose.yaml / Caddyfile if they changed, then in the TrueNAS UI:
# Apps > trading > Update, or stop and start the app to pull the newer image.
```

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
| `APP_URL` | The address you open the app at, e.g. `https://192.168.4.102`. Used as the link in notifications. |
| `PUSHOVER_USER_KEY`, `PUSHOVER_APP_TOKEN` | Turn on notifications. The user key is on the Pushover dashboard; the token comes from creating an application there. |
| `REMINDER_WEEKDAY`, `REMINDER_HOUR` | When the weekly Schwab sign-in reminder goes out, US Eastern. Default Sunday 18:00. |
| `WATCHLIST` | Underlyings whose IV is recorded daily even with no position, e.g. `SPY,QQQ,IWM`. |
| `DEMO_MODE` | `true` shows a simulated account. Leave `false` for real use. |

## One-tap Schwab sign-in (optional)

Instead of pasting the redirect address, Schwab can send the browser straight back to the
app. This needs a change to the Schwab app registration, which can put it back through
Schwab's approval for a few days, during which sign-in may not work. Do it at a quiet time.

1. In the Schwab developer portal, edit the app's callback URLs to
   `https://127.0.0.1:8182,https://192.168.4.102/schwab/callback` (your app's address).
   Keep the first one as a fallback.
2. Wait until the app shows "Ready For Use" again.
3. In `.env`, set `SCHWAB_CALLBACK_URL=https://192.168.4.102/schwab/callback`, then stop and
   start the app.
4. The Settings page now shows a single "Sign in with Schwab" button.

The browser will warn about the certificate on the way back unless Caddy's root
certificate is trusted on that device (section 5).
