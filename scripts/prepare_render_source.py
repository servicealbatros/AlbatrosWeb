"""Create an explicit source-only directory. Never copy the live data tree.

This does not commit, push, create Render resources or upload secrets.
"""
import json
from pathlib import Path
import shutil

BASE = Path(__file__).resolve().parents[1]
FILES = (
    'app.py', 'offer_presentation.py', 'customer_offer_text.py', 'cloud_app.py', 'reception_bridge.py', 'cloud_backup.py', 'navigation_ui.py',
    'agent.py', 'security.py', 'database.py', 'labour.py',
    'vision.py', 'dashboard_devices.py', 'scheduler.py', 'schedule_import_control.py', 'mechanic_schedule_clock.py', 'dsm_session_vault.py', 'automotive_vocabulary.py',
    'oe_english_vocabulary.py', 'part_accuracy.py', 'worker_run_guard.py', 'browser_resources.py',
    'catalogue_probe.py', 'catalogue_crawler.py', 'original_oil_policy.py', 'provider_sessions.py', 'labour_fallback_runner.py', 'dsm_readonly.py', 'dsm_schedule_sync.py',
    'dsm_accepted_offers.py', 'dsm_booking_approval.py', 'dsm_billing_sync.py', 'dsm_open_orders_readonly.py', 'dsm_history_import.py', 'dsm_history_cache.py', 'pre_reception.py', 'dsm_pre_reception.py', 'pre_arrival_reminders.py', 'dsm_pre_arrival.py',
    'autototal_request_worker.py', 'technician_request_worker_v6_time_parse_fix.py',
    'tecrmi_request_worker.py', 'robot_cdp_health.py', 'runtime_control.py',
    'requirements.txt', 'render.yaml', 'Dockerfile', '.dockerignore', '.gitignore',
    '.env.example', 'RENDER_DEPLOYMENT.md', 'test_cloud_access.py',
)


def prepare():
    target = BASE / 'deploy' / 'render-source'
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in FILES:
        shutil.copy2(BASE / name, target / name)
        copied.append(name)
    for folder in ('static', 'website/dist'):
        for source in (BASE / folder).rglob('*'):
            if not source.is_file() or source.suffix.lower() not in {'.html', '.css', '.js', '.svg', '.jpg', '.jpeg', '.webp', '.png'}:
                continue
            relative = source.relative_to(BASE)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(relative.as_posix())
    # Fail if this directory was contaminated outside the explicit copier.
    unexpected = [str(p.relative_to(target)) for p in target.rglob('*')
                  if p.is_file() and p.relative_to(target).as_posix() not in copied]
    if unexpected:
        raise RuntimeError('Unexpected files in source package: ' + ', '.join(unexpected))
    print(json.dumps({'source_directory': str(target), 'files':len(copied),
                      'contains_runtime_data':False,'published':False}))


if __name__ == '__main__':
    prepare()
