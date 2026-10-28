"""Opt-in, synthetic loopback probes. Never connects to suppliers or live CDP.

Measures the complete owned process tree (Windows RSS/private commit or Linux
RSS), and cgroup memory when available. The optional Chromium baseline is a
fresh fixture, not a capacity test for a fully authenticated robot deployment.
"""
import argparse
import asyncio
from contextlib import closing
import ctypes
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_resources import memory_sample
from conversation_log_index import ConversationLog
from playwright_lifecycle import CdpDriver
from provider_sessions import session_response_metadata


def process_tree():
    if os.name == 'nt':
        from ctypes import wintypes as w
        class Entry(ctypes.Structure):
            _fields_ = [('size',w.DWORD),('usage',w.DWORD),('pid',w.DWORD),('heap',ctypes.c_size_t),
                        ('module',w.DWORD),('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),
                        ('flags',w.DWORD),('exe',w.WCHAR*260)]
        class Memory(ctypes.Structure):
            _fields_ = [('size',w.DWORD),('faults',w.DWORD)]+[(name,ctypes.c_size_t) for name in (
                'peak_rss','rss','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile','private_commit')]
        kernel, psapi = ctypes.WinDLL('kernel32',use_last_error=True), ctypes.WinDLL('psapi',use_last_error=True)
        kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
        kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD,w.DWORD]
        kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [w.HANDLE,ctypes.POINTER(Entry)]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.OpenProcess.argtypes = [w.DWORD,w.BOOL,w.DWORD]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE,ctypes.POINTER(Memory),w.DWORD]
        handle = kernel.CreateToolhelp32Snapshot(2,0)
        entries = {}
        try:
            item = Entry();item.size = ctypes.sizeof(item)
            ok = kernel.Process32FirstW(handle,ctypes.byref(item))
            while ok:
                entries[item.pid] = {'pid':item.pid,'parent':item.parent,'name':item.exe}
                ok = kernel.Process32NextW(handle,ctypes.byref(item))
        finally:
            kernel.CloseHandle(handle)
        selected = {os.getpid()}
        while True:
            following = selected | {pid for pid,item in entries.items() if item['parent'] in selected}
            if following == selected:break
            selected = following
        rows = []
        for pid in sorted(selected):
            item = entries.get(pid,{'pid':pid})
            handle = kernel.OpenProcess(0x1000|0x10,False,pid)
            if handle:
                try:
                    mem = Memory();mem.size = ctypes.sizeof(mem)
                    if psapi.GetProcessMemoryInfo(handle,ctypes.byref(mem),mem.size):
                        item.update(rss_bytes=mem.rss,private_commit_bytes=mem.private_commit)
                finally:
                    kernel.CloseHandle(handle)
            rows.append(item)
    else:
        entries = {}
        for folder in Path('/proc').glob('[0-9]*'):
            try:
                stat = (folder/'stat').read_text();parts = stat[stat.rfind(')')+2:].split()
                rss = next(line.split()[1] for line in (folder/'status').read_text().splitlines() if line.startswith('VmRSS:'))
                entries[int(folder.name)] = dict(pid=int(folder.name),parent=int(parts[1]),
                                                name=stat[stat.find('(')+1:stat.rfind(')')],rss_bytes=int(rss)*1024)
            except (OSError,ValueError,StopIteration):
                continue
        selected = {os.getpid()}
        while True:
            following = selected | {pid for pid,item in entries.items() if item['parent'] in selected}
            if following == selected:break
            selected = following
        rows = [entries[pid] for pid in sorted(selected) if pid in entries]
    return {'process_count':len(rows),'rss_sum_bytes':sum(row.get('rss_bytes',0) for row in rows),
            'private_commit_sum_bytes':sum(row.get('private_commit_bytes',0) for row in rows) if os.name=='nt' else None,
            'processes':rows,'cgroup':memory_sample()}


class Handler(BaseHTTPRequestHandler):
    body = b'x'*(1024*1024)
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length',str(len(self.body)))
        self.send_header('Set-Cookie','fixture_session=preserved; Path=/')
        self.end_headers()
        self.wfile.write(self.body)
    def log_message(self,*_):pass


