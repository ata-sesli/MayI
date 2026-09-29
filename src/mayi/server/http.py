import asyncio
import hmac

from ..core.models import hold
from .protocol import MAX_BYTES, decode, encode, status


class Application:
    def __init__(self, evaluator, *, token=None, max_bytes=MAX_BYTES, timeout=15.0):
        self.evaluator = evaluator
        self.token = token
        self.max_bytes = max_bytes
        self.timeout = timeout

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        if self.token is not None:
            values = [
                value
                for name, value in scope.get("headers", [])
                if name.lower() == b"authorization"
            ]
            if len(values) != 1 or not hmac.compare_digest(
                values[0], ("Bearer " + self.token).encode()
            ):
                await self._reply(send, 401, hold("Authentication required").to_dict())
                return
        path, method = scope["path"], scope["method"]
        routes = {
            "/v1/health": "GET",
            "/v1/status": "GET",
            "/v1/decide": "POST",
            "/v1/events": "POST",
        }
        if path not in routes:
            await self._reply(send, 404, {"error": "Not found"})
            return
        if method != routes[path]:
            await self._reply(send, 405, {"error": "Method not allowed"})
            return
        if path not in {"/v1/decide", "/v1/events"}:
            await self._reply(send, 200, status(self.evaluator))
            return
        try:
            body = bytearray()
            async with asyncio.timeout(self.timeout):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    body.extend(event.get("body", b""))
                    if len(body) > self.max_bytes:
                        await self._reply(
                            send, 413, hold("Request too large").to_dict()
                        )
                        return
                    if not event.get("more_body", False):
                        break
            value = decode(body)
        except ValueError, UnicodeError, RecursionError:
            await self._reply(send, 400, hold("Malformed JSON").to_dict())
            return
        except TimeoutError:
            await self._reply(send, 408, hold("Request timeout").to_dict())
            return
        if path == "/v1/events":
            result = self.evaluator.record_event(value)
            await self._reply(send, 200 if result["stored"] else 400, result)
        elif isinstance(value, dict) and value.get("action") == "event":
            # The envelope also works over configured hook endpoints/Unix sockets.
            await self._reply(
                send, 200, self.evaluator.record_event(value.get("event"))
            )
        else:
            result = await self.evaluator.authorize(value)
            await self._reply(send, 200, result.to_dict())

    @staticmethod
    async def _reply(send, code, value):
        body = encode(value)
        await send(
            {
                "type": "http.response.start",
                "status": code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
