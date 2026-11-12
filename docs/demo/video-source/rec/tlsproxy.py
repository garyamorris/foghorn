#!/usr/bin/env python3
"""Local-only HTTPS reverse proxy (self-signed cert) in front of the game/LangGraph server so that an https page
(LangGraph Studio) can reach it without mixed-content blocking.   usage: tlsproxy.py [listen_port=2028] [upstream_port=2027]"""
import http.client, http.server, ssl, sys, os, socketserver
LISTEN = int(sys.argv[1]) if len(sys.argv) > 1 else 2028
UP = int(sys.argv[2]) if len(sys.argv) > 2 else 2027
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailers', 'transfer-encoding', 'upgrade', 'content-length'}
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *a): pass
    def _go(self):
        try:
            n = int(self.headers.get('content-length') or 0)
            body = self.rfile.read(n) if n else None
            c = http.client.HTTPConnection('127.0.0.1', UP, timeout=3600)
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ('connection', 'host', 'keep-alive', 'transfer-encoding', 'content-length')}
            if body is not None: hdrs['content-length'] = str(len(body))
            c.request(self.command, self.path, body=body, headers=hdrs)
            r = c.getresponse()
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in HOP: self.send_header(k, v)
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            while True:
                b = r.read1(65536)
                if not b: break
                self.wfile.write(b'%x\r\n' % len(b) + b + b'\r\n'); self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n'); self.wfile.flush()
            c.close()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError): pass
        except Exception as e:
            try:
                self.send_response(502); self.send_header('content-length', '0'); self.end_headers()
            except Exception: pass
    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _go
class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True
d = os.path.dirname(os.path.abspath(__file__))
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(os.path.join(d, 'tls/cert.pem'), os.path.join(d, 'tls/key.pem'))
srv = S(('127.0.0.1', LISTEN), H); srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
print(f'tlsproxy https://127.0.0.1:{LISTEN} -> http://127.0.0.1:{UP}', flush=True)
srv.serve_forever()
