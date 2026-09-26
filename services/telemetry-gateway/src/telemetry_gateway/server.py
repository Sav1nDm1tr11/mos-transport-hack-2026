"""Bounded asyncio TCP listener for NDTP frames."""

from __future__ import annotations

import asyncio
import inspect
import struct
from collections.abc import Awaitable, Callable
from weakref import WeakKeyDictionary

from .protocol import (
    MAX_DATA_SIZE,
    NPH_SIZE,
    NPL_SIZE,
    Frame,
    ProtocolError,
    decode_frame,
)

FrameCallback = Callable[[Frame], Awaitable[None]]
ErrorCallback = Callable[[str, int | None], Awaitable[None] | None]
_server_sessions: WeakKeyDictionary[asyncio.Server, dict[str, object]] = (
    WeakKeyDictionary()
)


async def close_server(server: asyncio.Server) -> None:
    """Close the listener and cancel its active client sessions."""
    server.close()
    state = _server_sessions.pop(server, None)
    if state is None:
        await server.wait_closed()
        return
    state["closing"] = True
    writers = tuple(state["writers"])
    tasks = tuple(state["tasks"])
    for writer in writers:
        writer.close()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if writers:
        await asyncio.gather(
            *(writer.wait_closed() for writer in writers), return_exceptions=True
        )
    await server.wait_closed()


async def start_server(
    callback: FrameCallback,
    host: str = "127.0.0.1",
    port: int = 9201,
    *,
    max_connections: int = 1000,
    read_timeout: float = 60,
    on_error: ErrorCallback | None = None,
) -> asyncio.Server:
    """Start the listener; malformed clients are reported and closed locally."""
    if max_connections < 1:
        raise ValueError("max_connections must be positive")
    if read_timeout <= 0:
        raise ValueError("read_timeout must be positive")

    state: dict[str, object] = {"closing": False, "tasks": set(), "writers": set()}
    tasks: set[asyncio.Task[None]] = state["tasks"]  # type: ignore[assignment]
    writers: set[asyncio.StreamWriter] = state["writers"]  # type: ignore[assignment]

    async def report(reason: str, unit_id: int | None) -> None:
        if on_error is None:
            return
        try:
            result = on_error(reason, unit_id)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Diagnostics must not take down a connection or the listener.
            pass

    async def read_frame(reader: asyncio.StreamReader) -> bytes:
        header = await asyncio.wait_for(reader.readexactly(NPL_SIZE), read_timeout)
        if struct.unpack_from("<H", header)[0] != 0x7E7E:
            raise ProtocolError("invalid NPL signature")
        size = struct.unpack_from("<H", header, 2)[0]
        if not NPH_SIZE <= size <= MAX_DATA_SIZE:
            raise ProtocolError("invalid NPL data size")
        remainder = await asyncio.wait_for(reader.readexactly(size), read_timeout)
        return header + remainder

    async def connection(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer_unit: int | None = None
        try:
            first = decode_frame(await read_frame(reader))
            if first.kind != "handshake":
                raise ProtocolError("realtime frame before handshake")
            peer_unit = first.unit_id
            await callback(first)
            while True:
                try:
                    raw = await read_frame(reader)
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        raise ProtocolError("truncated frame") from exc
                    return
                current = decode_frame(raw)
                if current.kind != "realtime":
                    raise ProtocolError("unexpected handshake after connection setup")
                if current.unit_id != peer_unit:
                    raise ProtocolError("connection identity mismatch")
                await callback(current)
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                await report("truncated frame", peer_unit)
        except TimeoutError:
            await report("frame read timeout", peer_unit)
        except ProtocolError as exc:
            await report(str(exc), peer_unit)
        except Exception as exc:
            await report(f"callback or connection failure: {exc}", peer_unit)
        finally:
            writer.close()
            writers.discard(writer)
            try:
                await writer.wait_closed()
            except (ConnectionError, asyncio.CancelledError):
                pass

    def client_connected(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if state["closing"] or len(tasks) >= max_connections:
            writer.close()
            return
        writers.add(writer)
        task = asyncio.create_task(connection(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    server = await asyncio.start_server(
        client_connected,
        host,
        port,
        limit=NPL_SIZE + MAX_DATA_SIZE,
    )
    _server_sessions[server] = state
    return server
