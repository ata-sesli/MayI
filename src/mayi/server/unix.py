import asyncio
import errno
import os
import stat
from pathlib import Path

from ..core.models import hold
from .protocol import MAX_BYTES, decode, dispatch, encode


class UnixServer:
    def __init__(self, evaluator, path, *, idle_timeout=15.0, max_bytes=MAX_BYTES):
        self.evaluator = evaluator
        self.path = Path(path).expanduser()
        self.idle_timeout = idle_timeout
        self.max_bytes = max_bytes
        self.server = None
        self.identity = None
        self.clients = set()

    async def start(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise OSError(
                "Socket directory must be owned by this user and not writable by others"
            )
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("Refusing to replace a non-socket or unowned path")
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(self.path), 1
                )
            except OSError as error:
                if error.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                    raise
                if self.path.exists() and self.path.lstat().st_ino == info.st_ino:
                    self.path.unlink()
            else:
                writer.close()
                await writer.wait_closed()
                raise OSError("MayI socket is already in use")
        self.server = await asyncio.start_unix_server(
            self._handle,
            path=self.path,
            limit=self.max_bytes + 1,
            start_serving=False,
            cleanup_socket=False,
        )
        self.path.chmod(0o600)
        info = self.path.stat()
        self.identity = (info.st_dev, info.st_ino)
        await self.server.start_serving()

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.readline(), self.idle_timeout)
                except ValueError:
                    writer.write(encode(hold("Request too large").to_dict()))
                    await writer.drain()
                    break
                if not data:
                    break
                if len(data) > self.max_bytes or not data.endswith(b"\n"):
                    result = hold("Request too large or incomplete").to_dict()
                else:
                    result = await dispatch(self.evaluator, data)
                writer.write(encode(result))
                await asyncio.wait_for(writer.drain(), self.idle_timeout)
        except TimeoutError, ConnectionError, OSError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError, OSError:
                pass
            self.clients.discard(task)

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for task in list(self.clients):
            task.cancel()
        await asyncio.gather(*self.clients, return_exceptions=True)
        if self.identity is not None and self.path.exists():
            info = self.path.lstat()
            if (info.st_dev, info.st_ino) == self.identity and stat.S_ISSOCK(
                info.st_mode
            ):
                self.path.unlink()
        self.identity = None


async def query(path, value, *, timeout=12.0):
    data = encode(value)
    if len(data) > MAX_BYTES:
        raise ValueError("Request too large")
    async with asyncio.timeout(timeout):
        reader, writer = await asyncio.open_unix_connection(
            str(Path(path).expanduser()), limit=MAX_BYTES
        )
        try:
            writer.write(data)
            await writer.drain()
            reply = await reader.readline()
            if not reply.endswith(b"\n") or len(reply) > MAX_BYTES:
                raise ValueError("Invalid server response")
            result = decode(reply)
            if not isinstance(result, dict):
                raise TypeError("Invalid server response")
            return result
        finally:
            writer.close()
            await writer.wait_closed()