async def response_probe(url, count, dispose):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        request = await playwright.request.new_context()
        try:
            for _ in range(10):
                await session_response_metadata(request,url,timeout=5000)
            samples = [{'request':0,**process_tree()}]
            for i in range(1,count+1):
                if dispose:
                    await session_response_metadata(request,url,timeout=5000)
                else:
                    # Deliberate control: reproduces the former body retention.
                    response = await request.get(url,timeout=5000)
                    assert response.status == 200
                if i % max(1,count//4)==0 or i==count:
                    samples.append({'request':i,**process_tree()})
            return samples
        finally:
            await request.dispose()


async def failure_probe(count):
    with closing(socket.socket()) as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    owner=CdpDriver(f'http://127.0.0.1:{port}',timeout=250)
    samples=[process_tree()]
    try:
        for _ in range(count):
            try:await owner.context()
            except Exception:pass
            samples.append(process_tree())
        assert all(sample['process_count']==samples[0]['process_count'] for sample in samples), 'Owned driver process leaked'
        return samples
    finally:
        await owner.shutdown()


async def browser_probe(fixture_url):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        executable = playwright.chromium.executable_path
    if not Path(executable).exists():
        return {'unavailable':'Chromium not installed'}
    import httpx
    with tempfile.TemporaryDirectory() as root, closing(socket.socket()) as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        process=subprocess.Popen([executable,'--headless','--no-sandbox','--no-first-run',
            '--remote-debugging-address=127.0.0.1',f'--remote-debugging-port={port}',f'--user-data-dir={root}','about:blank'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        owner=CdpDriver(f'http://127.0.0.1:{port}',timeout=5000)
        try:
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    try:
                        (await client.get(f'http://127.0.0.1:{port}/json/version')).raise_for_status();break
                    except httpx.HTTPError:await asyncio.sleep(.1)
            context=await owner.context()
            await context.add_cookies([{'name':'fixture','value':'preserved','domain':'127.0.0.1','path':'/'}])
            await session_response_metadata(context.request,fixture_url,timeout=5000)
            assert any(cookie['name']=='fixture_session' for cookie in await context.cookies())
            before=process_tree()
            reconnects=[]
            for _ in range(10):
                await owner.shutdown()
                assert process.poll() is None, 'Driver shutdown closed external Chromium'
                owner=CdpDriver(f'http://127.0.0.1:{port}',timeout=5000)
                context=await owner.context()
                cookies={cookie['name'] for cookie in await context.cookies()}
                assert {'fixture','fixture_session'} <= cookies
                sample=process_tree()
                assert sum(row.get('name','').lower() in {'node','node.exe'} for row in sample['processes']) == 1
                reconnects.append(sample)
            return {'baseline':before,'after_reconnect':process_tree(),'external_browser_survived':True,
                    'fixture_cookie_preserved':True,'request_cookie_preserved_after_disposal':True,
                    'reconnect_samples':reconnects}
        finally:
            # This test alone owns this fresh browser/profile; production never closes it.
            if owner.browser is not None and owner.browser.is_connected():
                await owner.browser.close()
            await owner.shutdown()
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()


def history_probe(records):
    with tempfile.TemporaryDirectory() as root:
        path=Path(root)/'history.jsonl'
        with path.open('w',encoding='utf-8') as output:
            for i in range(records):
                output.write(json.dumps({'phone':str(i%1000),'case_id':str(i%1000),'direction':'client',
                                         'text':'x'*500,'timestamp':'2026-10-28T08:00:00'})+'\n')
        before=process_tree();start=time.monotonic();store=ConversationLog(path);store.sync()
        after=process_tree();rows,_=store.page({'phone':'1','session_id':'1'},limit=100)
        return {'records':records,'jsonl_bytes':path.stat().st_size,'seconds':round(time.monotonic()-start,3),
                'returned_page_rows':len(rows),'before':before,'after':after}


async def main(args):
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_address[1]}/fixture'
    try:
        result={'platform':sys.platform,'python':sys.version,'timestamp':datetime.now(timezone.utc).isoformat(),
                'body_bytes':len(Handler.body),'requests_per_case':args.requests,
                'unsafe_control':await response_probe(url,args.requests,False),
                'disposed_responses':await response_probe(url,args.requests,True),
                'repeated_cdp_failures':await failure_probe(args.failures),
                'history':history_probe(args.records)}
        if args.chromium:result['chromium_fixture']=await browser_probe(url)
        if args.requests >= 100:
            metric='private_commit_sum_bytes' if os.name=='nt' else 'rss_sum_bytes'
            growth=lambda samples:max(item[metric] for item in samples)-samples[0][metric]
            assert growth(result['disposed_responses']) < growth(result['unsafe_control'])*.6, 'Response retention regressed'
        Path(args.output).parent.mkdir(parents=True,exist_ok=True)
        Path(args.output).write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps({'report':str(Path(args.output).resolve()),'checks':'passed'}))
    finally:
        server.shutdown();server.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requests',type=int,default=100)
    parser.add_argument('--failures',type=int,default=15)
    parser.add_argument('--records',type=int,default=30000)
    parser.add_argument('--chromium',action='store_true')
    parser.add_argument('--output',default='test_runtime_data/resource-probe.json')
    asyncio.run(main(parser.parse_args()))
