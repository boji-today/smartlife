import base64
import json
import os
import struct


def split_page(result):
    if isinstance(result, list):
        return result, None
    if isinstance(result, dict):
        chunk = None
        for key in ("items", "entities", "states"):
            if isinstance(result.get(key), list):
                chunk = result[key]
                break
        if chunk is None:
            raise ConnectionError("unexpected ha page")
        cursor = result.get("next") or result.get("next_page") or result.get("cursor")
        if cursor in ("", None, 0, False):
            return chunk, None
        return chunk, cursor
    raise ConnectionError("unexpected ha result")


def _ws_send(sock, opcode, payload):
    mask = os.urandom(4)
    n = len(payload)
    header = bytearray([0x80 | opcode])
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", n))
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def _ws_handshake(sock, host, port):
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            "GET /api/websocket HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode()
    )
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("websocket handshake closed")
        buf.extend(chunk)
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    status = head.decode().split("\r\n", 1)[0]
    if " 101 " not in status:
        raise ConnectionError("websocket upgrade failed")
    return bytearray(rest)


def _ws_recv(sock, pending):
    def recv_exact(n):
        while len(pending) < n:
            chunk = sock.recv(max(4096, n - len(pending)))
            if not chunk:
                raise ConnectionError("websocket closed")
            pending.extend(chunk)
        out = bytes(pending[:n])
        del pending[:n]
        return out

    parts = []
    while True:
        b0, b1 = recv_exact(2)
        fin = b0 & 0x80
        op = b0 & 0x0F
        length = b1 & 0x7F
        if b1 & 0x80:
            raise ConnectionError("server frame is masked")
        if length == 126:
            length = struct.unpack("!H", recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", recv_exact(8))[0]
        data = recv_exact(length) if length else b""
        if op == 0x8:
            return None
        if op == 0x9:
            _ws_send(sock, 0xA, data)
            continue
        if op == 0xA:
            continue
        if op in (0x1, 0x0):
            parts.append(data)
        if fin and parts:
            return b"".join(parts).decode()


def _ws_json(sock, pending):
    raw = _ws_recv(sock, pending)
    if raw is None:
        raise ConnectionError("websocket closed")
    return json.loads(raw)
