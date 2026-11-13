"""E2E: เว็บจริงบน :3000 -> remote_agent (client) -> ท่อ 65434 -> remote_proxy (admin)"""
import sys, time, threading, socket, logging
logging.basicConfig(level=logging.INFO, format="   [client] %(message)s")
sys.path.insert(0, "/Users/puriphat.bu/Documents/ai-camera-remote-test/admin/ai-camera-admin/backend")
sys.path.insert(0, "/Users/puriphat.bu/Documents/ai-camera-remote-test/client")

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Web(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
            self.end_headers()
            i = 0
            try:
                while True:                      # สตรีมไม่มีวันจบเอง
                    self.wfile.write(b"--f\r\nframe%d\r\n" % i); self.wfile.flush()
                    i += 1; time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                print(f"   [web :3000] ฝั่ง client ตัดการดูดสตรีมที่ frame {i}")
        elif self.path == "/store_info":
            page = b"<html><body><h1>store info</h1></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Set-Cookie", "sess=abc; Path=/")
            self.end_headers(); self.wfile.write(page)
        else:
            self.send_response(404); self.end_headers()
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        got = self.rfile.read(n)
        self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
        self.wfile.write(b'{"echo":"%s"}' % got)

web = ThreadingHTTPServer(("127.0.0.1", 3000), Web)
threading.Thread(target=web.serve_forever, daemon=True).start()

import remote_proxy as rp
import remote_agent as ra
threading.Thread(target=rp.start_remote_listener, daemon=True).start()
time.sleep(0.4)

CID = "SHOP-001"
info = rp.mint_token(CID)                 # แอดมินออก token
print("1) แอดมินออก token:", {k: v for k, v in info.items() if k != "token"})

ra.on_open_remote(info, CID)              # frontend emit ให้ client -> client หมุนกลับ
time.sleep(0.6)
print("2) เซสชันขึ้นฝั่งแอดมิน:", rp.session_status(CID))

st, hdr, body = rp.open_request(CID, "GET", "/store_info", {})
print("3) GET /store_info ->", st, dict(hdr), b"".join(body))

st, hdr, body = rp.open_request(CID, "POST", "/echo", {"Content-Type": "text/plain"}, b"hello")
print("4) POST มี body ->", st, b"".join(body))

st, hdr, body = rp.open_request(CID, "GET", "/stream", {})
print("5) MJPEG head มาก่อน body จบ ->", st, hdr.get("Content-Type"))
got = [next(body) for _ in range(5)]
print("   5 chunk แรก:", b"".join(got)[:60])
body.close()                              # ผู้ชมปิด iframe
time.sleep(0.4)
print("6) หลังปิด: streams =", rp.session_status(CID)["streams"])

st, hdr, body = rp.open_request(CID, "GET", "/ไม่มีหน้านี้", {})
print("7) 404 ส่งผ่านเป็น status ปกติ ->", st)

# กัน SSRF: path ที่ชี้ไปโฮสต์อื่น
try:
    st, hdr, body = rp.open_request(CID, "GET", "//evil.example.com/", {})
    print("8) FAIL: ควรถูกปฏิเสธ ->", st)
except rp.RemoteError as e:
    print("8) path ชี้ออกนอกเครื่องถูกปฏิเสธ:", e)

ra.close_remote()
time.sleep(0.3)
print("9) client ตัดสาย -> แอดมินเห็นว่า:", rp.session_status(CID))
