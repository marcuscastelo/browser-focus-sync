import importlib.util
import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import bfs_config as cfg

spec=importlib.util.spec_from_file_location("manage",Path(__file__).resolve().parents[1]/"manage.py")
manage=importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)


class SetupTests(unittest.TestCase):
    def test_matching_inventory_is_accepted(self):
        own={"tabIds":["a","b"],"spaceIds":["s"],"folderIds":["f"]}
        manage.require_alignment(own,{**own,"tabIds":["b","a"]})

    def test_drift_never_seeds_a_baseline(self):
        own={"tabIds":["a"],"spaceIds":["s"],"folderIds":[]}
        with self.assertRaisesRegex(ValueError,"Sessions differ"):
            manage.require_alignment(own,{**own,"tabIds":["a","extra"]})

    def test_mac_plist_uses_chosen_paths(self):
        home=Path('/Users/example user')
        target,content=manage.service_files('mac',home/'Sync Data',home/'sync.toml',home)
        data=plistlib.loads(content)
        self.assertEqual(data['ProgramArguments'][0],str(home/'Sync Data/venv/bin/python'))
        self.assertEqual(data['EnvironmentVariables']['BROWSER_FOCUS_SYNC_CONFIG'],str(home/'sync.toml'))
        self.assertIn('LaunchAgents',str(target))

    def test_systemd_paths_are_not_shell_fragments(self):
        _,content=manage.service_files('linux',Path('/home/example/Sync Data'),Path('/home/example/100%config.toml'),Path('/home/example'))
        text=content.decode()
        self.assertIn('100%%config.toml',text)
        self.assertIn('"/home/example/Sync Data/venv/bin/python"',text)
        self.assertIn('UMask=0077',text)

    def test_config_requires_explicit_absolute_profile(self):
        with patch.object(cfg,'SETTINGS',{'linux':{'profile':'relative/profile'}}):
            with self.assertRaises(ValueError):cfg.profile('linux')

    def test_inventory_reads_synthetic_session_without_urls(self):
        import lz4.block
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            session={'tabs':[{'zenSyncId':'a','entries':[{'url':'https://private.invalid'}]}],'spaces':[{'uuid':'s'}],'folders':[]}
            (root/'zen-sessions.jsonlz4').write_bytes(b'mozLz40\0'+lz4.block.compress(json.dumps(session).encode()))
            with patch.object(cfg,'profile',return_value=root):
                result=manage.inventory(cfg,'linux')
            self.assertEqual(result['tabIds'],['a'])
            self.assertNotIn('private.invalid',json.dumps(result))

    def test_empty_inventory_is_rejected(self):
        import lz4.block
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'zen-sessions.jsonlz4').write_bytes(b'mozLz40\0'+lz4.block.compress(b'{"tabs":[],"spaces":[]}'))
            with patch.object(cfg,'profile',return_value=root),self.assertRaises(ValueError):
                manage.inventory(cfg,'linux')

    def test_install_dry_run_cannot_start_services_or_write_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(cfg,'DATA_DIR',root/'data'),patch.object(cfg,'profile',return_value=root),patch.object(manage.subprocess,'run') as run,patch.object(cfg,'prepare_directories') as prepare:
                manage.install(cfg,'linux',True)
                run.assert_not_called();prepare.assert_not_called()
                self.assertFalse((root/'data').exists())

    def test_install_refuses_existing_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(cfg,'DATA_DIR',root),patch.object(cfg,'profile',return_value=root),patch.object(manage.subprocess,'run') as run:
                with self.assertRaisesRegex(ValueError,'already exists'):
                    manage.install(cfg,'linux',False)
                run.assert_not_called()


if __name__=='__main__':unittest.main()
