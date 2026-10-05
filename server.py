#!/usr/bin/env python3
"""
server.py: static file server for the BHTP/1 binary protocol

Each client connection gets its own thread and stays open until the client
sends GOAWAY, disconnects, or goes quiet for IDLE_TIMEOUT seconds.
"""
import datetime
import hashlib
import mimetypes
import os
import socket
import struct
import sys
import threading

# protocol constants
PREFACE = b"BHTP\x01\x00\r\n"   
HDR_LEN = 8

T_REQUEST, T_RESPONSE, T_DATA, T_GOAWAY = 0x01, 0x02, 0x03, 0x07
F_END_STREAM, F_REUSE_HEADERS = 0x01, 0x02

METHODS = {1: "GET", 2: "HEAD", 3: "POST", 4: "PUT", 5: "DELETE", 6: "OPTIONS"}

STATIC = ["host", "user-agent", "accept", "if-none-match", "content-type",
          "content-length", "last-modified", "etag", "cache-control", "date"]
STATIC_INDEX = {name: i + 1 for i, name in enumerate(STATIC)}

MAX_REQUEST_PAYLOAD = 65535
DATA_CHUNK = 16384     # max body bytes per DATA frame
IDLE_TIMEOUT = 30      # seconds

VERBOSE = False
_print_lock = threading.Lock()


class Malformed(Exception):
    pass


# framing
def pack_frame(ftype, flags, rid, payload=b""):
    n = len(payload)
    if n > 0xFFFFFF:
        raise ValueError("payload too large for one frame")
    return struct.pack("!II", (n << 8) | ftype, (flags << 24) | rid) + payload


def unpack_header(h):
    a, b = struct.unpack("!II", h)
    return a >> 8, a & 0xFF, b >> 24, b & 0xFFFFFF


def recv_exact(sock, n):
    # recv() can return fewer bytes than asked, so loop.
    # Returns None if the peer closed cleanly before sending anything.
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 65536))
        if not chunk:
            if not buf:
                return None
            raise ConnectionError("peer closed mid-frame")
        buf += chunk
    return bytes(buf)


def discard(sock, n):
    while n > 0:
        chunk = sock.recv(min(n, 65536))
        if not chunk:
            raise ConnectionError("peer closed mid-frame")
        n -= len(chunk)


def hexdump(addr, direction, label, data):
    if not VERBOSE:
        return
    lines = [f"[{addr[0]}:{addr[1]}] {direction} {label}"]
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        hx = " ".join(f"{b:02x}" for b in row[:8]) + "  " + " ".join(f"{b:02x}" for b in row[8:])
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"    {off:08x}  {hx:<48}  |{asc}|")
    with _print_lock:
        print("\n".join(lines), file=sys.stderr, flush=True)


def frame_label(h):
    length, ftype, flags, rid = unpack_header(h)
    name = {T_REQUEST: "REQUEST", T_RESPONSE: "RESPONSE", T_DATA: "DATA",
            T_GOAWAY: "GOAWAY"}.get(ftype, f"UNKNOWN(0x{ftype:02x})")
    return f"{name} id={rid} len={length} flags=0x{flags:02x}"


# header encoding
def encode_headers(headers):
    out = bytearray([len(headers)])
    for name, value in headers:
        v = value.encode() if isinstance(value, str) else value
        idx = STATIC_INDEX.get(name)
        if idx:
            out.append(idx)
        else:
            n = name.encode("ascii")
            out += bytes([0x00, len(n)]) + n
        out += struct.pack("!H", len(v)) + v
    return bytes(out)


class Reader:
    # reads fields from a payload, raising Malformed instead of running off the end
    def __init__(self, data):
        self.d, self.i = data, 0

    def take(self, n):
        if self.i + n > len(self.d):
            raise Malformed("field runs past end of frame")
        s = self.d[self.i:self.i + n]
        self.i += n
        return s

    def u8(self):
        return self.take(1)[0]

    def u16(self):
        return struct.unpack("!H", self.take(2))[0]


