"""Length-prefixed messages shared by the Windows controller and Docker server."""

import json
import socket
import struct


MAGIC = b"DGST"
TYPE_FRAME = b"F"
TYPE_SUBSCRIBE = b"S"
MAX_FRAME_SIZE = 2 * 1024 * 1024
MAX_CONTROL_MESSAGE_SIZE = 4096


class ProtocolError(ValueError):
    """Raised when a peer sends an invalid or truncated protocol message."""


def receive_exact(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            if not data:
                return None
            raise ProtocolError("Connection closed in the middle of a message.")
        data.extend(chunk)
    return bytes(data)


def encode_message(message_type, payload=b""):
    if message_type not in (TYPE_FRAME, TYPE_SUBSCRIBE):
        raise ProtocolError("Unsupported message type.")
    if message_type == TYPE_FRAME and not 0 < len(payload) <= MAX_FRAME_SIZE:
        raise ProtocolError("Invalid JPEG frame size.")
    if message_type == TYPE_SUBSCRIBE and not 0 < len(payload) <= MAX_CONTROL_MESSAGE_SIZE:
        raise ProtocolError("Invalid subscription size.")
    return MAGIC + message_type + struct.pack("!I", len(payload)) + payload


def encode_json(payload):
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if not encoded or len(encoded) > MAX_CONTROL_MESSAGE_SIZE:
        raise ProtocolError("JSON message has an invalid size.")
    return struct.pack("!I", len(encoded)) + encoded


def receive_json(connection):
    header = receive_exact(connection, 4)
    if header is None:
        raise ConnectionError("Peer closed the connection.")
    size = struct.unpack("!I", header)[0]
    if not 0 < size <= MAX_CONTROL_MESSAGE_SIZE:
        raise ProtocolError("Invalid JSON message size.")
    payload = receive_exact(connection, size)
    if payload is None:
        raise ConnectionError("Peer closed during its response.")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("Invalid JSON message.") from exc


def receive_request(connection, first_header=None):
    """Read one typed request or a legacy 4-byte-length-prefixed JPEG frame."""
    header = first_header if first_header is not None else receive_exact(connection, 4)
    if header is None:
        return None

    legacy = header != MAGIC
    if legacy:
        message_type = TYPE_FRAME
        size = struct.unpack("!I", header)[0]
    else:
        message_type = receive_exact(connection, 1)
        if message_type is None:
            raise ProtocolError("Connection closed before the message type.")
        if message_type not in (TYPE_FRAME, TYPE_SUBSCRIBE):
            raise ProtocolError("Unknown client message type.")
        size_header = receive_exact(connection, 4)
        if size_header is None:
            raise ProtocolError("Connection closed before the message length.")
        size = struct.unpack("!I", size_header)[0]

    max_size = MAX_FRAME_SIZE if message_type == TYPE_FRAME else MAX_CONTROL_MESSAGE_SIZE
    if not 0 < size <= max_size:
        raise ProtocolError("Invalid client message size.")
    payload = receive_exact(connection, size)
    if payload is None:
        raise ProtocolError("Connection closed before the message payload.")

    if message_type == TYPE_SUBSCRIBE:
        try:
            request = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("Invalid subscription request.") from exc
        if request != {"subscribe": "gestures"}:
            raise ProtocolError("Unsupported subscription request.")

    return message_type, payload, legacy
