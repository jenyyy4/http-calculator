#!/usr/bin/env python3

import argparse
import socket
import sys
import time

HOST = "localhost"
PORT = 8080


def get(path, extra=""):
    return f"GET {path} HTTP/1.1\r\nHost: localhost\r\n{extra}\r\n".encode()


class Conn:

    def __init__(self):
        self.sock = socket.create_connection((HOST, PORT))
        self.sock.settimeout(5)
        self.buf = b""

    def send(self, data):
        self.sock.sendall(data)

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("server closed the connection")
        self.buf += chunk

    def read_response(self):
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", 0))
        while len(self.buf) < length:
            self._fill()
        body, self.buf = self.buf[:length], self.buf[length:]
        return status, headers, body.decode()

    def request(self, data):
        self.send(data)
        return self.read_response()

    def is_open(self):
        self.sock.setblocking(False)
        try:
            return self.sock.recv(1, socket.MSG_PEEK) != b""
        except BlockingIOError:
            return True
        except OSError:
            return False
        finally:
            self.sock.settimeout(5)

    def wait_closed(self, timeout):
        self.sock.settimeout(timeout)
        try:
            while True:
                if self.sock.recv(65536) == b"":
                    return True
        except (socket.timeout, OSError):
            return False
        finally:
            self.sock.settimeout(5)

    def close(self):
        self.sock.close()


results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def expect(conn, label, raw, status, body=None):
    got_status, headers, got_body = conn.request(raw)
    ok = got_status == status and (body is None or got_body == body)
    check(f"{label:<28} -> {status} {body or ''}".rstrip(), ok,
          "" if ok else f"got {got_status} {got_body!r}")
    return headers


def test_marking_script():
    print("\nThe marking script: one socket, every request")
    c = Conn()
    expect(c, "GET /add?a=2&b=3", get("/add?a=2&b=3"), 200, "5")
    expect(c, "GET /sub?a=10&b=4", get("/sub?a=10&b=4"), 200, "6")
    expect(c, "GET /mul?a=6&b=7", get("/mul?a=6&b=7"), 200, "42")
    expect(c, "GET /div?a=1&b=0", get("/div?a=1&b=0"), 400)
    expect(c, "GET /pow?a=2&b=8", get("/pow?a=2&b=8"), 404)
    expect(c, "POST /add", b"POST /add HTTP/1.1\r\nHost: localhost\r\n"
                           b"Content-Length: 0\r\n\r\n", 405)
    open_ = c.is_open()
    check("socket still open", open_, str(open_))
    print(f"  1 TCP handshake, 6 responses")
    c.close()


def test_full_feature_set():
    print("\nThe full feature set, still on one socket")
    c = Conn()
    expect(c, "GET /add?a=2&b=3", get("/add?a=2&b=3"), 200, "5")
    expect(c, "GET /sub?a=10&b=4", get("/sub?a=10&b=4"), 200, "6")
    expect(c, "GET /mul?a=6&b=7", get("/mul?a=6&b=7"), 200, "42")
    expect(c, "GET /div?a=9&b=3", get("/div?a=9&b=3"), 200, "3")
    expect(c, "GET /div?a=1&b=0", get("/div?a=1&b=0"), 400)
    expect(c, "GET /add?a=x&b=3", get("/add?a=x&b=3"), 400)
    expect(c, "GET /pow?a=2&b=8", get("/pow?a=2&b=8"), 404)
    h = expect(c, "POST /add", b"POST /add HTTP/1.1\r\nHost: localhost\r\n"
                               b"Content-Length: 0\r\n\r\n", 405)
    check("405 carries Allow header", "allow" in h, h.get("allow", "missing"))
    expect(c, "GET /add (no Host)", b"GET /add HTTP/1.1\r\n\r\n", 400)
    expect(c, "GET /add?a=-7&b=2.5", get("/add?a=-7&b=2.5"), 200, "-4.5")
    expect(c, "GET /div?a=1&b=4", get("/div?a=1&b=4"), 200, "0.25")
    expect(c, "GET /add?a=2 (missing b)", get("/add?a=2"), 400)
    check("socket still open after 12 requests", c.is_open())
    c.close()


def test_byte_n_plus_one():
    print("\nContent-Length: consume exactly n bytes, byte n+1 is the next request")
    c = Conn()
    c.send(b"POST /add HTTP/1.1\r\nHost: localhost\r\nContent-Length: 7\r\n\r\n"
           b"a=2&b=3" + get("/add?a=40&b=2"))
    s1, _, _ = c.read_response()
    s2, _, b2 = c.read_response()
    check("POST with 7-byte body -> 405", s1 == 405, str(s1))
    check("glued-on GET -> 200 42", (s2, b2) == (200, "42"), f"{s2} {b2!r}")

    evil = get("/pow?a=1&b=1")
    c.send(b"POST /add HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
           + str(len(evil)).encode() + b"\r\n\r\n" + evil + get("/mul?a=3&b=3"))
    s1, _, _ = c.read_response()
    s2, _, b2 = c.read_response()
    check("request-shaped body is not parsed", s1 == 405 and (s2, b2) == (200, "9"),
          f"{s1}, {s2} {b2!r}")
    c.close()


