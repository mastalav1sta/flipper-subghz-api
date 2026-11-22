"""Entry point:

    python3 -m flipper_subghz_api [--env-file PATH] [--actions-file PATH] [--check-config]
"""

from __future__ import annotations

import argparse
import logging
import sys

from .actions import load_actions
from .config import DEFAULT_ENV_FILE, ConfigError, load_config
from .flipper import FlipperCli, SerialTransport
from .server import App, build_server


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="flipper_subghz_api")
    p.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    p.add_argument("--actions-file", default=None,
                   help="JSON/TOML allowlist (default: $FLIPPER_ACTIONS_FILE, then the env "
                        "file's FLIPPER_ACTIONS_FILE, then ~/.config/flipper-subghz-api/"
                        "actions.json)")
    p.add_argument("--check-config", action="store_true", help="validate config and exit")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(args.env_file, actions_file=args.actions_file)
        allowlist = load_actions(cfg.actions_file)
    except ConfigError as exc:
        print(f"flipper-subghz-api: config error: {exc}", file=sys.stderr)
        return 2
    if args.check_config:
        print(f"config ok: bind={cfg.bind} port={cfg.port} serial={cfg.serial_port} "
              f"actions={','.join(allowlist.names)}")
        return 0
    cli = FlipperCli(lambda: SerialTransport(cfg.serial_port), allowlist,
                     tx_timeout=cfg.tx_timeout)
    app = App(cli, allowlist, cfg.serial_port, min_interval=cfg.min_interval)
    srv = build_server(app, cfg.token, cfg.bind, cfg.port)
    logging.info("listening on %s:%d (LAN only, token required), %d action(s)",
                 cfg.bind, cfg.port, len(allowlist))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
