"""COBS framing for the byte stream a USB-serial link delivers.

The v1 protocol's frame (``domain.frames``) has no delimiter and no
checksum: fine over ESP-NOW, which hands the receiver a whole packet, but
not over USB-CDC, which hands the operating system an unbounded byte
stream with no packet boundaries (exploration.md, section 2). Consistent
Overhead Byte Stuffing (COBS) removes every ``0x00`` byte from an encoded
block, so a single ``0x00`` can serve as an unambiguous frame delimiter —
and, the reason it is used here rather than a simpler escaping scheme, a
reader that loses synchronisation to a corrupted or truncated block can
always recover by scanning forward to the next ``0x00`` without needing to
understand the bytes in between.

Two independent things live here:

- ``cobs_encode``/``cobs_decode``: the block codec itself. It operates on
  one already-delimited block — the caller appends the wire delimiter (one
  ``0x00``) after ``cobs_encode``'s output, and strips it before handing a
  block to ``cobs_decode``.
- ``FrameStream``: the byte-in/blocks-out state machine that turns an
  unbounded, arbitrarily-chunked byte stream into a sequence of decoded
  blocks.

COBS proves the framing is intact; it is not a checksum. A corrupted block
that still happens to satisfy COBS's own length-code bookkeeping decodes
without complaint (S9's link-integrity trace is scoped to framing only —
content corruption is the protocol's own retry/ACK machinery's concern, not
this module's).
"""

from __future__ import annotations

from .frames import HEADER_BYTES, PAYLOAD_MAX

DELIMITER = 0x00

# The largest raw frame domain.frames can ever produce: a 9-byte header plus
# a 200-byte payload. COBS adds at most one overhead byte per 254 bytes of
# input, so an encoded block carrying a frame this size never needs more
# than one byte over MAX_FRAME_BYTES.
MAX_FRAME_BYTES = HEADER_BYTES + PAYLOAD_MAX
MAX_ENCODED_BLOCK_BYTES = MAX_FRAME_BYTES + (MAX_FRAME_BYTES // 0xFE) + 1


class CobsError(ValueError):
    """A block that is not well-formed COBS output."""


def cobs_encode(data: bytes) -> bytes:
    """Encode ``data`` into a COBS block containing no ``0x00`` byte.

    The caller appends the wire delimiter after this; COBS only guarantees
    the block's own bytes never collide with it.
    """
    output = bytearray()
    output.append(0)  # placeholder for the first block's length code
    code_index = 0
    code = 1

    for byte in data:
        if byte != DELIMITER:
            output.append(byte)
            code += 1
        else:
            output[code_index] = code
            code_index = len(output)
            output.append(0)  # placeholder for the next block's length code
            code = 1
        if code == 0xFF:
            output[code_index] = code
            code_index = len(output)
            output.append(0)
            code = 1

    output[code_index] = code
    return bytes(output)


def cobs_decode(data: bytes) -> bytes:
    """Decode one COBS block back into its original bytes.

    Raises ``CobsError`` when a length code claims more bytes than remain,
    or is itself ``0x00`` — either means ``data`` is not well-formed COBS
    output, whether from corruption or from being handed a block fragment.
    """
    output = bytearray()
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        if code == DELIMITER:
            raise CobsError("a length code of 0x00 cannot appear inside a COBS block")
        i += 1
        end = i + code - 1
        if end > n:
            raise CobsError(f"length code {code} claims more bytes than the {n - i} remaining")
        output += data[i:end]
        i = end
        if code < 0xFF and i != n:
            output.append(DELIMITER)
    return bytes(output)


class FrameStream:
    """Turns an unbounded, arbitrarily-chunked byte stream into frame blocks.

    ``feed()`` buffers bytes until the next delimiter, decodes the block
    found between two delimiters, and returns every block that decoded
    cleanly from this call's chunk.

    The invariant this holds, and the entire reason COBS is used instead of
    a plain length-prefixed frame: a block that fails — because it was
    corrupted, truncated, or grew past ``max_block_bytes`` without a
    delimiter ever arriving — is dropped and counted in ``decode_errors``,
    never raised, and never prevents the next delimiter-bounded block from
    decoding correctly. A reader can always resynchronise at the next
    ``0x00`` no matter what came before it.
    """

    def __init__(self, *, max_block_bytes: int = MAX_ENCODED_BLOCK_BYTES) -> None:
        self._max_block_bytes = max_block_bytes
        self._buffer = bytearray()
        self._discarding = False
        self.decode_errors = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[bytes]:
        blocks: list[bytes] = []
        for byte in chunk:
            if byte == DELIMITER:
                block = bytes(self._buffer)
                self._buffer.clear()
                if self._discarding:
                    self._discarding = False
                else:
                    self._decode_into(block, blocks)
                continue

            if self._discarding:
                continue

            self._buffer.append(byte)
            if len(self._buffer) > self._max_block_bytes:
                self.decode_errors += 1
                self._buffer.clear()
                self._discarding = True

        return blocks

    def _decode_into(self, block: bytes, blocks: list[bytes]) -> None:
        if not block:
            return  # a bare delimiter: inter-frame padding, not an error
        try:
            blocks.append(cobs_decode(block))
        except CobsError:
            self.decode_errors += 1
