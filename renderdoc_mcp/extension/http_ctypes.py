"""Minimal HTTP/1.1 client built on ws2_32 via ctypes.

RenderDoc bundles a cut-down Python 3.6 with **no `_socket` module**, so the
standard library `http`/`socket` cannot be used. However `_ctypes.pyd` *is*
shipped, so we talk to Winsock (ws2_32.dll) directly. This lets the extension
connect *directly* to a local server (e.g. CodeBuddy's `--serve` HTTP endpoint)
with no external helper process.

Scope: blocking client for localhost (IPv4) only. Supports GET/POST, reads the
whole response until the server closes the connection (we send
`Connection: close`), and de-chunks `Transfer-Encoding: chunked` bodies. Good
enough for REST + Server-Sent-Events.
"""

import ctypes

AF_INET = 2
SOCK_STREAM = 1
IPPROTO_TCP = 6
SOL_SOCKET = 0xFFFF
SO_RCVTIMEO = 0x1006
SO_SNDTIMEO = 0x1005

SOCKET = ctypes.c_size_t          # UINT_PTR
INVALID_SOCKET = SOCKET(~0).value
SOCKET_ERROR = -1

_ws2 = ctypes.WinDLL("ws2_32.dll")


class _sockaddr_in(ctypes.Structure):
    _fields_ = [
        ("sin_family", ctypes.c_short),
        ("sin_port", ctypes.c_ushort),
        ("sin_addr", ctypes.c_uint32),
        ("sin_zero", ctypes.c_char * 8),
    ]


