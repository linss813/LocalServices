"""Storage accounting and proxy isolation regressions; no SMTP or service mutation."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import volumes
from nodes import Nodes


def disk(device,mount,total,free,**extra):
    return dict(device=device,mount=mount,fsId=device,fstype='ext4',total=total,free=free,used=total-free,
        alertable=True,readOnly=False,**extra)

class StorageReview(unittest.TestCase):
    def test_system_scope_does_not_include_large_external_disk(self):
        d=volumes.summarize([disk('/dev/root','/',100,80,location='internal'),disk('/dev/usb','/usb',900,90,location='external')])
        self.assertEqual(d['scopes']['system']['percent'],20)
        self.assertEqual(d['scopes']['external']['percent'],90)
        self.assertEqual(d['scopes']['all']['percent'],83)
        self.assertEqual(d['scopes']['all']['total'],1000)
    def test_apfs_capacity_uses_authoritative_container_not_smallest_volume_free(self):
        rows=[dict(disk('/dev/disk3s1s1','/',1000,1),fstype='apfs',alertable=False),
              dict(disk('/dev/disk3s5','/System/Volumes/Data',1000,100),fstype='apfs')]
        d=volumes.summarize(rows,{'disk3':dict(total=1000,free=300,sampledAt=123)})
        self.assertEqual(d['scopes']['system']['percent'],70)
        self.assertEqual(d['volumes'][0]['capacitySampledAt'],123)
        self.assertIn('diskutil',d['volumes'][0]['capacitySource'])
    def test_unknown_type_is_not_mislabeled_external(self):
        d=volumes.summarize([disk('/dev/root','/',100,30)])
        self.assertEqual(d['volumes'][0]['location'],'unknown')
        self.assertEqual(d['scopes']['external']['volumeCount'],0)
        self.assertEqual(d['scopes']['system']['volumeCount'],1)
    def test_empty_system_scope_does_not_silently_become_external(self):
        d=volumes.summarize([disk('/dev/usb','/usb',100,50,location='external')])
        self.assertIsNone(d['scopes']['system']['percent'])
        self.assertEqual(d['scopes']['all']['percent'],50)
    def test_reserved_space_is_separate_and_totals_balance(self):
        d=volumes.summarize([dict(disk('/dev/root','/',1000,650),used=300)])
        self.assertEqual(d['reserved'],50);self.assertEqual(d['used'],350)
        self.assertEqual(d['used']+d['free'],d['total'])
    def test_mac_hardware_type_propagates_to_apfs_snapshot(self):
        listing={'AllDisksAndPartitions':[
            dict(DeviceIdentifier='disk0',Partitions=[dict(DeviceIdentifier='disk0s2')]),
            dict(DeviceIdentifier='disk4',Partitions=[dict(DeviceIdentifier='disk4s1')]),
            dict(DeviceIdentifier='disk3',APFSPhysicalStores=[dict(DeviceIdentifier='disk0s2')],
                APFSVolumes=[dict(DeviceIdentifier='disk3s1',MountedSnapshots=[dict(SnapshotBSD='disk3s1s1')])])
        ]}
        def probe(*args):
            if args[0]=='list':return listing
            return dict(Internal=args[-1]=='disk0',VirtualOrPhysical='Physical')
        with patch.object(volumes,'plist_command',side_effect=probe):data=volumes.mac_topology()
        self.assertEqual(data['disk3s1s1']['location'],'internal')
        self.assertEqual(data['disk3s1s1']['ioDevices'],['disk0'])
        self.assertEqual(data['disk4s1']['location'],'external')
    def test_linux_io_does_not_add_partition_loop_and_mapper_to_physical_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            real=Path;root=real(tmp);(root/'vda1').mkdir();(root/'vda1/partition').touch()
            def path(value):return root if str(value)=='/sys/class/block' else real(value)
            rows=[dict(name=n,readRate=10,writeRate=20) for n in ('vda','vda1','loop0','dm-0')]
            with patch.object(volumes,'Path',side_effect=path):result=volumes.aggregate_io(rows,'Linux')
            self.assertEqual(result['aggregateDevices'],['vda']);self.assertEqual(result['readRate'],10)
    def test_integration_script_import_does_not_touch_network(self):
        import importlib.util
        import urllib.request
        spec=importlib.util.spec_from_file_location('isolated_integration_script',Path(__file__).with_name('test_manager.py'))
        module=importlib.util.module_from_spec(spec)
        with patch.object(urllib.request,'urlopen',side_effect=AssertionError('network during import')):
            spec.loader.exec_module(module)
        self.assertTrue(callable(module.main))
    def test_slow_proxy_node_does_not_block_other_nodes(self):
        proxy=Nodes();entered=threading.Event();release=threading.Event();result=[]
        def request(node,path):
            if node=='slow':entered.set();release.wait(3)
            return dict(age=0,tag=node)
        with patch.object(proxy,'resolve',side_effect=lambda ident:ident),patch.object(proxy,'request',side_effect=request):
            thread=threading.Thread(target=lambda:result.append(proxy.status('slow')));thread.start()
            try:
                self.assertTrue(entered.wait(1))
                fast=[];done=threading.Event()
                worker=threading.Thread(target=lambda:(fast.append(proxy.status('fast')),done.set()));worker.start()
                self.assertTrue(done.wait(1),'slow node held a global network lock')
                self.assertEqual(fast[0]['tag'],'fast');worker.join()
            finally:release.set();thread.join()

if __name__=='__main__':unittest.main()
