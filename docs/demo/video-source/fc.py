import json, socket, sys
def call(method, params=None, port=7878, timeout=60):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall((json.dumps({"method": method, "params": params or {}, "id": 1}) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        c = s.recv(65536)
        if not c: break
        buf += c
    s.close()
    return json.loads(buf)
if __name__ == "__main__":
    m = sys.argv[1]; p = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(json.dumps(call(m, p), indent=1)[:3000])
