#!/usr/bin/env python3
"""Interactive SMTP setup; password never appears in arguments, output or repository."""
import argparse
import getpass
import time
from storage import read,write

if __name__=='__main__':
    p=argparse.ArgumentParser(description='配置本机告警邮箱（授权码通过隐藏输入填写）')
    p.add_argument('--host',default='smtp.163.com');p.add_argument('--port',type=int,default=465)
    p.add_argument('--encryption',choices=('ssl','starttls','none'),default='ssl')
    p.add_argument('--sender',required=True);p.add_argument('--to',required=True)
    p.add_argument('--username');p.add_argument('--hello',default='163.com');p.add_argument('--disable',action='store_true')
    args=p.parse_args()
    old=read('email.json',{})
    if args.disable:
        old['enabled']=False;write('email.json',old);print('邮件告警已禁用；不会清除已排队通知。')
    else:
        password=getpass.getpass('SMTP 授权码（不会显示）：')
        if not password:p.error('授权码不能为空')
        if not 1<=args.port<=65535:p.error('端口必须为 1–65535')
        cfg=dict(enabled=True,host=args.host,port=args.port,encryption=args.encryption,**{'from':args.sender},to=args.to,
            username=args.username or args.sender,password=password,hello=args.hello,timezone='Asia/Shanghai',enabledAt=old.get('enabledAt',time.time()))
        write('email.json',cfg);print('配置已保存。可在告警中心发送测试邮件；无需重启。')
