import importlib.machinery
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile
import io

PATH = Path(__file__).resolve().parents[2] / 'packages/ufi-voice/files/ufi-ims-policy'
loader = importlib.machinery.SourceFileLoader('ufi_ims_policy', str(PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
policy = importlib.util.module_from_spec(spec)
loader.exec_module(policy)


def response(kind, data, declared=None):
    return ('responseTLV:\nresponse  type = %s\nresponse  length = %d\n'
            'response  value = %s\n' % (kind, len(data) if declared is None else declared,
                                        ':'.join('%02X' % x for x in data)))


class PolicyResponseTests(unittest.TestCase):
    def test_legacy_registration_boolean_and_modern_enum(self):
        self.assertEqual(policy.registration({0x10: b'\0'}), 'not-registered')
        self.assertEqual(policy.registration({0x10: b'\1'}), 'registered')
        self.assertEqual(policy.registration({0x12: b'\x01\0\0\0'}), 'registering')
        self.assertEqual(policy.registration({0x12: b'\x02\0\0\0'}), 'registered')

    def test_service_status_enum_is_not_legacy_registration_boolean(self):
        self.assertEqual(policy.registration({0x10: bytes(4)}), 'unknown')
        self.assertEqual(policy.registration({}), 'unknown')

    def test_named_profile_fields_and_raw_nas_fields(self):
        text = response('"Result" (0x02)', bytes(4))
        text += response('0x20', b'\x03\0\0\0')
        text += response('"IMCN Flag" (0x22)', b'\x01')
        self.assertEqual(policy.decode(text)[0x20], b'\x03\0\0\0')
        self.assertEqual(policy.decode(text)[0x22], b'\x01')

    def test_qmi_failure_is_rejected_even_when_command_exits_zero(self):
        with self.assertRaisesRegex(RuntimeError, 'error=54'):
            policy.decode(response('0x02', b'\x01\0\x36\0'))

    def test_missing_result_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, 'missing QMI result'):
            policy.decode(response('0x22', b'\x01'))

    def test_truncated_and_duplicate_results_rejected(self):
        for text in (response('0x02', bytes(3), declared=4),
                     response('0x02', bytes(4)) * 2):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                policy.decode(text)

    def test_key_values_preserve_colon_in_values(self):
        self.assertEqual(policy.kv('sim.path : /example\nother : a:b'),
                         {'sim.path': '/example', 'other': 'a:b'})


class PolicyLifecycleTests(unittest.TestCase):
    def invoke(self, owned, operator, qmi):
        original_open = open
        def local_open(path, *args, **kwargs):
            if str(path) == '/tmp/sysinfo/board_name':
                return io.StringIO('thwc,ufi103s-v05')
            return original_open(path, *args, **kwargs)
        def command(*args):
            if args[0] == 'uci':
                return '1'
            if args[0] == 'qmicli':
                return "[3] 3gpp - profile3\n APN: 'ims'\n"
            if '-i' in args:
                return 'sim.properties.operator-code : ' + operator
            if '--voice-list-calls' in args:
                return 'No calls were found'
            return 'modem.generic.sim : /org/freedesktop/ModemManager1/SIM/0'
        with patch.object(policy, 'OWNED', str(owned)), \
             patch.object(policy, 'open', side_effect=local_open, create=True), \
             patch.object(policy.glob, 'glob', return_value=[]), \
             patch.object(policy, 'run', side_effect=command), \
             patch.object(policy, 'profile'), \
             patch.object(policy, 'qmi', side_effect=qmi):
            policy.sample()

    def test_acknowledged_set_retains_ownership_when_readback_times_out(self):
        with tempfile.TemporaryDirectory() as directory:
            owned = Path(directory) / 'owned'
            with self.assertRaisesRegex(RuntimeError, 'timeout'):
                self.invoke(owned, '46001', [{0x20: b'\x02\0\0\0'}, {},
                                            RuntimeError('timeout')])
            self.assertTrue(owned.exists())

    def test_already_restored_other_carrier_clears_stale_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            owned = Path(directory) / 'owned'
            owned.touch()
            self.invoke(owned, '46000', [{0x20: b'\x02\0\0\0'}])
            self.assertFalse(owned.exists())

    def test_existing_ps_preference_is_not_claimed(self):
        with tempfile.TemporaryDirectory() as directory:
            owned = Path(directory) / 'owned'
            self.invoke(owned, '46001', [{0x20: b'\x03\0\0\0'}])
            self.assertFalse(owned.exists())


if __name__ == '__main__':
    unittest.main()
