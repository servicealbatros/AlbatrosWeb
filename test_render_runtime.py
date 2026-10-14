"""Migration invariants: no Windows interpreter, no public CDP/worker port."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory

spec = importlib.util.spec_from_file_location('render_runtime', Path(__file__).parent / 'scripts/render_robot_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RenderRuntimeTests(unittest.TestCase):
    def test_render_stale_marker_recovery_is_scoped_to_same_single_instance_service(self):
        alive = lambda pid: True
        self.assertTrue(runtime.stale_lock_owner('srv-test-old-44', 'srv-test-new', 'srv-test', alive))
        self.assertFalse(runtime.stale_lock_owner('personal-laptop-44', 'srv-test-new', 'srv-test', alive))
        self.assertFalse(runtime.stale_lock_owner('srv-other-old-44', 'srv-test-new', 'srv-test', alive))
        self.assertFalse(runtime.stale_lock_owner('srv-test-old-44', 'srv-test-new', '', alive))

    def test_active_same_host_owner_is_not_unlocked(self):
        self.assertFalse(runtime.stale_lock_owner('srv-test-new-44', 'srv-test-new', 'srv-test', lambda pid: True))
        self.assertTrue(runtime.stale_lock_owner('srv-test-new-44', 'srv-test-new', 'srv-test', lambda pid: False))
        self.assertFalse(runtime.stale_lock_owner('invalid-marker', 'srv-test-new', 'srv-test', lambda pid: False))

    def test_browser_profile_identity_survives_runtime_reinitialization(self):
        with TemporaryDirectory() as root:
            profile = Path(root) / 'linux_browser'
            with patch.object(runtime.os.path, 'ismount', return_value=True):
                before = runtime.profile_storage(profile)
                (profile / 'session-fixture').write_text('existing browser data', encoding='utf-8')
                after = runtime.profile_storage(profile)
            self.assertEqual(before['browser_profile_id'], after['browser_profile_id'])
            self.assertTrue(after['browser_profile_on_persistent_mount'])
            self.assertEqual((profile / 'session-fixture').read_text(), 'existing browser data')

    def test_core_overrides_windows_settings_and_removes_external_bridge(self):
        with patch.dict(os.environ, {'ALBATROS_CLOUD_MODE': 'true',
                'RECEPTION_BRIDGE_URL': 'https://old-worker.example',
                'ALBATROS_TECHNICIAN_PYTHON': 'C:/old/python.exe'}):
            env = runtime.core_environment()
        self.assertEqual(env['ALBATROS_BIND_HOST'], '127.0.0.1')
        self.assertEqual(env['ALBATROS_PORT'], '8010')
        self.assertEqual(env['ALBATROS_TECHNICIAN_PYTHON'], runtime.sys.executable)
        self.assertNotIn('RECEPTION_BRIDGE_URL', env)
        self.assertEqual(env['ALBATROS_GATEWAY_ONLY'], 'false')

    def test_browser_keeps_debugging_private_and_loads_existing_extension(self):
        command = runtime.browser_command('/opt/chromium', '/private/profile')
        self.assertIn('--remote-debugging-address=127.0.0.1', command)
        self.assertIn('--remote-debugging-port=9222', command)
        self.assertTrue(any(arg.startswith('--load-extension=') for arg in command))
        self.assertNotIn('--remote-debugging-address=0.0.0.0', command)

    def test_internal_bridge_flag_does_not_allow_arbitrary_http_origins(self):
        from reception_bridge import bridge_url
        with patch.dict(os.environ, {'ALBATROS_INTERNAL_BRIDGE': 'true',
                                    'RECEPTION_BRIDGE_URL': 'http://127.0.0.1:8010'}):
            self.assertEqual(bridge_url(), 'http://127.0.0.1:8010')
            os.environ['RECEPTION_BRIDGE_URL'] = 'http://public.example:8010'
            with self.assertRaises(ValueError):
                bridge_url()
        with patch.dict(os.environ, {'ALBATROS_INTERNAL_BRIDGE': 'false',
                                    'RECEPTION_BRIDGE_URL': 'http://127.0.0.1:8010'}):
            with self.assertRaises(ValueError):
                bridge_url()


if __name__ == '__main__':
    unittest.main()
