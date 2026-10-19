#!/usr/bin/env python3
"""
Android RTSP Camera Viewer
Custom RTP/JPEG stream decoder for Android
"""

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.image import Image
from kivy.clock import Clock
from kivy.graphics.texture import Texture

import io
import re
import socket
import struct
import time
import threading
from PIL import Image as PILImage

# Android permissions
try:
    from android.permissions import request_permissions, Permission
    request_permissions([Permission.INTERNET, Permission.ACCESS_WIFI_STATE, Permission.ACCESS_NETWORK_STATE])
except:
    pass

class RTSPClient:
    def __init__(self):
        self.HOST = "192.168.1.1"
        self.RPORT = 7070
        self.PATH = "/webcam"
        self.DRI = 40
        self.RTP_PORT = 50000
        
        self.s = None
        self.rtp = None
        self.rtcp = None
        self.session = None
        self.cseq = 0
        self.running = False
        self.frames = {}
        self.srv_rtcp = None
        self.timeout = 30
        self.last_keep = time.time()
        self.last_rr = time.time()
        
        self.DHT = self._std_dht()
    
    def _std_dht(self):
        b = io.BytesIO()
        PILImage.new("RGB", (16, 16)).save(b, "JPEG")
        d = b.getvalue()
        i, out = 2, b""
        while i < len(d):
            m = d[i+1]
            l = struct.unpack(">H", d[i+2:i+4])[0]
            if m == 0xC4:
                out += d[i:i+2+l]
            if m == 0xDA:
                break
            i += 2 + l
        return out
    
    def rtsp(self, method, url, extra=""):
        self.cseq += 1
        msg = "%s %s RTSP/1.0\r\nCSeq: %d\r\n" % (method, url, self.cseq)
        if self.session:
            msg += "Session: %s\r\n" % self.session
        self.s.sendall((msg + extra + "\r\n").encode())
        
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        head, body = buf.split(b"\r\n\r\n", 1)
        lines = head.decode(errors="replace").split("\r\n")
        hdrs = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() 
                for l in lines[1:] if ":" in l}
        n = int(hdrs.get("content-length", 0))
        while len(body) < n:
            body += self.s.recv(4096)
        return lines[0], hdrs, body.decode(errors="replace")
    
    def connect(self):
        URL = "rtsp://%s:%d%s" % (self.HOST, self.RPORT, self.PATH)
        
        self.s = socket.create_connection((self.HOST, self.RPORT), timeout=5)
        
        self.rtsp("OPTIONS", URL)
        st, h, sdp = self.rtsp("DESCRIBE", URL, "Accept: application/sdp\r\n")
        
        ctl = re.search(r"a=control:(\S+)", sdp)
        ctl = ctl.group(1) if ctl else ""
        if ctl.startswith("rtsp://"):
            track = ctl
        elif ctl in ("", "*"):
            track = URL
        else:
            track = URL.rstrip("/") + "/" + ctl
        
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        self.rtp.bind(("", self.RTP_PORT))
        self.rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtcp.bind(("", self.RTP_PORT + 1))
        
        st, h, _ = self.rtsp("SETUP", track, 
                           "Transport: RTP/AVP;unicast;client_port=%d-%d\r\n" % 
                           (self.RTP_PORT, self.RTP_PORT + 1))
        
        if " 200" not in st:
            raise Exception("SETUP failed")
        
        self.session = h.get("session", "").split(";")[0]
        m = re.search(r"timeout=(\d+)", h.get("session", ""))
        if m:
            self.timeout = int(m.group(1))
        m = re.search(r"server_port=(\d+)-(\d+)", h.get("transport", ""))
        self.srv_rtcp = (self.HOST, int(m.group(2))) if m else None
        
        self.rtsp("PLAY", URL, "Range: npt=0.000-\r\n")
        self.rtp.settimeout(1.0)
        self.running = True
    
    def build_jpeg(self, f):
        scan = b"".join(f["parts"][o] for o in sorted(f["parts"]))
        qt = f["qt"]
        out = b"\xff\xd8"
        for i in range(len(qt) // 64):
            out += b"\xff\xdb\x00\x43" + bytes([i]) + qt[64*i:64*i+64]
        hv = 0x21 if (f["typ"] & 0x3F) == 0 else 0x22
        c2 = 1 if len(qt) > 64 else 0
        out += b"\xff\xc0\x00\x11\x08" + struct.pack(">HH", f["h"], f["w"]) + \
               bytes([3, 1, hv, 0, 2, 0x11, c2, 3, 0x11, c2])
        out += b"\xff\xdd\x00\x04" + struct.pack(">H", self.DRI)
        out += self.DHT
        out += bytes([0xFF, 0xDA, 0, 12, 3, 1, 0, 2, 0x11, 3, 0x11, 0, 0x3F, 0])
        out += scan
        if not scan.endswith(b"\xff\xd9"):
            out += b"\xff\xd9"
        return out
    
    def is_contiguous(self, f):
        pos = 0
        for o in sorted(f["parts"]):
            if o != pos:
                return False
            pos += len(f["parts"][o])
        return 0 in f["parts"]
    
    def get_frame(self):
        if not self.running:
            return None
        
        now = time.time()
        
        if now - self.last_keep > max(self.timeout // 2, 5):
            try:
                self.rtsp("OPTIONS", "rtsp://%s:%d%s" % (self.HOST, self.RPORT, self.PATH))
            except:
                pass
            self.last_keep = now
        
        if self.srv_rtcp and now - self.last_rr > 2:
            self.rtcp.sendto(b"\x80\xc9\x00\x01\x12\x34\x56\x78", self.srv_rtcp)
            self.last_rr = now
        
        try:
            d = self.rtp.recv(65535)
        except socket.timeout:
            return None
        
        if len(d) < 20:
            return None
        
        hdr = 12 + 4 * (d[0] & 0x0F)
        if d[0] & 0x10:
            hdr += 4 + 4 * struct.unpack(">H", d[hdr+2:hdr+4])[0]
        ts = struct.unpack(">I", d[4:8])[0]
        marker = d[1] >> 7
        j = d[hdr:]
        off = int.from_bytes(j[1:4], "big")
        typ, q, w8, h8 = j[4], j[5], j[6], j[7]
        rest = j[8:]
        if typ >= 64:
            rest = rest[4:]
        
        f = self.frames.setdefault(ts, {"parts": {}, "typ": typ, "w": w8*8, "h": h8*8, "qt": b""})
        if q >= 128 and off == 0:
            qlen = struct.unpack(">H", rest[2:4])[0]
            f["qt"] = rest[4:4+qlen]
            rest = rest[4+qlen:]
        f["parts"][off] = rest
        
        if marker:
            if self.is_contiguous(f) and f["qt"]:
                try:
                    jpeg_data = self.build_jpeg(f)
                    img = PILImage.open(io.BytesIO(jpeg_data))
                    for k in [k for k in self.frames if k <= ts]:
                        del self.frames[k]
                    return img
                except:
                    pass
            for k in [k for k in self.frames if k <= ts]:
                del self.frames[k]
        
        if len(self.frames) > 50:
            self.frames.clear()
        
        return None
    
    def disconnect(self):
        self.running = False
        try:
            self.rtsp("TEARDOWN", "rtsp://%s:%d%s" % (self.HOST, self.RPORT, self.PATH))
        except:
            pass
        if self.s:
            self.s.close()
        if self.rtp:
            self.rtp.close()
        if self.rtcp:
            self.rtcp.close()


class CameraApp(App):
    def __init__(self):
        super().__init__()
        self.client = None
        self.streaming = False
        self.frame_event = None
        
    def build(self):
        self.layout = BoxLayout(orientation='vertical', padding=10, spacing=10)
        
        self.status_label = Label(
            text='Connect to camera WiFi\n192.168.1.1',
            size_hint=(1, 0.1),
            color=(1, 1, 1, 1)
        )
        
        self.image_widget = Image(size_hint=(1, 0.7))
        
        control_layout = BoxLayout(orientation='horizontal', size_hint=(1, 0.2), spacing=5)
        
        self.connect_btn = Button(text='Connect', on_press=self.toggle_stream)
        self.save_btn = Button(text='Save Frame', on_press=self.save_frame, disabled=True)
        
        control_layout.add_widget(self.connect_btn)
        control_layout.add_widget(self.save_btn)
        
        self.layout.add_widget(self.status_label)
        self.layout.add_widget(self.image_widget)
        self.layout.add_widget(control_layout)
        
        return self.layout
    
    def toggle_stream(self, instance):
        if not self.streaming:
            self.start_stream()
        else:
            self.stop_stream()
    
    def start_stream(self):
        self.status_label.text = "Connecting..."
        self.connect_btn.disabled = True
        
        def connect_thread():
            try:
                self.client = RTSPClient()
                self.client.connect()
                self.streaming = True
                Clock.schedule_once(lambda dt: self.on_connected(), 0)
            except Exception as e:
                Clock.schedule_once(lambda dt: self.on_error(str(e)), 0)
        
        threading.Thread(target=connect_thread, daemon=True).start()
    
    def on_connected(self):
        self.status_label.text = "Connected - Streaming"
        self.connect_btn.text = "Disconnect"
        self.connect_btn.disabled = False
        self.save_btn.disabled = False
        self.frame_event = Clock.schedule_interval(self.update_frame, 1/30.0)
    
    def on_error(self, error):
        self.status_label.text = f"Error: {error}"
        self.connect_btn.disabled = False
        self.streaming = False
    
    def update_frame(self, dt):
        if not self.streaming or not self.client:
            return
        
        frame = self.client.get_frame()
        if frame:
            frame_rgba = frame.convert('RGBA')
            texture = Texture.create(size=(frame.width, frame.height), colorfmt='rgba')
            texture.blit_buffer(frame_rgba.tobytes(), colorfmt='rgba', bufferfmt='ubyte')
            texture.flip_vertical()
            self.image_widget.texture = texture
    
    def stop_stream(self):
        self.streaming = False
        if self.frame_event:
            self.frame_event.cancel()
        if self.client:
            self.client.disconnect()
        self.status_label.text = "Disconnected"
        self.connect_btn.text = "Connect"
        self.save_btn.disabled = True
    
    def save_frame(self, instance):
        if self.image_widget.texture:
            filename = f"/sdcard/DCIM/cam_{int(time.time())}.png"
            try:
                self.image_widget.export_to_png(filename)
                self.status_label.text = f"Saved: {filename}"
            except:
                self.status_label.text = "Save failed"
            Clock.schedule_once(lambda dt: self.reset_status(), 2)
    
    def reset_status(self):
        if self.streaming:
            self.status_label.text = "Connected - Streaming"
    
    def on_stop(self):
        self.stop_stream()


if __name__ == '__main__':
    CameraApp().run()
