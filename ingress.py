"""A fixed-target TCP relay for publishing the isolated local dashboard.

No HTTP forwarding destinations are accepted from clients. The only upstream
is the recorder on the private Compose network. Preserve HTTP bodies, ranges,
streaming responses and Host headers without inspecting credentials.
"""
import asyncio

UPSTREAM = 'streamrec'
PORT = 8080
MAX_CONNECTIONS = 64


async def copy(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()


async def relay(reader, writer):
    upstream = None
    tasks = []
    try:
        upstream_reader, upstream = await asyncio.wait_for(asyncio.open_connection(UPSTREAM, PORT), 5)
        tasks = [asyncio.create_task(copy(reader, upstream)), asyncio.create_task(copy(upstream_reader, writer))]
        await asyncio.gather(*tasks)
    except (OSError, asyncio.TimeoutError):
        pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for connection in (writer, upstream):
            if connection is not None:
                connection.close()
                try:
                    await connection.wait_closed()
                except OSError:
                    pass


async def main():
    slots = asyncio.Semaphore(MAX_CONNECTIONS)

    async def handle(reader, writer):
        if slots.locked():
            writer.close()
            await writer.wait_closed()
            return
        async with slots:
            await relay(reader, writer)

    server = await asyncio.start_server(handle, '0.0.0.0', PORT, limit=65536)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
