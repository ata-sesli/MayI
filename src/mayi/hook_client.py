"""Sequential hook transports. HTTP runs in a killable, deadline-bound worker."""

import asyncio
import errno
import hashlib
import http.client
import os
import ssl
import stat
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from .server.protocol import MAX_BYTES, decode, encode
from .server.unix import query
from .telemetry import milliseconds


class Unavailable(Exception):
    """Only an unavailable endpoint permits trying the next endpoint."""


def read_token(path):
    # Open without following symlinks; inspect the opened file, not a prior stat.
    fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("Token file must be private and owned by this user")
        token = source.read(4097).strip()
    if not token or len(token) > 4096 or any(c < 33 or c > 126 for c in token):
        raise ValueError("Invalid bearer token")
    return token.decode("ascii")


def http_worker():
    """One HTTP attempt: 0 = reply, 2 = unavailable, 1 = fail conservatively."""
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 8193)
        if len(raw) > MAX_BYTES + 8192:
            return 1
        task = decode(raw)
        endpoint = task["endpoint"]
        url = urlsplit(endpoint["url"])
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if endpoint["token_file"]:
            headers["Authorization"] = "Bearer " + read_token(endpoint["token_file"])
        connection_class = (
            http.client.HTTPSConnection
            if url.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_class(
            url.hostname, url.port, timeout=task["connect_timeout"]
        )
        try:
            started = time.monotonic()
            connection.connect()
            connect_ms = milliseconds(started)
            connection.sock.settimeout(endpoint["timeout"])
            connection.request(
                "POST", url.path or "/", body=encode(task["request"]), headers=headers
            )
            response = connection.getresponse()
            if response.status in {404, 502, 503, 504}:
                sys.stdout.buffer.write(encode({"reason": f"http_{response.status}"}))
                return 2
            # No redirects, environment proxies, or credential forwarding.
            if response.status != 200:
                return 1
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                return 1
            result = decode(body)
            if not valid_reply(task["request"], result):
                return 1
            result["_connect_ms"] = connect_ms
            sys.stdout.buffer.write(encode(result))
            return 0
        finally:
            connection.close()
    except ssl.SSLError:
        return 1
    except ConnectionError, TimeoutError:
        return 2
    except OSError as error:
        # Missing credentials/permission failures must not cause failover.
        if error.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
            return 2
        # DNS lookup failures use socket.gaierror, an OSError subtype.
        import socket

        return 2 if isinstance(error, socket.gaierror) else 1
    except Exception:  # noqa: BLE001 - no secrets or request bodies on stderr.
        return 1


async def http_query(endpoint, request, connect_timeout):
    # A thread cannot be cancelled during DNS resolution or slow HTTP reads.
    # A worker process lets the parent enforce the complete attempt deadline.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "mayi.hook_client",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        value = asdict(endpoint)
        value["token_file"] = str(endpoint.token_file) if endpoint.token_file else None
        data = encode(
            {"endpoint": value, "request": request, "connect_timeout": connect_timeout}
        )
        stdout, _ = await process.communicate(data)
        if process.returncode == 2:
            reason = (
                decode(stdout).get("reason") if stdout else "connection_unavailable"
            )
            raise Unavailable(reason)
        if process.returncode != 0:
            raise ValueError("HTTP endpoint failed")
        return decode(stdout)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()


def endpoint_fingerprint(endpoints):
    values = [(str(e.unix_socket), e.url, str(e.token_file)) for e in endpoints]
    return hashlib.sha256(encode(values)).hexdigest()


def preferred_index(path, fingerprint, count):
    if path is None:
        return 0
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                return 0
            value = decode(source.read(4097))
        index = value.get("index")
        if (
            value.get("endpoints") == fingerprint
            and type(index) is int
            and 0 <= index < count
        ):
            return index
    except OSError, ValueError, TypeError, AttributeError, RecursionError:
        pass
    return 0


def remember_endpoint(path, fingerprint, index):
    if path is None:
        return
    temporary = None
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.parent.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            return
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(encode({"endpoints": fingerprint, "index": index}))
        os.replace(temporary, path)
    except OSError:
        # This is routing preference, not authorization/audit persistence.
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def valid_reply(request, result):
    if not isinstance(result, dict):
        return False
    if request.get("action") in {"event", "user_prompt"}:
        return type(result.get("stored")) is bool
    return result.get("decision") in {"approve", "hold", "deny"}


async def query_endpoints(
    endpoints, request, *, timeout, connect_timeout, state_file=None, routing=None
):
    fingerprint = endpoint_fingerprint(endpoints)
    start = preferred_index(state_file, fingerprint, len(endpoints))
    async with asyncio.timeout(timeout):
        for offset in range(len(endpoints)):
            index = (start + offset) % len(endpoints)
            endpoint = endpoints[index]
            attempt = {"endpoint": index, "outcome": "cancelled", "connect_ms": None}
            started = time.monotonic()
            if routing is not None:
                routing["attempts"].append(attempt)
            try:
                async with asyncio.timeout(endpoint.timeout):
                    if endpoint.unix_socket is not None:
                        try:
                            result = await query(
                                endpoint.unix_socket, request, timeout=endpoint.timeout
                            )
                        except OSError as error:
                            if error.errno in {
                                errno.ENOENT,
                                errno.ECONNREFUSED,
                                errno.ECONNRESET,
                                errno.EPIPE,
                            }:
                                raise Unavailable("unix_unavailable") from error
                            raise
                    else:
                        result = await http_query(endpoint, request, connect_timeout)
                    if not valid_reply(request, result):
                        raise ValueError("Invalid endpoint response")
                    attempt["connect_ms"] = result.pop("_connect_ms", None)
                    attempt["outcome"] = "reply"
                    if routing is not None:
                        routing["selected_endpoint"] = index
                    # Only authorization replies update routing preference.
                    if request.get("action") not in {"event", "user_prompt"}:
                        remember_endpoint(state_file, fingerprint, index)
                    return result
            except Unavailable as error:
                attempt["outcome"] = "unavailable"
                attempt["reason"] = str(error)
            except TimeoutError:
                attempt["outcome"] = "timeout"
            except Exception:
                attempt["outcome"] = "error"
                raise
            finally:
                attempt["duration_ms"] = milliseconds(started)
    return (
        {"stored": False}
        if request.get("action") in {"event", "user_prompt"}
        else {"decision": "hold"}
    )


if __name__ == "__main__":
    raise SystemExit(http_worker())
