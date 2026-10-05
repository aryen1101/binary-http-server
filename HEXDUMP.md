# Annotated hexdump: one complete request and response

Captured with:
```
python3 server.py -v ./www 9000                   # -v: the server hexdumps every frame it sends and receives
client: GET localhost:9000/hello.txt              # www/hello.txt = "Hello, I am Aryen.\n" (19 bytes)
```
These are the actual bytes on the wire, from `server.py -v`. There are 6 transmissions in total: preface ×2, REQUEST, RESPONSE, DATA, GOAWAY.
**Total bytes:** client → server 8 + 57 + 13 = **78**. Server → client 8 + 147 + 27 = **182**.

---

## 1. Client preface (8 bytes)
```
42 48 54 50 01 00 0d 0a      |BHTP....|
```
| Bytes | Meaning |
|---|---|
| `42 48 54 50` | magic `"BHTP"` |
| `01` | major version 1 |
| `00` | minor version 0 |
| `0d 0a` | CRLF, so an HTTP/1 server sees a broken request line and rejects it quickly |

## 2. REQUEST frame (8-byte header + 49-byte payload = 57 bytes)
```
00000000  00 00 31 01 01 00 00 01  01 00 0a 2f 68 65 6c 6c  |..1......../hell|
00000010  6f 2e 74 78 74 03 01 00  0e 6c 6f 63 61 6c 68 6f  |o.txt....localho|
00000020  73 74 3a 39 30 30 30 02  00 09 62 63 75 72 6c 2f  |st:9000...bcurl/|
00000030  31 2e 30 03 00 03 2a 2f  2a                       |1.0...*/*|
```
| Offset | Bytes | Field | Value |
|---|---|---|---|
| 0x00 | `00 00 31` | **Length** (24) | 0x31 = **49** payload bytes |
| 0x03 | `01` | **Type** | REQUEST |
| 0x04 | `01` | **Flags** | END_STREAM (no request body) |
| 0x05 | `00 00 01` | **Request ID** (24) | 1 |
| 0x08 | `01` | method | 1 = GET |
| 0x09 | `00 0a` | path_len | 10 |
| 0x0B | `2f 68 65 6c 6c 6f 2e 74 78 74` | path | `/hello.txt` |
| 0x15 | `03` | header count | 3 |
| 0x16 | `01` | header 1 name | static #1 = `host` |
| 0x17 | `00 0e` | value_len | 14 |
| 0x19 | `6c 6f … 30 30` | value | `localhost:9000` |
| 0x27 | `02` | header 2 name | static #2 = `user-agent` |
| 0x28 | `00 09` | value_len | 9 |
| 0x2A | `62 63 … 2e 30` | value | `bcurl/1.0` |
| 0x33 | `03` | header 3 name | static #3 = `accept` |
| 0x34 | `00 03` | value_len | 3 |
| 0x36 | `2a 2f 2a` | value | `*/*` |

Check: 1 + 2 + 10 + 1 + (1+2+14) + (1+2+9) + (1+2+3) = **49** ✓. The same request in HTTP/1.1 text takes 85 bytes. Here it takes 57.

## 3. Server preface (8 bytes)
```
42 48 54 50 01 00 0d 0a      |BHTP....|
```
These are the client's 8 bytes sent back. They mean "I speak BHTP 1.0".

