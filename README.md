# A calculator that stays on the line

An HTTP/1.1 calculator written on a bare TCP socket (Python 3 standard library only, with no
http.server and no framework). It keeps each connection open and answers every
request on it.

```
python3 server.py                       # listens on :8080, 15s idle timeout
python3 test_client.py                  # raw-socket test suite

# to also test the idle timeout:
python3 server.py --idle-timeout 2
python3 test_client.py --idle-timeout 2
```

| Request                  | Response |
|--------------------------|----------|
| `GET /add?a=2&b=3`       | 200 `5`  |
| `GET /sub?a=10&b=4`      | 200 `6`  |
| `GET /mul?a=6&b=7`       | 200 `42` |
| `GET /div?a=9&b=3`       | 200 `3`  |
| `GET /div?a=1&b=0`       | 400      |
| `GET /add?a=x&b=3`       | 400      |
| `GET /pow?a=2&b=8`       | 404      |
| `POST /add`              | 405 (with `Allow: GET, HEAD`) |
| `GET /add` with no Host  | 400      |

## Two kinds of 400

| Kind | Example | Connection |
|------|---------|------------|
| Bad **content**, good framing | `a=x`, `b=0`, missing `Host` | **stays open**: the server knows where the next request starts |
| Bad **framing** | `Content-Length: abc`, both CL and TE, garbage request line, `Host : x` | **closed**: the next request boundary is unknown, and guessing it is how request smuggling happens |

The server reads a request's body even when it rejects that request with 405.
Otherwise the unread body would be taken as the start of the next request.

## Also handled

* `Expect: 100-continue`: the server sends `100 Continue` before reading the body, so curl doesn't wait.
* `HEAD`: returns the same headers as `GET`, with no body.
* Limits: 8 KiB per line, 100 headers, 1 MiB body, 64-character operands.
* Integer results stay integers (`9/3` gives `3`, not `3.0`). Division that isn't exact
  returns a decimal (`1/4` gives `0.25`).
