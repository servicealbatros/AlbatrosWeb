"""Migration invariants: no Windows interpreter, no public CDP/worker port."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('render_runtime', Path(__file__).parent / 'scripts/render_robot_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RenderRuntimeTests(unittest.TestCase):
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