def decode_headers(r):
    headers = []
    for _ in range(r.u8()):
        tag = r.u8()
        if 1 <= tag <= len(STATIC):
            name = STATIC[tag - 1]
        elif tag == 0:
            nlen = r.u8()
            if nlen == 0:
                raise Malformed("empty literal header name")
            try:
                name = r.take(nlen).decode("ascii").lower()
            except UnicodeDecodeError:
                raise Malformed("header name is not ASCII")
        else:
            raise Malformed(f"unknown static header index {tag}")
        headers.append((name, r.take(r.u16())))
    return headers


def parse_request(payload):
    r = Reader(payload)
    method = r.u8()
    try:
        path = r.take(r.u16()).decode("utf-8")
    except UnicodeDecodeError:
        raise Malformed("path is not UTF-8")
    if not path.startswith("/"):
        raise Malformed("path must start with '/'")
    headers = decode_headers(r)
    if r.i != len(payload):
        raise Malformed("trailing bytes after header block")
    return method, path, headers


# server
def http_date(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) \
        .strftime("%a, %d %b %Y %H:%M:%S GMT")


def log(addr, msg):
    print(f"[{addr[0]}:{addr[1]}] {msg}", file=sys.stderr, flush=True)


class Connection:
    def __init__(self, sock, addr, root):
        self.sock, self.addr, self.root = sock, addr, root
        self.last_id = 0
        self.prev_headers = {}

    def send(self, ftype, flags, rid, payload=b""):
        frame = pack_frame(ftype, flags, rid, payload)
        hexdump(self.addr, ">", frame_label(frame[:HDR_LEN]), frame)
        self.sock.sendall(frame)

    def goaway(self, code, reason):
        payload = struct.pack("!I", self.last_id)[1:] + struct.pack("!H", code) + reason.encode()
        try:
            self.send(T_GOAWAY, 0, 0, payload)
        except OSError:
            pass
        log(self.addr, f"GOAWAY {code} {reason}")
        if code:
            # closing on an error: drain unread input first, otherwise the OS
            # sends RST and the client may never see the GOAWAY
            try:
                self.sock.shutdown(socket.SHUT_WR)
                self.sock.settimeout(0.5)
                while self.sock.recv(65536):
                    pass
            except OSError:
                pass

    def respond(self, rid, status, headers, body=b"", head_only=False, path=None):
        headers = [("date", http_date(datetime.datetime.now().timestamp())),
                   ("server", "server/1.0")] + headers
        no_body = head_only or (path is None and not body) or \
            (path is not None and os.path.getsize(path) == 0)
        payload = struct.pack("!H", status) + encode_headers(headers)
        self.send(T_RESPONSE, F_END_STREAM if no_body else 0, rid, payload)
        if no_body:
            return
        if path is None:
            self.send(T_DATA, F_END_STREAM, rid, body)
            return
        with open(path, "rb") as f:
            chunk = f.read(DATA_CHUNK)
            while chunk:
                nxt = f.read(DATA_CHUNK)
                self.send(T_DATA, 0 if nxt else F_END_STREAM, rid, chunk)
                chunk = nxt

    def error(self, rid, status, text):
        body = (text + "\n").encode()
        self.respond(rid, status, [("content-type", "text/plain; charset=utf-8"),
                                   ("content-length", str(len(body)))], body)

    def resolve(self, url_path):
        # don't allow ../ to escape the root
        rel = url_path.split("?", 1)[0].lstrip("/")
        full = os.path.realpath(os.path.join(self.root, rel))
        if full != self.root and not full.startswith(self.root + os.sep):
            return None
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        return full if os.path.isfile(full) else None

    def handle_request(self, flags, rid, payload):
        try:
            method, path, headers = parse_request(payload)
        except Malformed as e:
            self.prev_headers = {}
            log(self.addr, f"#{rid} 400 {e}")
            self.error(rid, 400, f"400 Bad Request: {e}")
            return
        merged = dict(self.prev_headers) if flags & F_REUSE_HEADERS else {}
        merged.update({k: v for k, v in headers})
        self.prev_headers = merged

        name = METHODS.get(method, f"method-{method}")
        if method not in (1, 2):
            log(self.addr, f"#{rid} {name} {path} -> 405")
            self.error(rid, 405, "405 Method Not Allowed: only GET and HEAD")
            return
        full = self.resolve(path)
        if full is None:
            log(self.addr, f"#{rid} {name} {path} -> 404")
            self.error(rid, 404, f"404 Not Found: {path}")
            return

        st = os.stat(full)
        etag = '"' + hashlib.sha1(f"{st.st_size}-{st.st_mtime_ns}".encode()).hexdigest()[:16] + '"'
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        common = [("etag", etag), ("last-modified", http_date(st.st_mtime)),
                  ("cache-control", "max-age=60")]
        if merged.get("if-none-match", b"").decode(errors="replace") == etag:
            log(self.addr, f"#{rid} {name} {path} -> 304")
            self.respond(rid, 304, common)
            return
        log(self.addr, f"#{rid} {name} {path} -> 200 ({st.st_size} bytes)")
        self.respond(rid, 200, [("content-type", ctype),
                                ("content-length", str(st.st_size))] + common,
                     head_only=(method == 2), path=full)

    def serve(self):
        self.sock.settimeout(IDLE_TIMEOUT)
        try:
            pre = recv_exact(self.sock, len(PREFACE))
            if pre is None:
                return
            hexdump(self.addr, "<", "PREFACE", pre)
            if pre[:4] != PREFACE[:4] or pre[6:] != PREFACE[6:]:
                self.goaway(400, "bad connection preface (not BHTP)")
                return
            if pre[4] != PREFACE[4]:
                self.goaway(505, f"unsupported BHTP major version {pre[4]}")
                return
            self.sock.sendall(PREFACE)
            hexdump(self.addr, ">", "PREFACE", PREFACE)
            log(self.addr, "connected")

            while True:
                h = recv_exact(self.sock, HDR_LEN)
                if h is None:
                    log(self.addr, "client closed")
                    return
                length, ftype, flags, rid = unpack_header(h)
                if ftype != T_REQUEST or length > MAX_REQUEST_PAYLOAD:
                    hexdump(self.addr, "<", frame_label(h) + " (payload skipped)", h)

                if ftype == T_REQUEST:
                    if rid == 0 or rid <= self.last_id:
                        discard(self.sock, length)
                        self.goaway(400, f"request id {rid} is 0 or not increasing")
                        return
                    self.last_id = rid
                    if length > MAX_REQUEST_PAYLOAD:
                        discard(self.sock, length)
                        self.error(rid, 400, "400 Bad Request: REQUEST frame too large")
                        continue
                    payload = recv_exact(self.sock, length) if length else b""
                    hexdump(self.addr, "<", frame_label(h), h + payload)
                    self.handle_request(flags, rid, payload)
                elif ftype == T_GOAWAY:
                    discard(self.sock, length)
                    log(self.addr, "client sent GOAWAY")
                    return
                else:
                    # unknown frame types are skipped, not treated as errors
                    discard(self.sock, length)
                    if ftype not in (T_DATA, T_RESPONSE):
                        log(self.addr, f"skipped unknown frame type 0x{ftype:02x} ({length} bytes)")
        except socket.timeout:
            self.goaway(408, "idle timeout")
        except (ConnectionError, OSError) as e:
            log(self.addr, f"connection dropped: {e}")
        finally:
            self.sock.close()


def main():
    global VERBOSE
    args = sys.argv[1:]
    if args and args[0] == "-v":
        VERBOSE, args = True, args[1:]
    if len(args) != 2:
        print("usage: server.py [-v] <root-dir> <port>", file=sys.stderr)
        sys.exit(1)
    root = os.path.realpath(args[0])
    if not os.path.isdir(root):
        print(f"server: {root} is not a directory", file=sys.stderr)
        sys.exit(1)
    port = int(args[1])

    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("0.0.0.0", port))
    ls.listen(128)
    print(f"server: serving {root} on port {port} (BHTP/1)", file=sys.stderr, flush=True)
    try:
        while True:
            sock, addr = ls.accept()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=Connection(sock, addr, root).serve, daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        ls.close()


if __name__ == "__main__":
    main()
