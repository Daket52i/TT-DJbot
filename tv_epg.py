"""TV EPG (XMLTV programme guide) + FFmpeg live MPEG-TS relay for TeamTalk TV bot."""
import gzip
import http.server
import os
import re
import socketserver
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

EPG_URLS = (
    "https://iptvx.one/epg/epg_lite.xml.gz",
    "https://epg.one/epg2.xml.gz",
    "http://epg.it999.ru/epg.xml.gz",
)

CHANNEL_ALIASES = {
    "первый канал": ("первый канал", "первый", "1tv", "channel one", "1 канал"),
    "россия 1": ("россия 1", "russia 1", "россия-1"),
    "матч тв": ("матч тв", "матч!", "матч", "match tv", "match! ultra", "матч! ультра"),
    "нтв": ("нтв", "ntv", "нтв hd", "ntv hd", "ntv russia"),
    "пятый канал": ("пятый канал", "5 канал", "channel 5", "channel 5 russia"),
    "россия к": ("россия к", "россия культура", "культура", "russia k"),
    "россия 24": ("россия 24", "russia 24"),
    "карусель": ("карусель", "carousel", "karusel"),
    "отр": ("отр", "otr"),
    "тв центр": ("тв центр", "твц", "tv centr", "tvc"),
    "рен тв": ("рен тв", "ren tv", "рен-тв"),
    "спас": ("спас", "spas"),
    "стс": ("стс", "sts", "ctc"),
    "домашний": ("домашний", "domashniy", "domashny"),
    "тв 3": ("тв 3", "тв-3", "тв3", "tv 3", "tv-3", "tv3"),
    "пятница": ("пятница", "пятница!", "friday", "friday!"),
    "звезда": ("звезда", "zvezda"),
    "мир": ("мир", "mir", "mir hd", "мир hd"),
    "мир 24": ("мир 24", "mir 24"),
    "тнт": ("тнт", "tnt"),
    "муз тв": ("муз тв", "муз-тв", "muz tv", "muz-tv"),
    "че": ("че", "че!", "che", "che!"),
    "2x2": ("2x2", "2х2"),
    "ю": ("ю", "u", "ю тв"),
    "эфир": ("эфир", "эфир тв", "татарстан 24"),
    "360": ("360", "360°", "360 новости", "360° news", "360 подмосковье"),
    "тнв татарстан": ("тнв татарстан", "тнв", "тнв планета"),
    "bridge tv": ("bridge", "bridge tv", "bridge classic"),
    "bridge rock": ("bridge rock",),
    "ностальгия": ("ностальгия", "nostalgia", "nostalgia russia"),
    "мир сериала": ("мир сериала", "mir seriala"),
    "1hd": ("1hd", "1hd music", "1hd music television"),
    "музыка первого": ("музыка первого", "muzyka pervogo"),
    "бобер": ("бобер", "бобёр", "bober"),
    "бст": ("бст", "bst"),
}


def norm_channel_name(name: str) -> str:
    s = (name or "").lower().replace("ё", "е")
    # Strip quality/region/time-shift tags like (1080p), (576i), (Казань), [Not 24/7], +4
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\+\d+\b", " ", s)
    s = re.sub(r"\b(hd|sd|fhd|uhd|4k|russia)\b", " ", s)
    s = re.sub(r"[^a-zа-я0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_xmltv_time(raw: str):
    if not raw:
        return None
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?:\s*([+-]\d{2})(\d{2}))?", raw.strip())
    if not m:
        return None
    y, mo, d, hh, mm, ss = (int(x) for x in m.groups()[:6])
    tz_h, tz_m = m.group(7), m.group(8)
    if tz_h and tz_m:
        sign = 1 if int(tz_h) >= 0 else -1
        offset = timezone(timedelta(hours=int(tz_h), minutes=sign * int(tz_m)))
    else:
        offset = timezone.utc
    try:
        return datetime(y, mo, d, hh, mm, ss, tzinfo=offset).timestamp()
    except ValueError:
        return None


