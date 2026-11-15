import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import plistlib
import macos_backend as mac

class LaunchAgents(unittest.TestCase):
    def test_existing_agent_controls_preserve_plist(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'example.plist'
            raw=plistlib.dumps(dict(Label='org.example.web',ProgramArguments=['/usr/bin/true']))
            path.write_bytes(raw)
            with patch.object(mac,'AGENTS',Path(folder)), patch.object(mac,'configs',return_value=[]), patch.object(mac,'brew_services',return_value=[]), patch.object(mac,'launch_jobs',return_value={}), patch.object(mac,'disabled_jobs',return_value=set()), patch.object(mac,'run',return_value='') as run:
                row=mac.snapshot()['services'][0]
                self.assertEqual(row['id'],'launchd:org.example.web')
                mac.action(dict(id=row['id'],action='start'))
                self.assertEqual(run.call_args_list[0].args[0][1],'bootstrap')
                self.assertEqual(run.call_args_list[1].args[0][1],'kickstart')
                mac.action(dict(id=row['id'],action='autostart',enabled=False))
                self.assertEqual(run.call_args.args[0][1],'disable')
                self.assertEqual(path.read_bytes(),raw)
                with self.assertRaises(ValueError):mac.action(dict(id=row['id'],action='delete'))
                with self.assertRaises(ValueError):mac.action(dict(id='launchd:com.apple.fake',action='stop'))
    def test_stop_unloads_without_deleting_original(self):
        with patch.object(mac,'native_plists',return_value={'example':Path('/tmp/example.plist')}), patch.object(mac,'launch_jobs',return_value={'example':{'pid':123}}), patch.object(mac,'run',return_value='') as run:
            mac.action(dict(id='launchd:example',action='stop'))
            self.assertEqual(run.call_args.args[0][1],'bootout')

if __name__=='__main__':unittest.main()
