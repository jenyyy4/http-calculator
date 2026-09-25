import argparse
import re
import socket
import threading
import time
from urllib.parse import unquote_plus, urlsplit

MAX_LINE = 8 * 1024
MAX_HEADERS = 100
MAX_BODY = 1024 * 1024
MAX_OPERAND_LEN = 64
REQUEST_TIMEOUT = 10.0

REASONS = {
    100: "Continue",
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    413: "Content Too Large",
    431: "Request Header Fields Too Large",
    501: "Not Implemented",
    505: "HTTP Version Not Supported",
}

class ProtocolError(Exception):

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class PeerClosed(Exception):
    pass

class Reader:

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def _fill(self):
        data = self.sock.recv(65536)
        if not data:
            raise PeerClosed()
        self.buf += data

    def has_buffered(self):
        return len(self.buf) > 0

    def read_line(self, limit=MAX_LINE, status=400):
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                if i > limit:
                    raise ProtocolError(status, "line too long")
                line = bytes(self.buf[:i])
                del self.buf[:i + 1]
                return line[:-1] if line.endswith(b"\r") else line
            if len(self.buf) > limit:
                raise ProtocolError(status, "line too long")
            self._fill()

    def read_exact(self, n):
        while len(self.buf) < n:
            self._fill()
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

TOKEN_RE = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
VERSION_RE = re.compile(rb"^HTTP/(\d)\.(\d)$")

class Request:
    def __init__(self, method, target, version, headers, body):
        self.method = method
        self.target = target
        self.version = version
        self.headers = headers
        self.body = body

    def header(self, name):
        values = self.headers.get(name)
        return values[-1] if values else None

    def connection_tokens(self):
        tokens = set()
        for value in self.headers.get("connection", []):
            tokens.update(t.strip().lower() for t in value.split(","))
        return tokens

    def wants_keep_alive(self):
        tokens = self.connection_tokens()
        if "close" in tokens:
            return False
        if self.version >= (1, 1):
            return True 
        return "keep-alive" in tokens 


def read_request(reader, sock, idle_timeout):
    sock.settimeout(idle_timeout)
    try:
        line = reader.read_line()
        for _ in range(4):
            if line:
                break
            line = reader.read_line()
    except (PeerClosed, socket.timeout, ConnectionError):
        if reader.has_buffered():
            raise ProtocolError(408, "incomplete request")
        return None

    sock.settimeout(REQUEST_TIMEOUT)
    try:
        return _read_rest(reader, sock, line)
    except socket.timeout:
        raise ProtocolError(408, "request not completed in time")
    except PeerClosed:
        raise ProtocolError(400, "connection closed mid-request")


def _read_rest(reader, sock, request_line):
    parts = request_line.split(b" ")
    if len(parts) != 3:
        raise ProtocolError(400, "malformed request line")
    method_b, target_b, version_b = parts
    if not TOKEN_RE.match(method_b):
        raise ProtocolError(400, "malformed method")
    m = VERSION_RE.match(version_b)
    if not m:
        raise ProtocolError(400, "malformed HTTP version")
    version = (int(m.group(1)), int(m.group(2)))
    if version[0] != 1:
        raise ProtocolError(505, "only HTTP/1.x is supported")
    if not target_b or any(c <= 0x20 or c == 0x7F for c in target_b):
        raise ProtocolError(400, "malformed request target")

    headers = {}
    count = 0
    while True:
        line = reader.read_line(status=431)
        if line == b"":
            break
        count += 1
        if count > MAX_HEADERS:
            raise ProtocolError(431, "too many header fields")
        if line[:1] in (b" ", b"\t"):
            raise ProtocolError(400, "obsolete line folding is not accepted")
        name, sep, value = line.partition(b":")
        if not sep or not TOKEN_RE.match(name):
            raise ProtocolError(400, "malformed header field")
        headers.setdefault(name.decode("ascii").lower(), []).append(
            value.strip(b" \t").decode("latin-1"))

    body = b""
    te = headers.get("transfer-encoding")
    cl = headers.get("content-length")

    if te is not None and cl is not None:
        raise ProtocolError(400, "both Transfer-Encoding and Content-Length")

    if te is not None:
        codings = [c.strip().lower() for v in te for c in v.split(",") if c.strip()]
        if codings != ["chunked"]:
            raise ProtocolError(501, "only 'Transfer-Encoding: chunked' is supported")
        maybe_continue(sock, headers, version)
        body = read_chunked(reader)

    elif cl is not None:
        values = {v.strip() for raw in cl for v in raw.split(",")}
        if len(values) != 1:
            raise ProtocolError(400, "conflicting Content-Length values")
        value = values.pop()
        if not value.isdigit() or not value.isascii():
            raise ProtocolError(400, "invalid Content-Length")
        length = int(value)
        if length > MAX_BODY:
            raise ProtocolError(413, "body too large")
        if length:
            maybe_continue(sock, headers, version)
        body = reader.read_exact(length)

    return Request(method_b.decode("ascii"), target_b.decode("latin-1"),
                   version, headers, body)


def read_chunked(reader):
    body = bytearray()
    while True:
        size_line = reader.read_line()
        size_str = size_line.split(b";", 1)[0].strip()
        if not size_str or not re.match(rb"^[0-9A-Fa-f]+$", size_str):
            raise ProtocolError(400, "malformed chunk size")
        size = int(size_str, 16)
        if size == 0:
            break
        if len(body) + size > MAX_BODY:
            raise ProtocolError(413, "body too large")
        body += reader.read_exact(size)
        if reader.read_line() != b"":
            raise ProtocolError(400, "chunk data not followed by CRLF")
    for _ in range(MAX_HEADERS + 1):
        if reader.read_line(status=431) == b"":
            return bytes(body)
    raise ProtocolError(431, "too many trailer fields")


