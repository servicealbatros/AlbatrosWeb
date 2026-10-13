"""Recover only an unresponsive AutoTotal tab in the dedicated CDP browser."""
import asyncio
import json
from urllib.request import urlopen
from urllib.parse import urlparse
import websockets

async def _call(ws, ident, method, params):
    await ws.send(json.dumps({'id':ident,'method':method,'params':params}))
    async with asyncio.timeout(4):
        while True:
            message=json.loads(await ws.recv())
            if message.get('id')==ident:
                if message.get('error'): raise RuntimeError(str(message['error']))
                return message.get('result',{})

async def _responsive(target):
    try:
        async with websockets.connect(target['webSocketDebuggerUrl'],open_timeout=3,close_timeout=1) as ws:
            await _call(ws,1,'Runtime.evaluate',{'expression':'1+1','returnByValue':True})
        return True
    except Exception:
        return False

async def ensure_autototal_responsive():
    targets=json.load(urlopen('http://127.0.0.1:9222/json/list',timeout=3))
    recovered=[]
    for target in targets:
        if target.get('type')!='page' or urlparse(target.get('url','')).hostname not in {'www3.eoriginal.ro','www.eoriginal.ro'}:
            continue
        if await _responsive(target) or await _responsive(target):
            continue
        version=json.load(urlopen('http://127.0.0.1:9222/json/version',timeout=3))
        async with websockets.connect(version['webSocketDebuggerUrl'],open_timeout=3,close_timeout=1) as ws:
            result=await _call(ws,1,'Target.closeTarget',{'targetId':target['id']})
            if not result.get('success'): raise RuntimeError('Fila AutoTotal blocată nu a putut fi refăcută.')
            await _call(ws,2,'Target.createTarget',{'url':'https://www3.eoriginal.ro/#/','background':True})
        recovered.append(target['id'])
    return recovered
