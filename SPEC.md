# BHTP/1: Binary HTTP, version 1

A binary framing of HTTP request/response semantics over one long-lived TCP connection.
This document is enough to build a compatible client or server. MUST / SHOULD / MAY follow RFC 2119.
All integers are **unsigned, big-endian (network byte order)**. All lengths are in **bytes**.

## 1. Connection
- Transport is TCP. The default port is **9000**. One connection carries any number of requests, and the server **keeps it open**.
- **Preface.** The client's first 8 bytes MUST be `42 48 54 50 01 00 0D 0A` (`"BHTP"`, major=1, minor=0, CRLF).
  The server checks them and, if they are good, sends the same 8 bytes back before any frame.
  - Bad magic (bytes 0–3) or bytes 6–7 not CRLF → the server sends `GOAWAY(400)` and closes. This check comes first. In that case the client reads a GOAWAY frame header where it expected the server's preface. A client MUST treat any 8 bytes that aren't `"BHTP"` + major 1 as a failed handshake. Because of this, an HTTP/1.x client gets a clean failure instead of a hang. The CRLF also makes an HTTP/1.x server reject our preface quickly.
  - Major ≠ 1 → `GOAWAY(505)` and close. A minor version the receiver doesn't know is accepted (minor versions only add things).
- **Closing.** The client closes the connection. It SHOULD send `GOAWAY(0)` first, with `last_request_id` set to the last ID it sent. A server that receives GOAWAY processes no further frames and closes. If the server sees no frame for **30 s**, it MAY send `GOAWAY(408)` and close.
- A client MUST NOT need a second connection. Every request goes on the one connection.

## 2. Frame header: fixed 8 bytes, then the payload
```
 0               1               2               3
+---------------+---------------+---------------+---------------+
|              Length (24)                      |   Type (8)    |
+---------------+---------------+---------------+---------------+
|   Flags (8)   |                Request ID (24)                |
+---------------+---------------+---------------+---------------+
|                  Payload (Length bytes) ...
```
| Field | Width | Why this width |
|---|---|---|
| Length | 24 bit | The payload can be 0 to 16,777,215 bytes. That's the same as HTTP/2: 16 MiB is plenty for one frame, and bigger bodies are split across several DATA frames. 16 bits (64 KiB) would cost 2× the frames on big files. 32 bits would let one frame block the link for gigabytes. |
| Type | 8 bit | 256 frame types is far more than v1 needs (4). The spare types are where extensions go. |
| Flags | 8 bit | Per-type booleans. v1 uses 2 bits. Unused bits MUST be sent as 0 and ignored on receipt. |
| Request ID | 24 bit | This field is what makes pipelining and interleaving possible: every frame says which request it belongs to. 16.7 M requests per connection is more than any session needs. HTTP/2 uses 31 bits plus 1 reserved bit, a 9-byte header. We chose 24 bits so the header is a **round 8 bytes**: two 32-bit words, read with two `ntohl()` calls and no masking of a reserved bit. ID **0** means the connection itself (only GOAWAY uses it). |

A receiver always knows where the next frame starts: 8 + Length bytes later. **This is the rule that makes everything below safe.**

## 3. Frame types
| Type | Name | Sent by | Request ID | Flags used |
|---|---|---|---|---|
| `0x01` | REQUEST | client | ≥1, **strictly increasing** on the connection (gaps allowed) | END_STREAM, REUSE_HEADERS |
| `0x02` | RESPONSE | server | the ID of the request it answers | END_STREAM |
| `0x03` | DATA | both | the request it belongs to | END_STREAM |
| `0x07` | GOAWAY | both | 0 | none |
| anything else | unknown | | any | |

**Flags:** `0x01 END_STREAM` means this is the last frame for this request ID in this direction. `0x02 REUSE_HEADERS` (REQUEST only) is described in section 5.

> **A receiver that meets a frame type it does not know MUST read and discard `Length` payload bytes and carry on. It MUST NOT treat that as an error.**
> This is the v2 escape hatch, the same as TLV: new types (PING, SETTINGS, PUSH...) can be added without breaking v1 peers.

A receiver MUST also silently drop DATA or RESPONSE frames for an ID that isn't open (it may already have been answered).

## 4. REQUEST payload
```
method (1) | path_len (2) | path (path_len bytes, UTF-8, MUST start with "/") | header block (section 5)
```
Method codes: `1` GET, `2` HEAD, `3` POST, `4` PUT, `5` DELETE, `6` OPTIONS.
If the request has no body, the REQUEST frame carries END_STREAM. If it does have a body, DATA frames follow, and the last one carries END_STREAM. A server that doesn't use request bodies (v1 file servers support only GET/HEAD) answers as soon as the REQUEST arrives and drops the DATA frames that follow.
**Path → file:** the server serves files under its root directory. A path ending in `/` serves that directory's `index.html`. Anything after `?` is ignored. Percent-encoding is not used, because the path is raw UTF-8.
The version is not repeated in each request. It was settled once, by the preface.

