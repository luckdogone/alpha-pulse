import argparse
import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path

from .binance import Binance
from .config import Settings
from .models import INTERVAL_MS, INTERVALS, Prediction, normalize_symbol
from .service import analyze
from .transport import DataError, JsonHTTP


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Alpha Pulse — Binance USD-M 行情分析 Agent")
    root.add_argument("--env-file", default=".env", help="dotenv 文件；环境变量优先")
    root.add_argument("--verbose", action="store_true")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("analyze", "watch", "stream", "snapshot", "klines"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--symbol", default="BTCUSDT")
        cmd.add_argument(
            "--interval",
            choices=INTERVALS if name in {"stream", "klines"} else list(INTERVAL_MS),
            default="1m",
        )
        if name in {"analyze", "watch"}:
            cmd.add_argument(
                "--engine",
                choices=["deepseek", "claude", "rules"],
                default=None,
                help="默认读取 .env 中的 MODEL_PROVIDER",
            )
        if name in {"watch", "stream"}:
            cmd.add_argument("--count", type=int, default=0, help="输出数量；0 表示持续运行")
        if name in {"analyze", "snapshot", "klines"}:
            cmd.add_argument("--output", type=Path)
        if name == "klines":
            cmd.add_argument("--start-ms", type=int)
            cmd.add_argument("--end-ms", type=int)
            cmd.add_argument("--limit", type=int, default=500)
    demo = commands.add_parser("demo", help="合成行情，离线验证 JSON 合约；不调用模型")
    demo.add_argument("--scenario", choices=["long", "short", "ranging"], default="long")
    demo.add_argument("--output", type=Path)
    commands.add_parser("schema", help="输出预测结果 JSON Schema")
    commands.add_parser("doctor", help="检查配置、代理 REST 和 WSS，不调用模型")
    return root


def emit(value, output: Path | None = None, *, compact: bool = False):
    data = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    text = json.dumps(data, ensure_ascii=False, indent=None if compact else 2, allow_nan=False)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    print(text, flush=True)


async def watch(settings: Settings, binance: Binance, args):
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)

    async def produce():
        try:
            async for event in binance.stream(args.symbol, args.interval):
                if event["candle"]["closed"]:
                    if queue.full():
                        queue.get_nowait()
                    await queue.put(event)
        except Exception as exc:
            if queue.full():
                queue.get_nowait()
            await queue.put(exc)

    task = asyncio.create_task(produce())
    try:
        count = 0
        while args.count == 0 or count < args.count:
            event = await queue.get()
            if isinstance(event, Exception):
                raise event
            # Only one analysis at a time; a bounded queue coalesces closes during slow model calls.
            emit(await analyze(settings, args.symbol, args.interval, args.engine), compact=True)
            count += 1
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def run(args) -> int:
    settings = Settings(_env_file=args.env_file)
    if hasattr(args, "symbol"):
        args.symbol = normalize_symbol(args.symbol)
    if hasattr(args, "count") and args.count < 0:
        raise ValueError("count must be non-negative")
    if args.command == "schema":
        emit(Prediction.model_json_schema())
        return 0
    if args.command in {"analyze", "demo"}:
        prediction = (
            await analyze(settings, engine="demo", scenario=args.scenario)
            if args.command == "demo"
            else await analyze(settings, args.symbol, args.interval, args.engine)
        )
        emit(prediction, args.output)
        return (
            2
            if prediction.engine == "guard" or prediction.reason_code == "insufficient_data"
            else 0
        )
    http = JsonHTTP(settings.proxy(binance=True), settings.request_timeout_seconds)
    binance = Binance(settings, http)
    try:
        if args.command == "stream":
            count = 0
            async for event in binance.stream(args.symbol, args.interval):
                emit(event, compact=True)
                count += 1
                if args.count and count >= args.count:
                    break
        elif args.command == "watch":
            await watch(settings, binance, args)
        elif args.command == "klines":
            emit(
                await binance.klines(
                    args.symbol, args.interval, args.start_ms, args.end_ms, args.limit
                ),
                args.output,
            )
        elif args.command == "snapshot":
            from .evidence import EvidenceStore
            from .market import MarketData

            store = EvidenceStore()
            snapshot = await MarketData(settings, binance, store).snapshot(
                args.symbol, args.interval
            )
            emit({"snapshot": snapshot.model_dump(), "evidence": store.ledger()}, args.output)
            return 2 if snapshot.problems else 0
        elif args.command == "doctor":
            report = {
                "proxy_configured": bool(settings.proxy(binance=True)),
                "model_provider": settings.model_provider,
                "model": settings.model_name(),
                "model_credentials_in_settings": bool(settings.deepseek_api_key)
                if settings.model_provider == "deepseek"
                else bool(settings.anthropic_api_key or settings.anthropic_auth_token),
                "model_api_checked": False,
                "coinglass_configured": bool(settings.coinglass_api_key),
            }
            try:
                report["binance_server_time"] = await binance.get("/fapi/v1/time")
                report["rest"] = "ok"
            except DataError as exc:
                report["rest"] = str(exc)
            stream = binance.stream("BTCUSDT", "1m")
            try:
                async with asyncio.timeout(20):
                    event = await anext(stream)
                    report["websocket"] = "ok"
                    report["websocket_event_time"] = event["event_time"]
            except (TimeoutError, DataError):
                report["websocket"] = "unavailable; check proxy and endpoint"
            finally:
                await stream.aclose()
            emit(report)
            return 0 if report["rest"] == report["websocket"] == "ok" else 2
    finally:
        await http.close()
    return 0


def main():
    args = parser().parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(message)s",
    )
    # Never enable HTTP/SDK debug logging: it can expose request data or authentication details.
    for name in ("httpx", "httpcore", "anthropic", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        code = 130
    except (DataError, ValueError, OSError) as exc:
        message = (
            str(exc)
            if isinstance(exc, DataError)
            else f"invalid configuration/input ({type(exc).__name__})"
        )
        emit({"status": "error", "error": message})
        code = 2
    sys.exit(code)