def test_pipelining():
    print("\nPipelining: all six at once, answered in order")
    c = Conn()
    batch = [
        (get("/add?a=2&b=3"), 200, "5"),
        (get("/sub?a=10&b=4"), 200, "6"),
        (get("/mul?a=6&b=7"), 200, "42"),
        (get("/div?a=1&b=0"), 400, None),
        (get("/pow?a=2&b=8"), 404, None),
        (b"POST /add HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n", 405, None),
    ]
    c.send(b"".join(raw for raw, _, _ in batch))
    got = [c.read_response() for _ in batch]
    ok = all(s == es and (eb is None or b == eb)
             for (s, _, b), (_, es, eb) in zip(got, batch))
    check("6 pipelined requests, 6 in-order responses", ok,
          ", ".join(f"{s}" + (f" {b}" if s == 200 else "") for s, _, b in got))
    check("socket still open", c.is_open())
    c.close()


def test_trickle():
    print("\nPartial reads: one request sent a byte at a time")
    c = Conn()
    for byte in get("/mul?a=12&b=12"):
        c.send(bytes([byte]))
        time.sleep(0.002)
    s, _, b = c.read_response()
    check("byte-by-byte GET -> 200 144", (s, b) == (200, "144"), f"{s} {b!r}")
    c.close()


def test_chunked():
    print("\nChunked request body, then another request on the same socket")
    c = Conn()
    c.send(b"POST /add HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n"
           b"4\r\na=2&\r\n"
           b"3;ext=1\r\nb=3\r\n"
           b"0\r\nX-Trailer: yes\r\n\r\n"
           + get("/add?a=1&b=1"))
    s1, _, _ = c.read_response()
    s2, _, b2 = c.read_response()
    check("chunked POST -> 405", s1 == 405, str(s1))
    check("following GET -> 200 2", (s2, b2) == (200, "2"), f"{s2} {b2!r}")
    c.close()


def test_connection_close():
    print("\nConnection: close is honoured")
    c = Conn()
    s, h, b = c.request(get("/add?a=2&b=3", "Connection: close\r\n"))
    check("response 200 5", (s, b) == (200, "5"))
    check("response says Connection: close", h.get("connection") == "close",
          h.get("connection"))
    check("server closes the socket", c.wait_closed(2))
    c.close()

    c = Conn()
    s, h, _ = c.request(b"GET /add?a=1&b=1 HTTP/1.0\r\n\r\n")
    check("HTTP/1.0 defaults to close", s == 200 and c.wait_closed(2))
    c.close()

    c = Conn()
    c.request(b"GET /add?a=1&b=1 HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
    check("HTTP/1.0 + keep-alive stays open", c.is_open())
    c.close()


def test_malformed_framing():
    print("\nBroken framing: 400 and close (the next request boundary is unknown)")
    cases = [
        ("bad Content-Length", b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n"),
        ("CL + TE together", b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n"
                             b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"),
        ("garbage request line", b"HELLO\r\n\r\n"),
        ("space before colon", b"GET /add?a=1&b=1 HTTP/1.1\r\nHost : x\r\n\r\n"),
    ]
    for label, raw in cases:
        c = Conn()
        s, h, _ = c.request(raw)
        check(f"{label:<22} -> 400 + close",
              s == 400 and h.get("connection") == "close" and c.wait_closed(2), str(s))
        c.close()


def test_idle_timeout(seconds):
    print(f"\nIdle timeout: server should hang up after ~{seconds:g}s of silence")
    c = Conn()
    c.request(get("/add?a=1&b=1"))
    start = time.time()
    closed = c.wait_closed(seconds + 3)
    took = time.time() - start
    check(f"closed after idle", closed and took >= seconds * 0.9, f"{took:.1f}s")
    c.close()


def main():
    global HOST, PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--idle-timeout", type=float, default=0,
                    help="if set, also test that the server idles out after this long")
    args = ap.parse_args()
    HOST, PORT = args.host, args.port

    test_marking_script()
    test_full_feature_set()
    test_byte_n_plus_one()
    test_pipelining()
    test_trickle()
    test_chunked()
    test_connection_close()
    test_malformed_framing()
    if args.idle_timeout:
        test_idle_timeout(args.idle_timeout)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