## 4. RESPONSE frame (8 + 139 = 147 bytes)
```
00000000  00 00 8b 02 00 00 00 01  00 c8 07 0a 00 1d 53 75  |..............Su|
00000010  6e 2c 20 30 34 20 4f 63  74 20 32 30 32 36 20 31  |n, 04 Oct 2026 1|
00000020  37 3a 30 38 3a 31 38 20  47 4d 54 00 06 73 65 72  |7:08:18 GMT..ser|
00000030  76 65 72 00 0a 73 65 72  76 65 72 2f 31 2e 30 05  |ver..server/1.0.|
00000040  00 0a 74 65 78 74 2f 70  6c 61 69 6e 06 00 02 31  |..text/plain...1|
00000050  39 08 00 12 22 65 35 36  62 37 36 36 36 32 39 31  |9..."e56b7666291|
00000060  62 65 65 63 30 22 07 00  1d 53 75 6e 2c 20 30 34  |beec0"...Sun, 04|
00000070  20 4f 63 74 20 32 30 32  36 20 31 37 3a 30 32 3a  | Oct 2026 17:02:|
00000080  32 30 20 47 4d 54 09 00  0a 6d 61 78 2d 61 67 65  |20 GMT...max-age|
00000090  3d 36 30                                          |=60|
```
| Offset | Bytes | Field | Value |
|---|---|---|---|
| 0x00 | `00 00 8b` | **Length** | 0x8b = **139** |
| 0x03 | `02` | **Type** | RESPONSE |
| 0x04 | `00` | **Flags** | none. The body follows in DATA frames, so no END_STREAM yet |
| 0x05 | `00 00 01` | **Request ID** | 1, which answers request 1 |
| 0x08 | `00 c8` | status | 0x00c8 = **200** |
| 0x0A | `07` | header count | 7 |
| 0x0B | `0a` · `00 1d` · 29 B | static #10 `date` | `Sun, 04 Oct 2026 17:08:18 GMT` |
| 0x2B | `00` · `06` `73 65 72 76 65 72` · `00 0a` · 10 B | **literal** name, len 6 = `server` | `server/1.0`. This name is not in the static table, so it is spelled out |
| 0x3F | `05` · `00 0a` · 10 B | static #5 `content-type` | `text/plain` |
| 0x4C | `06` · `00 02` · `31 39` | static #6 `content-length` | `19` |
| 0x51 | `08` · `00 12` · 18 B | static #8 `etag` | `"e56b7666291beec0"` |
| 0x66 | `07` · `00 1d` · 29 B | static #7 `last-modified` | `Sun, 04 Oct 2026 17:02:20 GMT` |
| 0x86 | `09` · `00 0a` · 10 B | static #9 `cache-control` | `max-age=60` |

Check: 2 + 1 + 32 + 20 + 13 + 5 + 21 + 32 + 13 = **139** ✓. Six of the seven header names cost 1 byte each. Only `server` paid the literal price (1 + 1 + 6 = 8 bytes).

## 5. DATA frame (8 + 19 = 27 bytes)
```
00000000  00 00 13 03 01 00 00 01  48 65 6c 6c 6f 2c 20 49  |........Hello, I|
00000010  20 61 6d 20 41 72 79 65  6e 2e 0a                 | am Aryen..|
```
| Offset | Bytes | Field | Value |
|---|---|---|---|
| 0x00 | `00 00 13` | **Length** | 0x13 = 19 |
| 0x03 | `03` | **Type** | DATA |
| 0x04 | `01` | **Flags** | END_STREAM. This is the last frame of response 1 |
| 0x05 | `00 00 01` | **Request ID** | 1 |
| 0x08 | `48 … 0a` | body | `Hello, I am Aryen.\n`, the raw file bytes with no escaping |

## 6. Client GOAWAY (8 + 5 = 13 bytes)
```
00000000  00 00 05 07 00 00 00 00  00 00 01 00 00           |.............|
```
| Offset | Bytes | Field | Value |
|---|---|---|---|
| 0x00 | `00 00 05` | **Length** | 5 |
| 0x03 | `07` | **Type** | GOAWAY |
| 0x04 | `00` | **Flags** | none |
| 0x05 | `00 00 00` | **Request ID** | 0, the connection itself |
| 0x08 | `00 00 01` | last_request_id | 1 |
| 0x0B | `00 00` | code | 0 = graceful goodbye, no reason text |

Then the client closes the socket. The server sees the GOAWAY, closes its side, and keeps serving other connections.

---

## Bonus: pipelined 2nd request with REUSE_HEADERS (21 bytes)
When a client pipelines `/hello.txt` and then `/logo.png` on the same connection, request 2 arrives like this:
```
00 00 0d 01 03 00 00 02  01 00 09 2f 6c 6f 67 6f 2e 70 6e 67  00
└─len 13─┘ │  │  └─id 2─┘ │  └─9─┘ └──── "/logo.png" ────┘  └ 0 headers
         REQUEST│         GET
     END_STREAM|REUSE_HEADERS (0x03)
```
It inherits host, user-agent and accept from request 1. HTTP/1.1 would send 84 bytes for the same thing.

## Bonus: a 404 (status bytes only)
```
00 00 58 02 00 00 00 01  01 94 ...       status 0x0194 = 404
```
A DATA frame follows with the text `404 Not Found: /missing.txt\n`. The connection stays open for the next request.