class TvEpg:
    """Lazy-cached Russian XMLTV EPG guide."""

    def __init__(self, cache_dir: str, ttl_sec: int = 21600, log_fn=None):
        self.cache_path = os.path.join(cache_dir, "xmltv.xml.gz")
        self.ttl_sec = ttl_sec
        self._log = log_fn or (lambda msg: None)
        self._lock = threading.Lock()
        self._loading = False
        self._loaded_at = 0.0
        self._name_to_ids = {}   # norm_name -> list[channel_id]
        self._programmes = {}    # channel_id -> list[(start_ts, stop_ts, title)]

    def _is_cache_fresh(self) -> bool:
        try:
            st = os.stat(self.cache_path)
            return st.st_size > 50_000 and (time.time() - st.st_mtime) < self.ttl_sec
        except OSError:
            return False

    def ensure_loaded_async(self):
        with self._lock:
            if self._loading:
                return
            if self._programmes and (time.time() - self._loaded_at) < self.ttl_sec:
                return
            self._loading = True
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _download(self) -> bool:
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        tmp = self.cache_path + ".tmp"
        for url in EPG_URLS:
            try:
                self._log("epg downloading %s" % url)
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 SolnyshkoTT/1.0"})
                with urllib.request.urlopen(req, timeout=45) as resp, open(tmp, "wb") as out:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
                if os.path.getsize(tmp) > 50_000:
                    os.replace(tmp, self.cache_path)
                    self._log("epg downloaded %d bytes" % os.path.getsize(self.cache_path))
                    return True
            except Exception as e:
                self._log("epg download err (%s): %s" % (url, str(e)[:120]))
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return os.path.exists(self.cache_path) and os.path.getsize(self.cache_path) > 50_000

    def _refresh_worker(self):
        try:
            if not self._is_cache_fresh():
                if not self._download():
                    return
            self._parse_file()
        except Exception as e:
            self._log("epg refresh err: %s" % str(e)[:150])
        finally:
            with self._lock:
                self._loading = False

    def _parse_file(self):
        now = time.time()
        win_start = now - 3 * 3600
        win_end = now + 18 * 3600
        name_to_ids = {}
        programmes = {}
        ch_count = 0
        prog_count = 0
        with gzip.open(self.cache_path, "rb") as gz:
            for _ev, el in ET.iterparse(gz, events=("end",)):
                tag = el.tag
                if tag == "channel":
                    cid = el.attrib.get("id", "").strip()
                    if cid:
                        ch_count += 1
                        for dn in el.findall("display-name"):
                            k = norm_channel_name(dn.text or "")
                            if k:
                                name_to_ids.setdefault(k, []).append(cid)
                        k_id = norm_channel_name(cid)
                        if k_id:
                            name_to_ids.setdefault(k_id, []).append(cid)
                    el.clear()
                elif tag == "programme":
                    cid = el.attrib.get("channel", "").strip()
                    s_ts = _parse_xmltv_time(el.attrib.get("start", ""))
                    e_ts = _parse_xmltv_time(el.attrib.get("stop", ""))
                    if cid and s_ts and e_ts and e_ts >= win_start and s_ts <= win_end:
                        title = (el.findtext("title") or "").strip()
                        if title:
                            programmes.setdefault(cid, []).append((s_ts, e_ts, title))
                            prog_count += 1
                    el.clear()
        for plist in programmes.values():
            plist.sort(key=lambda x: x[0])
        with self._lock:
            self._name_to_ids = name_to_ids
            self._programmes = programmes
            self._loaded_at = time.time()
        self._log("epg loaded: %d channels, %d programmes in window" % (ch_count, prog_count))

    def _candidate_keys(self, title: str):
        base = norm_channel_name(title)
        if not base:
            return []
        keys = [base]
        for canon, aliases in CHANNEL_ALIASES.items():
            norm_aliases = [norm_channel_name(a) for a in aliases]
            if base == norm_channel_name(canon) or base in norm_aliases:
                keys.append(norm_channel_name(canon))
                keys.extend(norm_aliases)
                break
        # Dedup preserving order
        out = []
        for k in keys:
            if k and k not in out:
                out.append(k)
        return out

    def get_now(self, channel_title: str):
        """Return dict {title, start, stop, next_title, next_start} or None."""
        self.ensure_loaded_async()
        with self._lock:
            if not self._programmes:
                return None
            name_to_ids = self._name_to_ids
            programmes = self._programmes

        now = time.time()
        matched_ids = []
        for k in self._candidate_keys(channel_title):
            for cid in name_to_ids.get(k, ()):
                if cid not in matched_ids:
                    matched_ids.append(cid)
        # Fallback: substring match if exact alias missed
        if not matched_ids:
            base = norm_channel_name(channel_title)
            if len(base) >= 3:
                for k, cids in name_to_ids.items():
                    if base == k or (len(k) >= 3 and (base in k or k in base)):
                        for cid in cids:
                            if cid not in matched_ids:
                                matched_ids.append(cid)
                        if len(matched_ids) >= 5:
                            break

        for cid in matched_ids:
            plist = programmes.get(cid)
            if not plist:
                continue
            for idx, (s_ts, e_ts, ptitle) in enumerate(plist):
                if s_ts <= now < e_ts:
                    s_str = datetime.fromtimestamp(s_ts, MSK).strftime("%H:%M")
                    e_str = datetime.fromtimestamp(e_ts, MSK).strftime("%H:%M")
                    nxt_title = None
                    nxt_start = None
                    if idx + 1 < len(plist):
                        ns, _ne, nt = plist[idx + 1]
                        nxt_title = nt
                        nxt_start = datetime.fromtimestamp(ns, MSK).strftime("%H:%M")
                    return {
                        "title": ptitle,
                        "start": s_str,
                        "stop": e_str,
                        "next_title": nxt_title,
                        "next_start": nxt_start,
                    }
        return None


