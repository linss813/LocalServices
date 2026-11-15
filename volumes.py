"""Deduplicate filesystem aliases and APFS shared pools; immutable images are excluded."""
import re

def summarize(disks):
    groups={}
    for disk in disks:
        apfs=re.match(r'/dev/(disk\d+)s\d+',disk['device']) if disk['fstype'].lower()=='apfs' else None
        key='apfs:'+apfs[1] if apfs else 'fs:'+str(disk.get('fsId') or disk['device'])
        group=groups.setdefault(key,dict(id=key,records=[],sharedPool=bool(apfs)))
        group['records'].append(disk)
    volumes=[]
    for group in groups.values():
        records=group.pop('records')
        primary=min(records,key=lambda d:(0 if d['mount']=='/' else 1 if d['mount'].endswith('/Volumes/Data') else 2,len(d['mount'])))
        total=max(d['total'] for d in records);free=min(d['free'] for d in records)
        used=max(0,total-free)
        volumes.append(dict(group,device=primary['device'],fstype=primary['fstype'],mount=primary['mount'],
            mounts=sorted({d['mount'] for d in records}),total=total,used=used,free=free,
            reserved=max(0,total-free-max(d['used'] for d in records)),percent=round(used/total*100,1) if total else 0,
            alertable=any(d.get('alertable',True) for d in records),readOnly=all(d.get('readOnly',False) for d in records)))
    writable=[v for v in volumes if v['alertable']]
    total=sum(v['total'] for v in writable);free=sum(v['free'] for v in writable)
    return dict(total=total,used=total-free,free=free,percent=round((total-free)/total*100,1) if total else None,
        volumeCount=len(writable),mountCount=len(disks),excludedCount=len(volumes)-len(writable),volumes=volumes,
        sharedPools=any(v['sharedPool'] for v in writable),reserved=sum(v['reserved'] for v in writable))
