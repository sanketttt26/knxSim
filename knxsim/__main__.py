"""knxsim command line: python -m knxsim [-n 3] [-c installation.json]"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .host import Host
from .protocol import KNX_PORT
from .web import WebUI


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="knxsim", description="Multi-instance KNXnet/IP installation simulator")
    ap.add_argument("-n", "--instances", type=int, default=1,
                    help="number of instances with the default installation (default 1)")
    ap.add_argument("-c", "--config", help="JSON file with an 'instances' list (overrides -n)")
    ap.add_argument("--ip", help="IP address to advertise and to join multicast on (default: automatic)")
    ap.add_argument("--base-port", type=int, default=KNX_PORT + 1,
                    help=f"first UDP port for instances (default {KNX_PORT + 1}); {KNX_PORT} stays for discovery")
    ap.add_argument("--web-port", type=int, default=8080, help="browser UI port (default 8080)")
    ap.add_argument("--web-host", default="127.0.0.1", help="browser UI bind address (default 127.0.0.1)")
    ap.add_argument("--no-web", action="store_true", help="do not start the browser UI")
    ap.add_argument("--no-discovery", action="store_true", help=f"do not listen for searches on UDP {KNX_PORT}")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


async def run(args) -> None:
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            configs = json.load(fh)["instances"]
    else:
        configs = [{} for _ in range(args.instances)]
    host = Host(ip=args.ip, base_port=args.base_port)
    await host.start(discovery=not args.no_discovery)
    web = None
    try:
        for cfg in configs:
            await host.add_instance(cfg)
        print(f"KNXSim running - {len(host.instances)} instance(s) on {host.advertised_ip()}:")
        for inst in host.instances.values():
            print(f"  [{inst.id}] {inst.name:<24} UDP {inst.port}  interface {inst.snapshot()['ia']}")
        print(f"  discovery on UDP {KNX_PORT}: {'on' if host.discovery_active else 'OFF'}")
        if not args.no_web:
            web = WebUI(host, args.web_host, args.web_port)
            await web.start()
            print(f"  UI: http://{'localhost' if args.web_host == '127.0.0.1' else args.web_host}:{web.port}")
        print("Press Ctrl+C to stop.")
        await asyncio.Event().wait()
    finally:
        if web:
            web.close()
        host.stop()
        await asyncio.sleep(0.1)  # let DISCONNECT_REQUESTs leave the socket


def main(argv=None) -> None:
    args = parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # show the banner even when output is redirected
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RuntimeError) as exc:
        sys.exit(f"knxsim: {exc}")


if __name__ == "__main__":
    main()
