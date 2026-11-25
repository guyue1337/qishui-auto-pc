# -*- coding: utf-8 -*-
"""Long-running iOS 17-26.x WebDriverAgent runner.

This process owns the RSD tunnel and the XCUITest session.  It is kept alive
while WDA is needed; terminating the process closes the tunnel and test runner.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--udid", required=True)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--log", default="")
    return parser.parse_args()


async def _run(udid: str, bundle_id: str) -> None:
    from pymobiledevice3.remote.rsd_tunnel import PreferredRsdTunnel
    from pymobiledevice3.services.dvt.testmanaged.xcuitest import TestConfig, XCUITestService

    logging.info("正在建立 iOS 17+ RSD 通道")
    async with PreferredRsdTunnel(serial=udid) as rsd:
        logging.info("RSD 通道已建立，准备启动 %s", bundle_id)
        config = await TestConfig.create_for(rsd, runner_bundle_id=bundle_id)
        logging.info("已找到 WDA 测试运行器，正在启动")
        service = XCUITestService(rsd)
        await service.run(config)


def main() -> int:
    args = _arguments()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if args.log:
        path = Path(args.log)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    try:
        asyncio.run(_run(args.udid, args.bundle_id))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logging.exception("WDA 运行器退出：%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
