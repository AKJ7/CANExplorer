from aiohttp import web


async def handle(request):
    name = request.match_info.get('name', 'Anonymous')
    text = 'Hello, ' + name
    return web.Response(text=text)


async def ws_handle(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for msg in ws:
        if msg.type == web.WSMsgType.text:
            await ws.send_str(f'Hello: {msg.data}')
        elif msg.type == web.WSMsgType.binary:
            await ws.send_bytes(msg.data)
        elif msg.type == web.WSMsgType.close:
            break
    return ws


async def create_server():
    app = web.Application()
    app.add_routes(
        [web.get('/', handle), web.get('/echo', ws_handle), web.get('/{name}', handle)]
    )
    return app
