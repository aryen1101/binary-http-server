# binary-http-server
A small file server that speaks **BHTP/1**, a binary version of HTTP.

Instead of text like `GET /index.html HTTP/1.1\r\n...`, every message is a frame with a fixed 8-byte header (length, type, flags, request id) followed by the payload. Common header names are sent as a single byte. Requests can be pipelined on one TCP connection, and the connection stays open between requests.

The full protocol is in [SPEC.md](SPEC.md). A byte-by-byte walkthrough of a real request and response is in [HEXDUMP.md](HEXDUMP.md).

Written in Python 3 with only the standard library (plain sockets, no frameworks).

## Running

```bash
python3 server.py ./www 9000
```

Use `-v` to print a hexdump of every frame going in and out:

```bash
python3 server.py -v ./www 9000
```

On Windows, use `python` instead of `python3`.

## What it does

- Serves files from the given folder. `/` and directories serve `index.html`.
- Sends files in 16 KB DATA frames. Bytes are sent as-is, so binary files (images etc.) work.
- Returns 404 for missing files, 400 for malformed request frames, and 405 for anything other than GET/HEAD.
- Keeps the connection open after errors like 400/404. The connection is only closed on a bad preface, an invalid request id, a client GOAWAY, or 30 seconds of inactivity.
- Skips frame types it doesn't recognise, so newer clients can add frame types without breaking it.
- Supports ETag / If-None-Match (304) and the REUSE_HEADERS flag, which lets a client skip resending the same headers.
- Paths can't escape the root folder (`../` gives a 404).

## Files

```
server.py     the server
SPEC.md       protocol specification
HEXDUMP.md    annotated example of one request/response on the wire
www/          sample files to serve
```
