"""TecRMI first; open Haynes only to complete missing labour information."""
import asyncio
from datetime import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from worker_run_guard import write_worker_result


def needs_haynes(result):
    try:
        hours = float(result.get('technical_hours') or 0)
    except (TypeError, ValueError):
        return True
    if result.get('status') != 'ready' or not math.isfinite(hours) or hours <= 0:
        return True
    if result.get('unresolved_operations') or result.get('missing_operations'):
        return True
    wanted = {r.get('id') for r in result.get('detected_operations', []) if isinstance(r, dict) and r.get('id')}
    found = {r.get('id') for r in result.get('resolved_operations', []) if isinstance(r, dict) and r.get('id')}
    return bool(wanted - found)


def run_worker(command, log_path, timeout):
    with Path(log_path).open('w', encoding='utf-8') as log:
        try:
            return subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, shell=False,
                                  timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).returncode
        except subprocess.TimeoutExpired:
            return 124


async def haynes_on_demand(command, log_path, timeout):
    from playwright.async_api import async_playwright
    from provider_sessions import ProviderLoginVault, reconnect, provider_url
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp('http://127.0.0.1:9222', timeout=15000)
        context = browser.contexts[0]
        page = next((p for p in context.pages if not p.is_closed() and provider_url('Haynes', p.url)), None)
        created = page is None
        if created:
            page = await context.new_page()
        try:
            if created:
                await page.goto('https://www.workshopdata.com/touch/site/layout/makesOverview', wait_until='domcontentloaded', timeout=30000)
            if await page.locator('input[type=password]:visible').count():
                profile, key = os.getenv('ALBATROS_BROWSER_PROFILE_DIR'), os.getenv('ALBATROS_PROVIDER_STATE_KEY')
                credentials = ProviderLoginVault(profile, key, 'Haynes').load() if profile and key else None
                if credentials:
                    await reconnect(page, credentials, 'Haynes')
            return await asyncio.to_thread(run_worker, command, log_path, timeout)
        finally:
            # Never close a tab opened by a person or another worker.
            if created and not page.is_closed():
                await page.close()


def run(request_id, vin, symptom, haynes_output, offer_result, tecrmi_output,
        haynes_log, tecrmi_log, haynes_worker, tecrmi_worker):
    token = os.getenv('ALBATROS_WORKER_RUN_ID', '')
    timeout = max(60, int(os.getenv('ALBATROS_LABOUR_TIMEOUT_SECONDS', '240')))
    tecrmi_command = [sys.executable, str(tecrmi_worker), str(request_id), vin, symptom, str(tecrmi_output), str(offer_result)]
    try:
        code = run_worker(tecrmi_command, tecrmi_log, timeout)
    except OSError:
        code = 1
    try:
        result = json.loads(Path(tecrmi_output).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    if result.get('status') in {None, 'running', 'standby'}:
        result = {'request_id': int(request_id), 'status': 'error', 'source': 'AutoTotal TecRMI',
                  'message': 'Căutarea TecRMI nu s-a încheiat. Se verifică sursa de rezervă.',
                  'exit_code': code}
        if not write_worker_result(tecrmi_output, result, token):
            return
    base = {'request_id': int(request_id), 'vin': vin, 'symptom': symptom, 'source': 'HaynesPro — rezervă'}
    if not needs_haynes(result):
        write_worker_result(haynes_output, {**base, 'status': 'unavailable', 'skipped_due_to_tecrmi_complete': True,
            'message': 'TecRMI a completat toate operațiile. Haynes nu a fost deschis.'}, token)
        return
    started = {**base, 'status': 'running', 'started_at': datetime.now().isoformat(timespec='seconds'),
               'fallback_reason': result.get('status') or 'no_result',
               'message': 'TecRMI nu a completat timpii. Se verifică Haynes ca rezervă.'}
    if not write_worker_result(haynes_output, started, token):
        return  # A newer request run superseded this one.
    command = [sys.executable, str(haynes_worker), str(request_id), vin, symptom, str(haynes_output), str(offer_result)]
    try:
        code = asyncio.run(haynes_on_demand(command, haynes_log, timeout))
        try:
            reserve_result = json.loads(Path(haynes_output).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            reserve_result = {}
        if not isinstance(reserve_result, dict) or reserve_result.get('status') in {None, 'running', 'standby'}:
            write_worker_result(haynes_output, {**started, 'status': 'error', 'message': 'Căutarea de rezervă Haynes nu s-a încheiat. Este necesară verificarea consilierului.'}, token)
    except Exception:
        write_worker_result(haynes_output, {**started, 'status': 'error', 'message': 'Haynes nu a putut fi deschis sau autentificat. Verifică furnizorul în fereastra protejată.'}, token)


if __name__ == '__main__':
    run(*sys.argv[1:])