## 5. Header block: static table + length-prefixed literals (HPACK's first two ideas)
```
count (1) | entry × count          ← count is always present, even when it is 0
entry  = 0x01..0x0A  value_len (2) value                 ← name taken from the static table
       | 0x00  name_len (1) name  value_len (2) value     ← literal name, lowercase ASCII, 1..255 bytes
```
Static table: `1` host · `2` user-agent · `3` accept · `4` if-none-match · `5` content-type · `6` content-length · `7` last-modified · `8` etag · `9` cache-control · `10` date.
These are the ten names a file client and a file server send most often. Each one costs 1 byte instead of 4 to 14.
Values are opaque bytes. In v1 they hold the same text HTTP/1.1 would use, for example `content-length` = `"20"`. Index bytes `0x0B`–`0xFF` are reserved, and a receiver MUST reject them with 400.

**REUSE_HEADERS (fighting header repetition).** Most headers repeat on every request (host, user-agent, accept...). When a REQUEST has flag `0x02`, its effective header set is the **previous request's effective header set on this connection**, with this frame's entries added on top (an entry replaces any earlier header of the same name). On the first request, or after a request that got 400, the previous set is empty. So a pipelined second request only needs its method and path: `/logo.png` costs **21 bytes** on the wire, compared with 84 bytes in HTTP/1.1.

## 6. RESPONSE payload and body
```
status (2) | header block (section 5)
```
`status` is the HTTP status number itself (200, 304, 400, 404, 405, 505...). Two bytes holds every code without needing a lookup table.
The body follows in DATA frames. Their payloads concatenated are the body, **byte for byte**. Nothing is escaped, so binary files are safe. The last DATA frame carries END_STREAM.
If the body is empty (HEAD, 304, an empty file), the RESPONSE frame itself carries END_STREAM and no DATA frame follows.
`content-length` is only a hint. **END_STREAM is what ends a body.** Senders SHOULD keep DATA frames at 16 KiB or less. Receivers MUST accept any length.

## 7. Pipelining and ordering
A client MAY send many REQUESTs without waiting. A server MAY answer in any order, and MAY interleave the frames of different responses. A client matches frames to requests using the Request ID, never by arrival order.

## 8. Errors
| Situation | Kind | What happens |
|---|---|---|
| REQUEST payload can't be parsed: a field runs past Length, the path doesn't start with `/`, a reserved header index, trailing bytes | request | `RESPONSE 400` on that ID. **The connection stays open**, because framing is still in sync. |
| REQUEST payload > 65,535 bytes (the v1 limit) | request | The server reads and discards it, then sends `400`. |
| The file isn't there, or the path escapes the root | request | `404` |
| Method not supported | request | `405` |
| Bad preface / unsupported major version | connection | `GOAWAY(400)` / `GOAWAY(505)`, then close |
| REQUEST with ID 0 or an ID that isn't increasing | connection | `GOAWAY(400)`, then close |
| EOF in the middle of a frame | connection | close |

**GOAWAY payload:** `last_request_id (3) | code (2) | reason (the rest of the payload, UTF-8, optional)`. Code 0 means a graceful goodbye.
**Client exit status:** `0` if every response was < 400, `4` if any was 4xx, `5` if any was 5xx (5 wins over 4), `2` for a connection or protocol error, `1` for bad usage. The body still goes to stdout on a 4xx/5xx.

## 9. Extensibility
New frame types are skipped by old peers (section 3). Unknown flag bits are ignored. The minor version can go up without breaking anyone. Type numbers `0x04`–`0x06` are reserved for PING / SETTINGS / RESET in v2, and `0x80`–`0xFF` for private experiments.

## 10. Worked example
`GET /hello.txt` → `200`, 19-byte body. Every byte is annotated in **HEXDUMP.md**.
```
C→S  42 48 54 50 01 00 0d 0a                                      preface
C→S  00 00 31 01 01 00 00 01 | 01 00 0a "/hello.txt" 03 01 00 0e "localhost:9000" 02 00 09 "bcurl/1.0" 03 00 03 "*/*"
S→C  42 48 54 50 01 00 0d 0a                                      preface
S→C  00 00 8b 02 00 00 00 01 | 00 c8 07 0a 00 1d "Sun, 04 Oct…" 00 06 "server" 00 0a "server/1.0" 05 …
S→C  00 00 13 03 01 00 00 01 | "Hello, I am Aryen.\n"
C→S  00 00 05 07 00 00 00 00 | 00 00 01 00 00                     GOAWAY(0), last id 1
```