def maybe_continue(sock, headers, version):
    expect = (headers.get("expect") or [""])[-1].lower()
    if version >= (1, 1) and expect == "100-continue":
        sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")


NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")

class BadInput(Exception):
    pass

def parse_query(query):
    params = {}
    for pair in query.split("&"):
        if not pair:
            continue
        name, _, value = pair.partition("=")
        name, value = unquote_plus(name), unquote_plus(value)
        if name in params:
            raise BadInput(f"parameter '{name}' given more than once")
        params[name] = value
    return params

def parse_number(name, text):
    if len(text) > MAX_OPERAND_LEN or not NUMBER_RE.match(text):
        raise BadInput(f"'{name}' is not a number")
    try:
        return int(text)
    except ValueError:
        value = float(text)
        if value != value or value in (float("inf"), float("-inf")):
            raise BadInput(f"'{name}' is out of range")
        return value


def divide(a, b):
    if b == 0:
        raise BadInput("division by zero")
    if isinstance(a, int) and isinstance(b, int) and a % b == 0:
        return a // b 
    return a / b


OPERATIONS = {
    "/add": lambda a, b: a + b,
    "/sub": lambda a, b: a - b,
    "/mul": lambda a, b: a * b,
    "/div": divide,
}
ALLOWED_METHODS = ("GET", "HEAD")


def format_number(x):
    if isinstance(x, float):
        if x != x or x in (float("inf"), float("-inf")):
            raise BadInput("result is out of range")
        if x.is_integer() and abs(x) < 1e16:
            return str(int(x))
        return repr(x)
    return str(x)


def handle(request):
    if request.version >= (1, 1) and len(request.headers.get("host", [])) != 1:
        return 400, [], "missing or duplicate Host header"

    try:
        url = urlsplit(request.target)
    except ValueError:
        return 400, [], "malformed request target"
    if not url.path.startswith("/"):
        return 400, [], "malformed request target"

    op = OPERATIONS.get(url.path)
    if op is None:
        return 404, [], f"no such operation: {url.path}"
    if request.method not in ALLOWED_METHODS:
        return 405, [("Allow", ", ".join(ALLOWED_METHODS))], \
            f"method {request.method} not allowed"

    try:
        params = parse_query(url.query)
        if "a" not in params or "b" not in params:
            raise BadInput("need both 'a' and 'b'")
        a = parse_number("a", params["a"])
        b = parse_number("b", params["b"])
        return 200, [], format_number(op(a, b))
    except BadInput as e:
        return 400, [], str(e)
    except (OverflowError, ValueError):
        return 400, [], "result is out of range"

def build_response(status, body_text, keep_alive, idle_timeout,
                   extra_headers=(), head_only=False):
    body = body_text.encode("utf-8")
    lines = [
        f"HTTP/1.1 {status} {REASONS[status]}",
        "Server: stays-on-the-line/1.0",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
    ]
    for name, value in extra_headers:
        lines.append(f"{name}: {value}")
    if keep_alive:
        lines.append("Connection: keep-alive")
        lines.append(f"Keep-Alive: timeout={int(idle_timeout)}")
    else:
        lines.append("Connection: close")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    return head if head_only else head + body

_conn_ids = iter(range(1, 1 << 62))
_conn_lock = threading.Lock()


def log(conn_id, msg):
    print(f"{time.strftime('%H:%M:%S')} [conn {conn_id}] {msg}", flush=True)


def serve_connection(sock, addr, idle_timeout):
    with _conn_lock:
        conn_id = next(_conn_ids)
    log(conn_id, f"opened from {addr[0]}:{addr[1]}")
    reader = Reader(sock)
    served = 0
    reason = "client closed"
    try:
        while True:
            try:
                request = read_request(reader, sock, idle_timeout)
            except ProtocolError as e:
                log(conn_id, f"-> {e.status} ({e.message}), closing")
                try:
                    sock.sendall(build_response(e.status, e.message, False,
                                                idle_timeout))
                except OSError:
                    pass
                reason = "protocol error"
                break
            if request is None:
                reason = "client closed or idle timeout"
                break

            status, extra, text = handle(request)
            keep_alive = request.wants_keep_alive()
            sock.sendall(build_response(status, text, keep_alive, idle_timeout,
                                        extra, head_only=request.method == "HEAD"))
            served += 1
            log(conn_id, f"#{served} {request.method} {request.target} -> {status}"
                         f"{'' if keep_alive else ' (closing)'}")
            if not keep_alive:
                reason = "Connection: close"
                break
    except OSError as e:
        reason = f"socket error: {e}"
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        sock.close()
        log(conn_id, f"closed after {served} response(s): {reason}")


def main():
    parser = argparse.ArgumentParser(
        description="An HTTP/1.1 server written directly on top of a TCP socket: "
        "no http.server, no framework."
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--idle-timeout", type=float, default=15.0,
                        help="seconds a connection may sit idle between requests")
    args = parser.parse_args()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((args.host, args.port))
    listener.listen(128)
    print(f"listening on {args.host}:{args.port} "
          f"(idle timeout {args.idle_timeout:g}s)", flush=True)

    try:
        while True:
            sock, addr = listener.accept()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=serve_connection,
                             args=(sock, addr, args.idle_timeout),
                             daemon=True).start()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        listener.close()


if __name__ == "__main__":
    main()
