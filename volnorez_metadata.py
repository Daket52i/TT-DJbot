import base64
import json
import os
import re
import socket
import ssl
import struct
import threading
import time
import urllib.parse

# Ссылка на живой поток волнореза: https://stream.volnorez.com/live-app/<id>
_LIVE_APP = re.compile(r"/live-app/(\d+)")
# Шарды волнореза. Оба живые и умеют переадресовывать друг на друга
# сообщением {"cmd":"redirect","host":"..."}.
_SHARDS = ["s05.volnorez.com", "s04.volnorez.com"]
_WS_PATH = "/live-app/%s"
_WS_PORT = 443
_MAX_HOPS = 5


class VolnorezMetadataReader:
    """Читает «сейчас играет» волнореза по WebSocket — как сайт и vRadio.

    Волнорез НЕ кладёт трек в ICY-метаданные: всё, что видно на сайте
    (диджей, трек, слушатели), приходит по WebSocket ``wss://s0X.volnorez.com/
    live-app/<id>`` JSON-сообщениями. Клиент подключается, шлёт
    ``{"cmd":"add_player",...}`` и получает:

    * ``{"cmd":"set_desc","desc":{...}}`` — ``status``, ``rjs`` (диджеи),
      ``music`` (список треков с ``title`` и ``rj``);
    * ``{"cmd":"counter","val":N}`` — сколько слушают;
    * ``{"cmd":"redirect","host":"s0X.volnorez.com"}`` — перейти на другой шард.

    Экземпляр одноразовый, как и ICY-читатель: создаётся под конкретный
    поток и живёт до stop(). Сам ходит по редиректам и переподключается.
    """

    def __init__(self, stream_url, on_metadata=None, on_listeners=None,
                 reconnect_delay=5.0):
        match = _LIVE_APP.search(urllib.parse.urlsplit(stream_url).path)
        self.station_id = match.group(1) if match else None
        self.path = _WS_PATH % self.station_id if self.station_id else ""
        self.on_metadata = on_metadata
        self.on_listeners = on_listeners
        self.reconnect_delay = reconnect_delay
        self._stop = threading.Event()
        self._sock_lock = threading.Lock()
        self._sock = None
        self._thread = None
        self._last_text = None

    @classmethod
    def supports(cls, url):
        """Подходит ли поток волнореза (по ссылке вида .../live-app/<id>)."""
        parts = urllib.parse.urlsplit(url)
        if "volnorez.com" not in (parts.hostname or ""):
            return False
        return _LIVE_APP.search(parts.path) is not None

    def start(self):
        if self.station_id is None:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._close_socket()

    def _close_socket(self):
        with self._sock_lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _run(self):
        candidates = list(_SHARDS)
        while not self._stop.is_set():
            host = candidates.pop(0)
            candidates.append(host)
            try:
                redirect = self._session(host)
            except Exception as e:
                print(f"Volnorez reader error ({host}): {e}")
                redirect = None
            if self._stop.is_set():
                break
            if redirect:
                # Сервер сам сказал, где жить этой станции.
                candidates = [redirect] + [h for h in candidates if h != redirect]
                continue
            time.sleep(self.reconnect_delay)

    def _session(self, host):
        """Одна сессия с хостом. Возвращает новый хост при redirect, иначе None."""
        sock = self._connect(host)
        if sock is None:
            return None
        reader = self
        try:
            self._send_text(sock, {"cmd": "add_player", "paused": False, "type": "stream"})
            while not self._stop.is_set():
                opcode, payload = self._read_frame(sock)
                if opcode is None:
                    return None
                if opcode == 0x1:  # text
                    redirect = reader._handle_message(payload)
                    if redirect:
                        return redirect
                elif opcode == 0x9:  # ping -> pong
                    self._send_frame(sock, 0x0A, payload)
                elif opcode == 0x8:  # close
                    return None
                # бинарные кадры — это аудио, нам не нужны
        finally:
            if self._sock is sock:
                with self._sock_lock:
                    self._sock = None
            try:
                sock.close()
            except OSError:
                pass
        return None

    def _connect(self, host):
        url = f"wss://{host}:{_WS_PORT}{self.path}"
        parts = urllib.parse.urlsplit(url)
        try:
            raw = socket.create_connection((parts.hostname, parts.port), timeout=15)
        except OSError:
            return None
        try:
            sock = self._wrap_tls(raw, parts.hostname)
        except Exception:
            try:
                raw.close()
            except OSError:
                pass
            return None
        sock.settimeout(30)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: https://volnorez.com\r\n\r\n"
        )
        sock.sendall(request.encode("latin-1"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                return None
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0]
        if b" 101 " not in head.split(b"\r\n")[0] + b" ":
            sock.close()
            return None
        with self._sock_lock:
            if self._stop.is_set():
                sock.close()
                return None
            self._sock = sock
        return sock

    @staticmethod
    def _wrap_tls(raw, host):
        try:
            return ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        except ssl.SSLError:
            return ssl._create_unverified_context().wrap_socket(raw, server_hostname=host)

    def _read_frame(self, sock):
        header = self._recv_exact(sock, 2)
        if header is None:
            return None, None
        b0, b1 = header[0], header[1]
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        length = b1 & 0x7F
        if length == 126:
            ext = self._recv_exact(sock, 2)
            if ext is None:
                return None, None
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = self._recv_exact(sock, 8)
            if ext is None:
                return None, None
            length = struct.unpack(">Q", ext)[0]
        mask = None
        if masked:
            mask = self._recv_exact(sock, 4)
            if mask is None:
                return None, None
        payload = self._recv_exact(sock, length) if length else b""
        if payload is None:
            return None, None
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    @staticmethod
    def _recv_exact(sock, n):
        buf = b""
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except socket.timeout:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    @staticmethod
    def _send_frame(sock, opcode, payload):
        header = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        sock.sendall(bytes(header) + masked)

    def _send_text(self, sock, obj):
        self._send_frame(sock, 0x1, json.dumps(obj).encode("utf-8"))

    def _handle_message(self, payload):
        try:
            message = json.loads(payload.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(message, dict):
            return None
        cmd = message.get("cmd")
        if cmd == "redirect":
            host = str(message.get("host") or "").strip()
            return host or None
        if cmd == "set_desc":
            self._on_description(message.get("desc") or {})
        elif cmd == "counter":
            if self.on_listeners:
                try:
                    self.on_listeners(message.get("val"))
                except Exception as e:
                    print(f"Volnorez listeners callback error: {e}")
        return None

    def _on_description(self, desc):
        if not isinstance(desc, dict):
            return
        text = self._format(desc)
        if text == self._last_text:
            return
        self._last_text = text
        if self.on_metadata:
            try:
                self.on_metadata(text)
            except Exception as e:
                print(f"Volnorez metadata callback error: {e}")

    @staticmethod
    def _format(desc):
        """Собирает строку «диджей — трек» из описания эфира волнореза."""
        if desc.get("status") not in (None, "online"):
            return ""
        music = desc.get("music") or []
        rjs = desc.get("rjs") or []
        track = ""
        dj = ""
        if music and isinstance(music[0], dict):
            track = str(music[0].get("title") or "").strip()
            dj = str(music[0].get("rj") or "").strip()
        if not dj and rjs:
            dj = ", ".join(str(x).strip() for x in rjs if str(x).strip())
        if dj and track:
            return f"{dj} — {track}"
        return track or dj
