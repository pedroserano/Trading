# Deploying on TrueNAS (25.10)

The stack is three containers: the app (`backend`), its database (`postgres`), and an HTTPS
front door (`caddy`). The real compose file and its secrets live on a dataset; the TrueNAS
custom app is a three-line stub that points at it.

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
