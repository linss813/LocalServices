"""Filesystem identity, authoritative APFS pool capacity and explicit storage scopes."""
import os
import plistlib
import re
import subprocess
import time
from pathlib import Path


def plist_command(*args):
    result=subprocess.run(['/usr/sbin/diskutil',*args],capture_output=True,timeout=5)
    if result.returncode:raise ValueError('diskutil 信息不可读')
    return plistlib.loads(result.stdout)


def mac_topology():
    listing=plist_command('list','-plist');devices={};physical={}
    for disk in listing.get('AllDisksAndPartitions',[]):
        if disk.get('APFSVolumes') is not None:continue
        ident=disk['DeviceIdentifier']
        try:
            info=plist_command('info','-plist',ident)
            location='internal' if info.get('Internal') is True else 'external' if info.get('Internal') is False else 'unknown'
            io_device=ident if info.get('VirtualOrPhysical')!='Virtual' else None
        except (OSError,ValueError,subprocess.SubprocessError):location='unknown';io_device=None
        physical[ident]=dict(location=location,ioDevices=[io_device] if io_device else [])
        devices[ident]=dict(physical[ident])
        for partition in disk.get('Partitions',[]):devices[partition['DeviceIdentifier']]=dict(physical[ident])
    for disk in listing.get('AllDisksAndPartitions',[]):
        if not disk.get('APFSVolumes'):continue
        stores=disk.get('APFSPhysicalStores',[])
        source=[devices.get(s['DeviceIdentifier'],{}) for s in stores]
        locations={s.get('location','unknown') for s in source}
        location=next(iter(locations)) if len(locations)==1 else 'unknown'
        meta=dict(location=location,ioDevices=sorted({n for s in source for n in s.get('ioDevices',[])}),pool=disk['DeviceIdentifier'])
        devices[disk['DeviceIdentifier']]=meta
        for volume in disk['APFSVolumes']:
            devices[volume['DeviceIdentifier']]=meta
            for snap in volume.get('MountedSnapshots',[]):devices[snap['SnapshotBSD']]=meta
    return devices


def mac_pools():
    stamp=time.time();pools={}
    for pool in plist_command('apfs','list','-plist').get('Containers',[]):
        total=pool.get('CapacityCeiling');free=pool.get('CapacityFree')
        if isinstance(total,int) and isinstance(free,int) and 0<=free<=total:
            pools[pool['ContainerReference']]=dict(total=total,free=free,used=total-free,sampledAt=stamp)
    return pools


def linux_device_info(device):
    name=Path(os.path.realpath(device)).name
    sys=Path('/sys/class/block')/name
    if not sys.exists():return dict(location='unknown',ioDevices=[])
    base=sys.resolve().parent.name if (sys/'partition').exists() else name
    try:removable=(Path('/sys/class/block')/base/'removable').read_text().strip()=='1'
    except OSError:removable=False
    usb='/usb' in str(sys.resolve())
    io=[] if base.startswith(('dm-','md','loop')) else [base]
    return dict(location='external' if removable or usb else 'internal',ioDevices=io)


def device_metadata(device,mount,system,topology=None):
    if system=='Darwin':return dict((topology or {}).get((device[5:] if device.startswith('/dev/') else device),{}))
    if system=='Linux':return linux_device_info(device)
    if system=='Windows':
        try:
            import ctypes
            kind=ctypes.windll.kernel32.GetDriveTypeW(str(mount))
            return dict(location={2:'external',3:'unknown',4:'network',5:'optical'}.get(kind,'unknown'),ioDevices=[])
        except (AttributeError,OSError):pass
    return dict(location='unknown',ioDevices=[])


def aggregate(volumes):
    total=sum(v['total'] for v in volumes);used=sum(v['used'] for v in volumes);free=sum(v['free'] for v in volumes)
    return dict(total=total,used=used,free=free,reserved=sum(v.get('reserved',0) for v in volumes),
        percent=round(used/total*100,1) if total else None,volumeCount=len(volumes),
        volumeIds=[v['id'] for v in volumes],ioDevices=sorted({n for v in volumes for n in v.get('ioDevices',[])}))


def summarize(disks,pools=None):
    groups={};pools=pools or {}
    for disk in disks:
        apfs=re.match(r'/dev/(disk\d+)s\d+',disk['device']) if disk['fstype'].lower()=='apfs' else None
        pool=disk.get('pool') or (apfs[1] if apfs else None)
        key='apfs:'+pool if pool else 'fs:'+str(disk.get('fsId') or disk['device'])
        groups.setdefault(key,dict(id=key,records=[],sharedPool=bool(pool),pool=pool))['records'].append(disk)
    volumes=[]
    for group in groups.values():
        records=group.pop('records')
        primary=min(records,key=lambda d:(0 if d['mount']=='/' else 1 if d['mount'].endswith('/Volumes/Data') else 2,len(d['mount'])))
        authoritative=pools.get(group['pool'])
        if authoritative:
            total=authoritative['total'];free=authoritative['free'];used=total-free;reserved=0
            source='APFS 容器（diskutil）';sampled=authoritative.get('sampledAt')
        elif group['sharedPool']:
            # Fallback is explicitly estimated; never sum APFS volume capacities.
            total=max(d['total'] for d in records);free=min(d['free'] for d in records);used=total-free;reserved=0
            source='APFS 共享池估算（系统容器信息不可读）';sampled=None
        else:
            total=primary['total'];free=primary['free'];reserved=max(0,total-primary['used']-free);used=total-free
            source='文件系统（占用含系统保留）';sampled=None
        locations={d.get('location','unknown') for d in records if d.get('location','unknown')!='unknown'}
        location=next(iter(locations)) if len(locations)==1 else 'unknown'
        is_system=any(d.get('isSystem',d['mount']=='/' or d['mount'].rstrip('\\/').upper()==os.environ.get('SystemDrive','C:').upper()) for d in records)
        volumes.append(dict(group,device=primary['device'],fstype=primary['fstype'],mount=primary['mount'],
            mounts=sorted({d['mount'] for d in records}),total=total,used=used,free=free,reserved=reserved,
            percent=round(used/total*100,1) if total else None,capacitySource=source,capacitySampledAt=sampled,
            location=location,isSystem=is_system,ioDevices=sorted({n for d in records for n in d.get('ioDevices',[])}),
            alertable=any(d.get('alertable',True) for d in records),readOnly=all(d.get('readOnly',False) for d in records)))
    writable=[v for v in volumes if v['alertable']]
    scopes=dict(system=aggregate([v for v in writable if v['isSystem']]),
        all=aggregate(writable),external=aggregate([v for v in writable if v['location']=='external']))
    return dict(aggregate(writable),scopes=scopes,defaultScope='system',mountCount=len(disks),
        excludedCount=len(volumes)-len(writable),volumes=volumes,
        sharedPools=any(v['sharedPool'] for v in writable))


def aggregate_io(devices,system):
    included=[]
    for device in devices:
        name=device['name']
        if system=='Linux':
            path=Path('/sys/class/block')/name
            if (path/'partition').exists() or name.startswith(('loop','ram','dm-','md')):continue
        included.append(device)
    def rate(key):
        valid=[d[key] for d in included if d[key] is not None]
        return sum(valid) if valid else None
    return dict(devices=devices,aggregateDevices=[d['name'] for d in included],readRate=rate('readRate'),writeRate=rate('writeRate'))
