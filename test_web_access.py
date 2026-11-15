"""Web discovery/publication tests use only isolated temporary HTTP servers."""
import functools
import http.server
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import psutil
import storage
import web_access as web

class QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):self.send_response(200);self.end_headers()
    def do_GET(self):
        self.send_response(200);self.end_headers();self.wfile.write(b'web-access-test')
    def log_message(self,*args):pass

class WebAccess(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        patcher=patch.object(storage,'DATA',Path(self.tmp.name));patcher.start();self.addCleanup(patcher.stop)
        self.http=http.server.ThreadingHTTPServer(('127.0.0.1',0),QuietHandler)
        threading.Thread(target=self.http.serve_forever,kwargs={'poll_interval':.05},daemon=True).start()
        self.addCleanup(self.http.server_close);self.addCleanup(self.http.shutdown)
    def entry(self):
        return dict(key='test-web',pid=os.getpid(),created=psutil.Process().create_time(),name='temporary-test',
            address='127.0.0.1',probeHost='127.0.0.1',port=self.http.server_port,scheme='http',protocol='TCP')
    def test_protocol_detection_without_body_or_redirect(self):
        self.assertEqual(web.probe('127.0.0.1',self.http.server_port),'http')
    def test_non_http_socket_is_not_marked_web(self):
        class Banner(__import__('socketserver').BaseRequestHandler):
            def handle(self):self.request.sendall(b'SSH-2.0-test\r\n')
        server=__import__('socketserver').ThreadingTCPServer(('127.0.0.1',0),Banner)
        threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05},daemon=True).start()
        try:self.assertIsNone(web.probe('127.0.0.1',server.server_address[1]))
        finally:server.shutdown();server.server_close()
    def test_private_addresses_exclude_public_loopback_and_bridges(self):
        interfaces=[dict(name='en1',up=True,addresses=['192.168.2.13']),dict(name='bridge100',up=True,addresses=['192.168.64.1']),dict(name='lo',up=True,addresses=['127.0.0.1']),dict(name='eth1',up=True,addresses=['160.202.46.39'])]
        self.assertEqual(web.lan_addresses(interfaces),['192.168.2.13'])
    def test_web_command_lan_default_and_no_lan_fallback(self):
        body=dict(command=['/usr/bin/python3','-m','http.server','8000','--bind','127.0.0.1'],webAccess='lan')
        result=web.web_command(body,['192.168.2.13']);self.assertEqual(result['command'][-2:],['--bind','192.168.2.13'])
        self.assertEqual(result['command'].count('--bind'),1)
        self.assertEqual(web.web_command(body,[])['webBind'],'127.0.0.1')
    def test_next_vite_flags_and_generic_not_rewritten(self):
        for command,flag in [(['/usr/bin/node','/app/vite/bin/vite.js','--host','--port','5173'],'--host'),(['/usr/bin/node','/app/next/dist/bin/next','dev'],'--hostname')]:
            output=web.web_command(dict(command=command,webAccess='lan'),['10.0.0.2'])['command']
            self.assertEqual(output[-2:],[flag,'10.0.0.2'])
            if flag=='--host':self.assertIn('--port',output)
        body=dict(command=['/usr/bin/other','run']);self.assertEqual(web.web_command(body,['10.0.0.2']),body)
        with self.assertRaises(ValueError):web.web_command(dict(body,webAccess='lan'),['10.0.0.2'])
    def test_url_settings_reject_executable_schemes_and_credentials(self):
        for url in ('javascript:alert(1)','file:///etc/passwd','https://user:pass@example.com','http://example.com:99999','http://host/\n'):
            with self.assertRaises(ValueError):web.validate_url(url)
        self.assertEqual(web.validate_url('https://example.com/app/#home'),'https://example.com/app/#home')
    def test_publication_transfers_http_and_closes_port(self):
        entry=self.entry();registry=web.Publications();self.addCleanup(registry.close)
        # Loopback stands in for a private interface so CI never opens a real LAN socket.
        result=registry.configure(dict(key=entry['key'],enabled=True,port=0),[entry],['127.0.0.1'],set())
        with socket.create_connection(('127.0.0.1',result['port']),timeout=2) as s:
            s.sendall(b'GET / HTTP/1.0\r\n\r\n');raw=b''
            while True:
                chunk=s.recv(4096)
                if not chunk:break
                raw+=chunk
        self.assertIn(b'web-access-test',raw)
        registry.configure(dict(key=entry['key'],enabled=False),[entry],['127.0.0.1'],set())
        with self.assertRaises(OSError):socket.create_connection(('127.0.0.1',result['port']),timeout=.2)
    def test_target_identity_change_stops_publication(self):
        entry=self.entry();registry=web.Publications();self.addCleanup(registry.close)
        registry.configure(dict(key=entry['key'],enabled=True,port=0),[entry],['127.0.0.1'],set())
        changed=dict(entry,created=entry['created']+1)
        self.assertFalse(registry.reconcile([changed],['127.0.0.1'],set()))
        self.assertFalse(registry.active);self.assertIn('更换',registry.errors[entry['key']])
    def test_already_network_bound_and_unknown_owner_cannot_be_republished(self):
        registry=web.Publications();self.addCleanup(registry.close)
        for entry in [dict(self.entry(),address='0.0.0.0'),dict(self.entry(),pid=None)]:
            with self.assertRaises(ValueError):registry.configure(dict(key=entry['key'],enabled=True,port=0),[entry],['10.0.0.2'],set())
    def test_default_database_ports_and_monitor_process_not_probed(self):
        discovery=web.Discovery();self.addCleanup(lambda:discovery.pool.shutdown(wait=True))
        endpoints=[dict(protocol='TCP',port=port,pid=os.getpid(),name='python',address='127.0.0.1') for port in (8000,18767,3306,6379)]
        with patch.object(discovery.pool,'submit') as submit:
            self.assertEqual(discovery.update(endpoints,{},set()),[]);submit.assert_not_called()
    def test_local_services_header_is_not_a_publishable_web_target(self):
        class Marker(QuietHandler):
            def do_HEAD(self):self.send_response(405);self.send_header('X-Local-Services','monitor');self.end_headers()
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Marker)
        threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05},daemon=True).start()
        try:self.assertIsNone(web.probe('127.0.0.1',server.server_port))
        finally:server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()
