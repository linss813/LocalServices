"""Portable tests: native commands and SMTP are mocked; never touch real services/email."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch,MagicMock
import xml.etree.ElementTree as ET
import backends
import monitor
import mailer
from mail_template import render
import storage

class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.patch=patch.object(storage,'DATA',Path(self.tmp.name));self.patch.start();self.addCleanup(self.patch.stop)
    def sample(self):
        return dict(host=dict(cpu=95,memoryPercent=50),disks=[],network=dict(recvRate=None,sentRate=None),diskIO=dict(readRate=None,writeRate=None),resources=[],services=[],security=dict(exposed=[],firewall=dict(state='unknown',detail='')))
    def test_rate_reset_and_first_sample(self):
        self.assertIsNone(monitor.safe_rate(100,None,5));self.assertEqual(monitor.safe_rate(120,100,5),4)
        self.assertEqual(monitor.safe_rate(20,100,5),0);self.assertIsNone(monitor.safe_rate(20,10,0))
    def test_exposure_ipv4_ipv6(self):
        for ip in ('127.0.0.1','127.5.3.1','::1'):self.assertFalse(monitor.external(ip))
        for ip in ('0.0.0.0','::','192.168.1.1','fe80::1%en0','*'):self.assertTrue(monitor.external(ip))
    def test_sustained_alert_recovery_and_ack(self):
        engine=monitor.Alerts();engine.rules['recoverSustain']=0;d=self.sample()
        self.assertEqual(engine.evaluate(d,now=0,wall=100),[])
        self.assertEqual(engine.evaluate(d,now=29,wall=129),[])
        events=engine.evaluate(d,now=30,wall=130);self.assertEqual(len(events),1)
        engine.evaluate(d,now=50,wall=150);self.assertEqual(len(engine.events),1)
        engine.acknowledge(events[0]['id']);self.assertTrue(events[0]['acknowledged'])
        d['host']['cpu']=20;engine.evaluate(d,now=51,wall=151);self.assertEqual(events[0]['recovered'],151)
        engine.evaluate(self.sample(),now=52,wall=152);engine.evaluate(self.sample(),now=82,wall=182)
        self.assertEqual(len(engine.events),2)
    def test_no_duplicate_after_restart(self):
        e=monitor.Alerts();e.rules['sustain']=0;e.evaluate(self.sample(),now=0,wall=1)
        restarted=monitor.Alerts();restarted.rules['sustain']=0
        restarted.evaluate(self.sample(),now=time.monotonic()+5,wall=2)
        self.assertEqual(len(restarted.events),1)
    def test_immediate_security_unknown_firewall_not_safe_or_alert(self):
        e=monitor.Alerts();d=self.sample();d['host']['cpu']=0
        self.assertFalse(e.evaluate(d,now=0))
        d['security']['exposed']=[dict(port=6379,address='0.0.0.0',name='redis')]
        events=e.evaluate(d,now=1);self.assertEqual(events[0]['key'],'exposure')
    def test_offline_only_opted_in_services(self):
        e=monitor.Alerts();e.rules['sustain']=0;e.rules['watched']=['custom:one'];d=self.sample();d['host']['cpu']=0
        d['services']=[dict(id='custom:one',kind='custom',running=False,name='API'),dict(id='custom:two',kind='custom',running=False,name='other')]
        self.assertEqual(len(e.evaluate(d,now=0)),1)
    def test_rules_change_recovers_disabled_alert(self):
        e=monitor.Alerts();d=self.sample();d['host']['cpu']=0
        d['security']['firewall']=dict(state='disabled',detail='disabled')
        e.evaluate(d,now=0,wall=1)
        self.assertEqual(len(e.active),1)
        e.configure(dict(e.rules,firewall=False));e.evaluate(d,now=100,wall=2)
        self.assertFalse(e.active);self.assertEqual(e.events[0]['recovered'],2)
    def test_unknown_metric_does_not_claim_recovery(self):
        e=monitor.Alerts();d=self.sample();d['host']['cpu']=0
        d['security']['firewall']=dict(state='disabled',detail='disabled')
        e.evaluate(d,now=0,wall=1);d['security']['firewall']['state']='unknown'
        e.evaluate(d,now=100,wall=2);self.assertEqual(len(e.active),1)
    def test_readonly_disk_is_excluded_and_old_alert_is_suppressed(self):
        engine=monitor.Alerts();engine.rules['diskSustain']=0;d=self.sample();d['host']['cpu']=0
        d['disks']=[dict(mount='/snap/core',percent=100)]
        engine.evaluate(d,now=0,wall=1);self.assertEqual(len(engine.active),1)
        d['disks'][0]['alertable']=False
        events=engine.evaluate(d,now=1,wall=2)
        self.assertFalse(engine.active);self.assertTrue(events[0]['suppressed']);self.assertEqual(events[0]['recovered'],2)
        clean=monitor.Alerts();clean.events.clear();clean.active.clear();clean.pending.clear()
        self.assertEqual(clean.evaluate(d,now=0),[])
    def test_suppressed_disk_event_is_removed_from_mail_outbox(self):
        self.config_email();m=mailer.Mailer()
        event=dict(id='disk-event',title='Disk',detail='100%',time=100,recovered=None,level='warning')
        m.enqueue_events([event]);self.assertEqual(len(m.state['pending']),1)
        event.update(suppressed=True,recovered=200);m.enqueue_events([event]);self.assertFalse(m.state['pending'])
    def test_filesystem_aliases_are_counted_once(self):
        from volumes import summarize
        rows=[dict(device='/dev/vda1',fsId='1',fstype='ext4',mount=m,total=1000,used=300,free=650,percent=31,alertable=True) for m in ('/','/tmp','/var/lib/agent')]
        rows.append(dict(device='/dev/loop0',fsId='2',fstype='squashfs',mount='/snap/core',total=100,used=100,free=0,percent=100,alertable=False))
        d=summarize(rows);self.assertEqual(d['total'],1000);self.assertEqual(d['free'],650);self.assertEqual(d['volumeCount'],1)
        self.assertEqual(d['used'],350);self.assertEqual(d['percent'],35);self.assertEqual(len(d['volumes'][0]['mounts']),3)
    def test_apfs_shared_capacity_is_not_summed(self):
        from volumes import summarize
        rows=[dict(device=device,fsId=str(i),fstype='apfs',mount=mount,total=1000,used=used,free=400,percent=50,alertable=True) for i,(device,mount,used) in enumerate([('/dev/disk3s1s1','/',50),('/dev/disk3s5','/System/Volumes/Data',500)])]
        d=summarize(rows);self.assertEqual(d['total'],1000);self.assertEqual(d['used'],600);self.assertTrue(d['sharedPools'])
    def test_disk_capacity_waits_before_firing(self):
        e=monitor.Alerts();d=self.sample();d['host']['cpu']=0;d['disks']=[dict(mount='/',percent=95,free=5)]
        self.assertFalse(e.evaluate(d,now=0,wall=1));self.assertFalse(e.evaluate(d,now=119,wall=120))
        self.assertEqual(len(e.evaluate(d,now=120,wall=121)),1)
    def test_hysteresis_and_stable_recovery_prevent_flapping(self):
        e=monitor.Alerts();e.rules['sustain']=0;d=self.sample();e.evaluate(d,now=0,wall=1)
        d['host']['cpu']=88;e.evaluate(d,now=100,wall=101);self.assertEqual(len(e.active),1)
        d['host']['cpu']=80;e.evaluate(d,now=110,wall=111);e.evaluate(d,now=169,wall=170);self.assertEqual(len(e.active),1)
        d['host']['cpu']=89;e.evaluate(d,now=170,wall=171);self.assertEqual(len(e.active),1)
        d['host']['cpu']=80;e.evaluate(d,now=180,wall=181);e.evaluate(d,now=240,wall=241)
        self.assertFalse(e.active);self.assertEqual(e.events[0]['recovered'],241)
    def test_recovered_before_delivery_cancels_pending_email(self):
        self.config_email();m=mailer.Mailer();event=dict(id='temporary',title='CPU',detail='95%',time=100,recovered=None,level='warning')
        m.enqueue_events([event]);self.assertEqual(len(m.state['pending']),1)
        event['recovered']=200;m.enqueue_events([event]);self.assertFalse(m.state['pending'])
        m.enqueue_events([event]);self.assertFalse(m.state['pending']);self.assertIn('temporary:触发',m.state['dismissed'])
    def test_email_interval_persists_and_critical_bypasses_it(self):
        self.config_email();m=mailer.Mailer();event=dict(id='one',title='CPU',detail='95%',time=100,recovered=None,level='warning')
        m.enqueue_events([event])
        with patch.object(m,'send'):m.deliver()
        restarted=mailer.Mailer();second=dict(event,id='two');restarted.enqueue_events([second])
        with patch.object(restarted,'send') as send:restarted.deliver();send.assert_not_called()
        critical=dict(event,id='urgent',level='critical');restarted.enqueue_events([critical])
        with patch.object(restarted,'send') as send:restarted.deliver();self.assertEqual(send.call_args.args[1][0]['level'],'critical')
    def test_invalid_rules_are_atomic(self):
        e=monitor.Alerts();before=copy.deepcopy(e.rules)
        for value in (float('nan'),float('inf'),True,-1,101):
            with self.assertRaises(ValueError):e.configure(dict(before,cpu=value))
            self.assertEqual(e.rules,before)
    def test_linux_enumeration_disabled_and_active(self):
        def ctl(scope,*args):
            if args[0]=='list-units':return json.dumps([dict(unit='demo.service')])
            if args[0]=='list-unit-files':return json.dumps([dict(unit_file='demo.service',state='enabled'),dict(unit_file='idle.service',state='disabled')])
            return 'Id=demo.service\nMainPID=42\nActiveState=active\nUnitFileState=enabled\nDescription=Demo\n'
        with patch.object(backends,'systemctl',side_effect=ctl):items,warnings=backends.linux_services()
        self.assertFalse(warnings);self.assertEqual(len(items),4)
        active=next(s for s in items if s['id']=='systemd:user:demo.service');self.assertEqual(active['pid'],42);self.assertTrue(active['autostart'])
        self.assertFalse(next(s for s in items if s['id']=='systemd:system:idle.service')['autostart'])
    def test_systemd_action_scope_and_validation(self):
        svc=dict(id='systemd:system:demo.service',kind='systemd',scope='system')
        with patch.object(backends,'SYSTEM','Linux'),patch.object(backends,'services',return_value=([svc],[])),patch.object(backends,'systemctl') as ctl:
            backends.action(dict(id=svc['id'],action='autostart',enabled=True));ctl.assert_called_once_with('system','enable','demo.service')
            with self.assertRaises(ValueError):backends.action(dict(id='systemd:system:--host=bad',action='stop'))
    def test_systemd_escaping(self):
        self.assertEqual(backends.quote_unit('/tmp/a b%$"\\'),'"/tmp/a b%%$$\\"\\\\"')
    def test_windows_native_enumeration(self):
        s=MagicMock();s.as_dict.return_value=dict(name='Demo',display_name='Demo Service',pid=77,status='running',start_type='automatic')
        with patch.object(backends.psutil,'win_service_iter',return_value=[s],create=True):items,warnings=backends.windows_services()
        self.assertFalse(warnings);self.assertEqual(items[0]['pid'],77);self.assertTrue(items[0]['autostart'])
    def test_windows_task_xml_escapes_args_no_unwanted_autostart(self):
        item=dict(label='local.svcmanager.test')
        with patch.object(backends,'DATA',Path(self.tmp.name)),patch.object(backends,'run') as run:
            backends.windows_write(item,False)
            xml=ET.parse(Path(self.tmp.name)/(item['label']+'.xml'));ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
            self.assertIsNone(xml.find('.//t:LogonTrigger',ns));self.assertEqual(xml.find('.//t:ExecutionTimeLimit',ns).text,'PT0S')
            self.assertIn('service_runner.py',xml.find('.//t:Arguments',ns).text)
            backends.windows_write(item,True)
            self.assertIsNotNone(ET.parse(Path(self.tmp.name)/(item['label']+'.xml')).find('.//t:LogonTrigger',ns))
    def test_windows_scm_action(self):
        svc=dict(id='windows:Demo',kind='windows',label='Demo')
        with patch.object(backends,'SYSTEM','Windows'),patch.object(backends,'services',return_value=([svc],[])),patch.object(backends,'run') as run:
            backends.action(dict(id=svc['id'],action='autostart',enabled=False));run.assert_called_once_with(['sc.exe','config','Demo','start=','demand'])
    def config_email(self):storage.write('email.json',dict(enabled=True,enabledAt=0,host='test.invalid',port=465,encryption='ssl',username='test',password='test-secret',**{'from':'a@example.com'},to='b@example.com'))
    def test_email_html_plain_escaping(self):
        items=[dict(title='<script>alert(1)</script>',detail='service <api> & data',time=100,state='触发',level='warning')]
        html,plain=render(items,'Test <host>')
        self.assertNotIn('<script>',html);self.assertIn('&lt;script&gt;',html);self.assertIn('<script>',plain);self.assertIn('UTC+08',html)
    def test_smtp_multipart_ssl(self):
        self.config_email();m=mailer.Mailer();cfg=storage.read('email.json',{})
        item=dict(title='Test',detail='Healthy',time=100,state='测试',level='info')
        with patch.object(mailer.smtplib,'SMTP_SSL') as smtp:
            m.send(cfg,[item]);message=smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual(message.get_content_type(),'multipart/alternative');self.assertIn('text/html',message.as_string());self.assertNotIn('test-secret',message.as_string())
    def test_outbox_dedup_recovery(self):
        self.config_email();m=mailer.Mailer();e=dict(id='one',title='CPU',detail='95%',time=100,recovered=None,level='warning')
        m.enqueue_events([e]);m.enqueue_events([e]);self.assertEqual(len(m.state['pending']),1)
        with patch.object(m,'send'):m.deliver()
        self.assertFalse(m.state['pending']);m.enqueue_events([e]);self.assertFalse(m.state['pending'])
        e['recovered']=200;m.enqueue_events([e]);self.assertEqual(len(m.state['pending']),1)
        self.assertEqual(m.state['pending'][0]['state'],'恢复')
        restarted=mailer.Mailer();restarted.enqueue_events([e]);self.assertEqual(len(restarted.state['pending']),1)
    def test_mail_failure_retry_masks_credentials(self):
        self.config_email();m=mailer.Mailer();m.test()
        with patch.object(m,'send',side_effect=mailer.smtplib.SMTPAuthenticationError(535,b'test-secret')):m.deliver()
        self.assertFalse(m.state['last']['ok']);self.assertNotIn('test-secret',json.dumps(m.public()))
        self.assertEqual(m.state['pending'][0]['attempts'],1);self.assertGreater(m.state['pending'][0]['next'],time.time())
    def test_dead_service_pid_does_not_keep_cached_running_state(self):
        m=monitor.Monitor()
        stale=dict(id='systemd:system:dead.service',name='dead.service',kind='systemd',pid=2147483647,state='active',autostart=False)
        with patch.object(backends,'services',return_value=([stale],[])),patch.object(monitor,'firewall',return_value=dict(state='unknown',detail='',scope='test')):
            d=m.collect()
        service=next(s for s in d['services'] if s['id']==stale['id'])
        self.assertFalse(service['running']);self.assertIsNone(service['pid'])
    def test_resource_groups_no_pid_overlap(self):
        m=monitor.Monitor()
        with patch.object(backends,'services',return_value=([],[])),patch.object(monitor,'firewall',return_value=dict(state='unknown',detail='',scope='test')):
            d=m.collect()
        pids=[pid for g in d['resources'] for pid in g['pids']]
        self.assertEqual(len(pids),len(set(pids)));self.assertEqual(len(pids),d['host']['processCount'])
        self.assertTrue(d['network']['interfaces']);self.assertIsNone(d['diskIO']['readRate'])

if __name__=='__main__':unittest.main()
