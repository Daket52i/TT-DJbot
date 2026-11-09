import re
import socket
import ssl
import threading
import time
import urllib.parse

# Название трека в ICY-блоке метаданных: StreamTitle='...';
_ICY_TITLE = re.compile(rb"StreamTitle='(.*?)';")
# Статус-строка ответа: "HTTP/1.1 200 OK" или "ICY 200 OK"
_STATUS = re.compile(rb"^\S+\s+(\d{3})")
_REDIRECT_CODES = {301, 302, 303, 307, 308}


class IcyMetadataReader:
    """Родной читатель ICY-метаданных (StreamTitle) прямо из потока.

    Подключается к радиостанции с заголовком ``Icy-MetaData: 1``, узнаёт из
    ответа ``icy-metaint`` и затем после каждых ``metaint`` байт аудио читает
    блок метаданных, где и лежит ``StreamTitle``. Тем же путём метаданные
    получает телефонный vRadio, поэтому способ не зависит от того, что именно
    ffmpeg напишет в stderr, и одинаково работает и для Icecast (наш dtr), и
    для волнореза.

    Экземпляр одноразовый: создаётся под конкретный URL и живёт до stop().
    При обрыве связи пытается переподключиться.
    """

    def __init__(self, url, on_metadata=None, on_name=None,
                 user_agent="tt5bot-icy/1.0", reconnect_delay=3.0):
        self.url = url
        self.on_metadata = on_metadata
        self.on_name = on_name
        self.user_agent = user_agent
        self.reconnect_delay = reconnect_delay
        self._stop = threading.Event()
        self._sock_lock = threading.Lock()
        self._sock = None
        self._thread = None
        self._last_title = None

    def start(self):
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
        while not self._stop.is_set():
            try:
                self._read_stream()
            except Exception as e:
                print(f"ICY reader error: {e}")
            if self._stop.is_set():
                break
            time.sleep(self.reconnect_delay)

    def _open(self):
        """Открывает соединение, разбирается с редиректами. Возвращает (sock, headers, leftover)."""
        url = self.url
        for _ in range(6):
            parts = urllib.parse.urlsplit(url)
            host = parts.hostname
            if not host:
                return None
            scheme = parts.scheme or "http"
            port = parts.port or (443 if scheme == "https" else 80)
            path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")

            sock = socket.create_connection((host, port), timeout=15)
            if scheme == "https":
                sock = self._wrap_tls(sock, host)
            sock.settimeout(15)

            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                f"User-Agent: {self.user_agent}\r\n"
                f"Icy-MetaData: 1\r\n"
                f"Accept: */*\r\n"
                f"Connection: close\r\n\r\n"
            )
            sock.sendall(request.encode("latin-1"))

            status, headers, leftover = self._read_headers(sock)
            if status is None:
                sock.close()
                return None
            if status in _REDIRECT_CODES and headers.get("location"):
                sock.close()
                url = urllib.parse.urljoin(url, headers["location"])
                continue

            with self._sock_lock:
                if self._stop.is_set():
                    sock.close()
                    return None
                self._sock = sock
            return sock, headers, leftover
        return None

    @staticmethod
    def _wrap_tls(sock, host):
        try:
            context = ssl.create_default_context()
            return context.wrap_socket(sock, server_hostname=host)
        except ssl.SSLError:
            context = ssl._create_unverified_context()
            return context.wrap_socket(sock, server_hostname=host)

    @staticmethod
    def _read_headers(sock):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return None, None, None
            buf += chunk
            if len(buf) > 65536:
                return None, None, None
        head, leftover = buf.split(b"\r\n\r\n", 1)
        lines = head.split(b"\r\n")
        match = _STATUS.match(lines[0])
        status = int(match.group(1)) if match else None
        headers = {}
        for raw in lines[1:]:
            if b":" in raw:
                key, value = raw.split(b":", 1)
                headers[key.strip().decode("latin-1").lower()] = value.strip().decode("latin-1")
        return status, headers, leftover

    def _fill(self, buf, need, sock):
        while len(buf) < need:
            if self._stop.is_set():
                return False
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return False
            if not chunk:
                return False
            buf += chunk
        return True

    def _read_stream(self):
        opened = self._open()
        if opened is None:
            return
        sock, headers, leftover = opened
        try:
            metaint = headers.get("icy-metaint", "")
            name = headers.get("icy-name", "")
            if name and self.on_name:
                try:
                    self.on_name(name)
                except Exception as e:
                    print(f"ICY name callback error: {e}")
            if not metaint.isdigit() or int(metaint) <= 0:
                # Поток без ICY-метаданных: держим соединение открытым, пока
                # нас не остановят, чтобы не долбить сервер переподключениями.
                self._drain(sock)
                return
            metaint = int(metaint)
            buf = bytearray(leftover)
            while not self._stop.is_set():
                if not self._fill(buf, metaint + 1, sock):
                    break
                meta_len = buf[metaint] * 16
                if meta_len:
                    if not self._fill(buf, metaint + 1 + meta_len, sock):
                        break
                    self._handle_block(bytes(buf[metaint + 1:metaint + 1 + meta_len]))
                del buf[:metaint + 1 + meta_len]
        finally:
            if self._sock is sock:
                with self._sock_lock:
                    self._sock = None
            try:
                sock.close()
            except OSError:
                pass

    def _drain(self, sock):
        while not self._stop.is_set():
            try:
                if not sock.recv(65536):
                    return
            except socket.timeout:
                continue
            except OSError:
                return

    def _handle_block(self, block):
        match = _ICY_TITLE.search(block)
        if not match:
            return
        title = match.group(1).decode("utf-8", "replace").strip("\x00").strip()
        if not title or title == self._last_title:
            return
        self._last_title = title
        if self.on_metadata:
            try:
                self.on_metadata(title)
            except Exception as e:
                print(f"ICY metadata callback error: {e}")
