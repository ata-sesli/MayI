import argparse
import asyncio
import json
import logging
import os
import signal
import sys

from .adapters.codex import run_hook
from .config import load_config
from .core.evaluator import Evaluator
from .core.models import AuthorizationRequest, hold
from .julia.engine import JuliaEngine
from .server.protocol import MAX_BYTES, decode
from .server.unix import UnixServer, query

logger = logging.getLogger("mayi")


def parser():
    root = argparse.ArgumentParser(
        prog="mayi", description="Authorize coding-agent operations"
    )
    root.add_argument("--config", help="TOML configuration path")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Start the resident authorization daemon")
    commands.add_parser("status", help="Query the running daemon")
    decide = commands.add_parser(
        "decide", help="Evaluate locally using the configured model and audit store"
    )
    source = decide.add_mutually_exclusive_group(required=True)
    source.add_argument("--command", dest="operation")
    source.add_argument(
        "--stdin", action="store_true", help="Read a normalized JSON request"
    )
    decide.add_argument("--cwd", default=os.getcwd())
    decide.add_argument("--reason")
    hook = commands.add_parser("hook", help="Run an agent permission hook")
    hook.add_argument("agent", choices=["codex"])
    logs = commands.add_parser("logs", help="Read audit records")
    logs.add_argument("--decision", choices=["approve", "hold", "deny"])
    logs.add_argument("--limit", type=int, default=100)
    return root


def read_request():
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Request too large")
    return decode(raw)


async def build_evaluator(config):
    from .storage.zova import AuditStore

    audit = AuditStore(config.storage_path, retain_input=config.retain_input)
    model = None
    if config.model:
        logger.info("julia_loading")
        try:
            model = await asyncio.to_thread(
                JuliaEngine.load, config.model, device=config.device
            )
        except Exception:  # noqa: BLE001 - ML runtime failures must leave HOLD available.
            logger.warning("julia_unavailable ambiguous_requests_will_hold")
    else:
        logger.warning("julia_not_configured ambiguous_requests_will_hold")
    return Evaluator(
        model,
        threshold=config.approval_threshold,
        timeout=config.request_timeout,
        audit=audit,
    )


async def close_evaluator(evaluator):
    if evaluator.model is not None:
        await evaluator.model.close()
    evaluator.audit.close()


async def serve(config):
    evaluator = await build_evaluator(config)
    unix = UnixServer(evaluator, config.unix_socket)
    http_server = None
    http_task = None
    stop_task = None
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    try:
        await unix.start()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stopping.set)
        if config.http_enabled:
            from granian.constants import Interfaces
            from granian.server.embed import Server

            from .server.http import Application

            http_server = Server(
                Application(evaluator, token=config.bearer_token),
                address=config.host,
                port=config.port,
                interface=Interfaces.ASGI,
                ssl_cert=config.ssl_cert,
                ssl_key=config.ssl_key,
                log_access=False,
            )
            http_task = asyncio.create_task(http_server.serve())
        logger.info("daemon_ready")
        stop_task = asyncio.create_task(stopping.wait())
        tasks = {stop_task}
        if http_task:
            tasks.add(http_task)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if http_task in done:
            await http_task
            raise RuntimeError("HTTP server stopped unexpectedly")
    finally:
        if stop_task:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        if http_server and http_task:
            http_server.stop()
            await asyncio.gather(http_task, return_exceptions=True)
        await unix.close()
        await close_evaluator(evaluator)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


async def execute(args, config):
    if args.command == "hook":
        try:
            result = await run_hook(
                read_request(), config.unix_socket, timeout=config.request_timeout + 2
            )
        except ValueError, UnicodeError, RecursionError, OSError:
            result = {}
        print(json.dumps(result))
    elif args.command == "serve":
        await serve(config)
    elif args.command == "status":
        result = await query(config.unix_socket, {"action": "status"})
        print(json.dumps(result))
    elif args.command == "logs":
        from .storage.zova import AuditStore

        store = AuditStore(config.storage_path)
        try:
            print(json.dumps(store.logs(decision=args.decision, limit=args.limit)))
        finally:
            store.close()
    elif args.command == "decide":
        try:
            request = (
                read_request()
                if args.stdin
                else AuthorizationRequest(
                    "manual",
                    "shell",
                    args.operation,
                    args.cwd,
                    args.reason,
                    {"command": args.operation},
                    {},
                )
            )
        except ValueError, UnicodeError, RecursionError:
            request = None
        evaluator = await build_evaluator(config)
        try:
            print(json.dumps((await evaluator.authorize(request)).to_dict()))
        finally:
            await close_evaluator(evaluator)


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr
    )
    try:
        config = load_config(args.config)
        asyncio.run(execute(args, config))
    except KeyboardInterrupt:
        return 130
    except Exception as error:  # noqa: BLE001 - final CLI failure boundary.
        if args.command == "hook":
            print("{}")
            return 0
        if args.command == "decide":
            print(json.dumps(hold("Configuration or infrastructure failure").to_dict()))
        # Exception class identifies failure without disclosing request contents.
        logger.error("command_failed error_type=%s", type(error).__name__)
        return 1
    return 0
