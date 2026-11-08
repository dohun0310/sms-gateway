"""Service entry point: poll the modem and serve the local API."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from . import config as config_mod
from .api import Api
from .gateway import Gateway
from .modem import ModemError, ModemManagerModem
from .store import Store

log = logging.getLogger("sms_gateway")


async def run(cfg: config_mod.Config) -> None:
    store = Store(cfg.database_path)
    gateway = Gateway(store, ModemManagerModem(), cfg.emergency)
    gateway.recover()
    api = Api(gateway, cfg.allowed_uids)
    server = await api.serve(cfg.socket_path)
    log.info("listening on %s; emergency forwarding %s", cfg.socket_path,
             "enabled" if cfg.emergency else "disabled")

    last_error = None
    async with server:
        while True:
            try:
                await api.poll_under_lock()
                if last_error:
                    log.info("modem available again")
                last_error = None
            except (ModemError, OSError) as exc:
                if str(exc) != last_error:  # log each distinct failure once
                    log.error("poll failed: %s", exc)
                last_error = str(exc)
            await asyncio.sleep(cfg.poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="ModemManager SMS gateway")
    parser.add_argument("--config", type=Path, default=Path("/etc/sms-gateway/config.toml"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(run(config_mod.load(args.config)))


if __name__ == "__main__":
    main()
