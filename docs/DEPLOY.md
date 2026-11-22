# Deployment

## Preconditions

* The Flipper is on its desktop screen (not inside the Sub-GHz app), unlocked, and
  connected over USB.
* Nothing else uses its serial port: no qFlipper, no `screen`/`minicom`, no Home
  Assistant serial integration pointed at it, and ModemManager removed or told to ignore
  the Flipper (see the README's security section). The API opens the port exclusively per
  request and answers `409 serial_port_busy` otherwise.
* The service user is in the `dialout` group (or whatever group owns `/dev/ttyACM*`).
* Every `.sub` file named in your actions file exists on the Flipper under
  `/ext/subghz/`, with a name that matches `[A-Za-z0-9_]+\.sub`.

## Install

```sh
git clone <this repo> ~/flipper-subghz-api
cd ~/flipper-subghz-api
install -d -m 700 ~/.config/flipper-subghz-api
install -m 600 config.env.example   ~/.config/flipper-subghz-api/config.env
install -m 644 actions.example.json ~/.config/flipper-subghz-api/actions.json
$EDITOR ~/.config/flipper-subghz-api/config.env ~/.config/flipper-subghz-api/actions.json
python3 -m flipper_subghz_api --check-config
```

`--check-config` prints `config ok: bind=… port=… serial=… actions=…` (never the token)
or exits with status 2 and the reason.

To find the stable serial path: `ls -l /dev/serial/by-id/` (it looks like
`usb-Flipper_Devices_Inc._Flipper_<name>_<name>-if00`).

## Run as a service

```sh
sed -e "s|<user>|$USER|g" -e "s|<install-dir>|$HOME/flipper-subghz-api|g" \
    deploy/flipper-subghz-api.service.example > flipper-subghz-api.service
sudo install -m 644 flipper-subghz-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flipper-subghz-api.service
journalctl -u flipper-subghz-api -f
```

The log has request lines, action names and outcomes only: no tokens, no signal data.

Recommended: a host firewall rule that allows the API port only from your Home Assistant
host. Never port-forward it on your router.

## First test

Do this while you can see the device you are controlling.

```sh
TOKEN=...   # from config.env, in your shell only
curl -s -H "Authorization: Bearer $TOKEN" http://192.0.2.10:8787/health
curl -s -X POST -H "Authorization: Bearer $TOKEN" http://192.0.2.10:8787/api/send/Light_on
```

1. `/health` should show `"device_present": true, "device_rw": true`.
2. Send one action, watch the device, read the response body. Wait at least
   `FLIPPER_MIN_INTERVAL` (3 s) between sends, or you get 429.
3. If a key-file action answers `sent` but the receiver doesn't react, try a higher
   `repeat` (more frames per transmission). For RAW captures, re-record the signal first
   before raising `repeat`.
4. If you get `unknown`, check the log and the device before sending again.

## Upgrading from `/api/toggle/`

`/api/toggle/<action>` still works and behaves exactly like `/api/send/<action>`, but it
is deprecated (responses carry `Deprecation: true`). Point your `rest_command` URLs at
`/api/send/<action>`.

## Rollback

```sh
sudo systemctl disable --now flipper-subghz-api.service
```