_ws2.WSAStartup.argtypes = [ctypes.c_ushort, ctypes.c_void_p]
_ws2.WSAStartup.restype = ctypes.c_int
_ws2.WSACleanup.restype = ctypes.c_int
_ws2.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
_ws2.socket.restype = SOCKET
_ws2.connect.argtypes = [SOCKET, ctypes.c_void_p, ctypes.c_int]
_ws2.connect.restype = ctypes.c_int
_ws2.send.argtypes = [SOCKET, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
_ws2.send.restype = ctypes.c_int
_ws2.recv.argtypes = [SOCKET, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
_ws2.recv.restype = ctypes.c_int
_ws2.closesocket.argtypes = [SOCKET]
_ws2.closesocket.restype = ctypes.c_int
_ws2.setsockopt.argtypes = [SOCKET, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
_ws2.setsockopt.restype = ctypes.c_int
_ws2.inet_addr.argtypes = [ctypes.c_char_p]
_ws2.inet_addr.restype = ctypes.c_uint32

_started = False


def _ensure_started():
    global _started
    if not _started:
        wsadata = ctypes.create_string_buffer(512)
        rc = _ws2.WSAStartup(0x0202, wsadata)
        if rc != 0:
            raise OSError("WSAStartup failed: %d" % rc)
        _started = True


def _htons(port):
    return ((port & 0xFF) << 8) | ((port >> 8) & 0xFF)


class HttpError(Exception):
    pass


class CancelToken(object):
    """Allows another thread to abort an in-flight request by closing its socket,
    which unblocks a recv() that would otherwise wait for the (possibly stuck)
    server."""

    def __init__(self):
        self._cancelled = False
        self._sock = None

    def bind(self, sock):
        self._sock = sock
        if self._cancelled:
            self._close()

    def _close(self):
        s = self._sock
        if s is not None:
            try:
                _ws2.closesocket(s)
            except Exception:  # noqa: BLE001
                pass

    def cancel(self):
        self._cancelled = True
        self._close()

    def cancelled(self):
        return self._cancelled


def request(port, method, path, headers=None, body=b"", host="127.0.0.1",
            recv_timeout_ms=120000, stop_marker=None, max_bytes=16 * 1024 * 1024,
            cancel=None):
    """Perform an HTTP request against host:port and return (status, headers, body_bytes).

    Reads until the peer closes the connection (send ``Connection: close``) or
    until ``stop_marker`` (bytes) appears in the accumulated stream. The body is
    de-chunked if it used chunked transfer-encoding. If ``cancel`` (a
    :class:`CancelToken`) is tripped, the socket is closed and the read aborts.
    """
    _ensure_started()

    if isinstance(body, str):
        body = body.encode("utf-8")

    hdrs = {
        "Host": "%s:%d" % (host, port),
        "Accept": "*/*",
        "Connection": "close",
    }
    if headers:
        hdrs.update(headers)
    if body:
        hdrs["Content-Length"] = str(len(body))

    lines = ["%s %s HTTP/1.1" % (method, path)]
    for k, v in hdrs.items():
        lines.append("%s: %s" % (k, v))
    req = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body

    sock = _ws2.socket(AF_INET, SOCK_STREAM, IPPROTO_TCP)
    if sock == INVALID_SOCKET:
        raise HttpError("socket() failed")

    if cancel is not None:
        cancel.bind(sock)

    try:
        if cancel is not None and cancel.cancelled():
            raise HttpError("cancelled")

        tv = ctypes.c_int(recv_timeout_ms)
        _ws2.setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, ctypes.byref(tv), ctypes.sizeof(tv))
        _ws2.setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, ctypes.byref(tv), ctypes.sizeof(tv))

        addr = _sockaddr_in()
        addr.sin_family = AF_INET
        addr.sin_port = _htons(port)
        addr.sin_addr = _ws2.inet_addr(host.encode("ascii"))
        if _ws2.connect(sock, ctypes.byref(addr), ctypes.sizeof(addr)) == SOCKET_ERROR:
            raise HttpError("connect() to %s:%d failed (is the server running?)" % (host, port))

        # Send the full request.
        total = 0
        while total < len(req):
            n = _ws2.send(sock, req[total:], len(req) - total, 0)
            if n == SOCKET_ERROR or n <= 0:
                raise HttpError("send() failed")
            total += n

        # Receive until closed / stop_marker / limit.
        buf = ctypes.create_string_buffer(65536)
        chunks = []
        received = bytearray()
        while len(received) < max_bytes:
            if cancel is not None and cancel.cancelled():
                break
            n = _ws2.recv(sock, buf, 65536, 0)
            if n == 0:
                break
            if n == SOCKET_ERROR:
                break
            piece = buf.raw[:n]
            chunks.append(piece)
            received += piece
            if stop_marker and stop_marker in received:
                break
        raw = bytes(received)
    finally:
        _ws2.closesocket(sock)

    return _parse_response(raw)


def _parse_response(raw):
    sep = raw.find(b"\r\n\r\n")
    if sep < 0:
        raise HttpError("malformed HTTP response (no header terminator)")
    head = raw[:sep].decode("iso-8859-1")
    body = raw[sep + 4:]

    header_lines = head.split("\r\n")
    status_line = header_lines[0]
    try:
        status = int(status_line.split(" ", 2)[1])
    except (IndexError, ValueError):
        status = 0

    headers = {}
    for line in header_lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = _dechunk(body)

    return status, headers, body


def _dechunk(body):
    out = bytearray()
    i = 0
    n = len(body)
    while i < n:
        j = body.find(b"\r\n", i)
        if j < 0:
            break
        size_str = body[i:j].split(b";")[0].strip()
        try:
            size = int(size_str, 16)
        except ValueError:
            break
        if size == 0:
            break
        start = j + 2
        out += body[start:start + size]
        i = start + size + 2  # skip data + trailing CRLF
    return bytes(out)


def _dechunk_incremental(buf):
    """Decode as many complete HTTP chunks as are present in ``buf``. Returns
    (decoded_bytes, leftover_bytearray) where leftover holds an incomplete chunk
    to be completed by later reads."""
    out = bytearray()
    i = 0
    n = len(buf)
    while True:
        j = buf.find(b"\r\n", i)
        if j < 0:
            break
        size_str = bytes(buf[i:j]).split(b";")[0].strip()
        try:
            size = int(size_str, 16)
        except ValueError:
            break
        if size == 0:
            i = j + 2
            break
        start = j + 2
        end = start + size
        if end + 2 > n:      # data (+ trailing CRLF) not fully arrived yet
            break
        out += buf[start:end]
        i = end + 2
    return bytes(out), bytearray(buf[i:])


def _find_event_sep(buf):
    """Find the earliest SSE event separator (blank line). Handles both LF and
    CRLF line endings. Returns (index, sep_len) or (-1, 0)."""
    a = buf.find(b"\n\n")
    b = buf.find(b"\r\n\r\n")
    if a < 0 and b < 0:
        return -1, 0
    if b < 0 or (0 <= a < b):
        return a, 2
    return b, 4


def stream(port, method, path, headers=None, body=b"", host="127.0.0.1",
           recv_timeout_ms=240000, on_event=None, cancel=None, max_bytes=64 * 1024 * 1024):
    """Like :func:`request` but delivers Server-Sent-Events *incrementally*.

    ``on_event(block_bytes)`` is called for each complete SSE event (the text
    between blank lines) as soon as it arrives, enabling live streaming. If
    ``on_event`` returns True, reading stops. Returns the HTTP status code."""
    _ensure_started()

    if isinstance(body, str):
        body = body.encode("utf-8")

    hdrs = {"Host": "%s:%d" % (host, port), "Accept": "*/*", "Connection": "close"}
    if headers:
        hdrs.update(headers)
    if body:
        hdrs["Content-Length"] = str(len(body))
    lines = ["%s %s HTTP/1.1" % (method, path)]
    for k, v in hdrs.items():
        lines.append("%s: %s" % (k, v))
    req = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body

    sock = _ws2.socket(AF_INET, SOCK_STREAM, IPPROTO_TCP)
    if sock == INVALID_SOCKET:
        raise HttpError("socket() failed")
    if cancel is not None:
        cancel.bind(sock)

    status = 0
    try:
        if cancel is not None and cancel.cancelled():
            raise HttpError("cancelled")
        tv = ctypes.c_int(recv_timeout_ms)
        _ws2.setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, ctypes.byref(tv), ctypes.sizeof(tv))
        _ws2.setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, ctypes.byref(tv), ctypes.sizeof(tv))

        addr = _sockaddr_in()
        addr.sin_family = AF_INET
        addr.sin_port = _htons(port)
        addr.sin_addr = _ws2.inet_addr(host.encode("ascii"))
        if _ws2.connect(sock, ctypes.byref(addr), ctypes.sizeof(addr)) == SOCKET_ERROR:
            raise HttpError("connect() to %s:%d failed (is the server running?)" % (host, port))

        total = 0
        while total < len(req):
            n = _ws2.send(sock, req[total:], len(req) - total, 0)
            if n == SOCKET_ERROR or n <= 0:
                raise HttpError("send() failed")
            total += n

        buf = ctypes.create_string_buffer(65536)
        header_done = False
        chunked = False
        header_raw = bytearray()
        pending = bytearray()     # undecoded (chunked) body bytes
        event_buf = bytearray()   # decoded body awaiting event-separator split
        received = 0
        stop = False
        while received < max_bytes and not stop:
            if cancel is not None and cancel.cancelled():
                break
            n = _ws2.recv(sock, buf, 65536, 0)
            if n == 0 or n == SOCKET_ERROR:
                break
            received += n
            piece = buf.raw[:n]

            if not header_done:
                header_raw += piece
                sep = header_raw.find(b"\r\n\r\n")
                if sep < 0:
                    continue
                head = bytes(header_raw[:sep]).decode("iso-8859-1")
                try:
                    status = int(head.split("\r\n", 1)[0].split(" ", 2)[1])
                except (IndexError, ValueError):
                    status = 0
                chunked = ("transfer-encoding: chunked" in head.lower())
                header_done = True
                piece = bytes(header_raw[sep + 4:])   # remainder is body

            if chunked:
                pending += piece
                decoded, pending = _dechunk_incremental(pending)
                event_buf += decoded
            else:
                event_buf += piece

            while True:
                idx, seplen = _find_event_sep(event_buf)
                if idx < 0:
                    break
                block = bytes(event_buf[:idx])
                del event_buf[:idx + seplen]
                if on_event is not None and on_event(block):
                    stop = True
                    break
        # Flush any trailing event without a terminating blank line.
        if not stop and event_buf.strip() and on_event is not None:
            on_event(bytes(event_buf))
    finally:
        _ws2.closesocket(sock)

    return status