def probe_tv_url_blocked(url: str, timeout: float = 4.0) -> tuple[bool, str]:
    """Quick pre-flight check to reject Rostelecom geo-block stubs or 403/404 streams."""
    if not url or not url.startswith(("http://", "https://")):
        return True, "некорректная ссылка"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = (resp.geturl() or "").lower()
            if "rtk_block" in final_url or "zabava-block" in final_url:
                return True, "канал заблокирован по региону (геоблок)"
            head = resp.read(512)
            if b"#EXT-X-ENDLIST" in head and b"block" in head.lower():
                return True, "заглушка провайдера"
        return False, ""
    except urllib.error.HTTPError as e:
        return True, f"HTTP {e.code}"
    except Exception as e:
        return True, str(e)[:80]


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class TvStreamRelay:
    """Local FFmpeg -> HTTP MPEG-TS live relay for smooth, crash-free TeamTalk TV video.

    Pulls arbitrary HTTPS/HLS/redirecting streams via system ffmpeg, scales to 480p@25fps
    progressive H.264 + 48kHz stereo AAC, and serves a single clean local HTTP MPEG-TS
    stream to TeamTalk SDK's startStreamingMediaFileToChannelEx().
    """

    def __init__(self, source_url: str, log_fn=None):
        self.source_url = source_url
        self._log = log_fn or (lambda msg: None)
        self._lock = threading.Lock()
        self._stopped = False
        self._procs = []
        self._server = None
        self.local_url = None

    def start(self) -> str:
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):
                pass

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

            def do_GET(self):
                if relay._stopped:
                    self.send_error(410)
                    return
                cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5",
                    "-rw_timeout", "10000000",
                    "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "-fflags", "+genpts+discardcorrupt",
                    "-i", relay.source_url,
                    "-map", "0:v:0",
                    "-map", "0:a:0?",
                    "-vf", "scale=w=854:h=480:force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p",
                    "-r", "25",
                    "-g", "50",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-tune", "zerolatency",
                    "-b:v", "750k",
                    "-maxrate", "950k",
                    "-bufsize", "1500k",
                    "-c:a", "aac",
                    "-ar", "48000",
                    "-ac", "2",
                    "-b:a", "128k",
                    "-f", "mpegts",
                    "pipe:1",
                ]
                proc = None
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        bufsize=0,
                    )
                    with relay._lock:
                        if relay._stopped:
                            proc.kill()
                            return
                        relay._procs.append(proc)
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp2t")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while not relay._stopped:
                        chunk = proc.stdout.read(32768)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                except Exception as e:
                    relay._log("tv relay stream err: %s" % str(e)[:120])
                finally:
                    if proc is not None:
                        try:
                            proc.terminate()
                            proc.wait(timeout=2)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                        with relay._lock:
                            if proc in relay._procs:
                                relay._procs.remove(proc)

        self._server = _ThreadedHTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self.local_url = f"http://127.0.0.1:{port}/live.ts"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self._log("tv relay listening on %s -> %s" % (self.local_url, self.source_url[:80]))
        return self.local_url

    def stop(self):
        with self._lock:
            self._stopped = True
            procs = list(self._procs)
            self._procs.clear()
        for proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        for proc in procs:
            try:
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        srv = self._server
        self._server = None
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
