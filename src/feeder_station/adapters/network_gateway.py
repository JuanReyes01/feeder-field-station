"""The TCP transport to feeder nodes over Wi-Fi (design.md section 4's own
P3 note: "keep the station link behind an interface... so P5 swaps ESP-NOW
or Wi-Fi for LoRa" -- this is that swap's first half, Wi-Fi, chosen over
the bench's own USB cable because the deployment site has Wi-Fi reaching
every feeder and no cable will).

**The decision this module exists to honour, not to re-litigate:** the
node dials out to the station. This gateway only ever listens; it never
opens a connection to anything. A listening station needs no knowledge of
node addresses, survives DHCP churn, and works if a node ever sits behind
NAT -- the same reasoning ``docs/design.md`` and this change's own report
both name.

**Why this file is not ``serial_gateway.py`` with a socket swapped in.**
That gateway is 1:1 -- one port, one ``FrameStream``, one write lock, one
``LinkState``. A listening socket is 1:N, and R6 requires at least ten
traps. Three things follow from that, each load-bearing:

1. **Each accepted connection gets its own ``FrameStream``.** COBS framing
   state belongs to one byte stream; feeding two nodes' bytes into a
   shared decoder produces garbage frames from perfectly correct senders
   the instant their bytes interleave on the wire at the OS's own
   discretion (``tests/adapters/test_network_gateway.py``'s own isolation
   tests prove this holds, not merely a docstring's claim).
2. **Outbound frames are routed by ``node_id``, read out of the frame's
   own header** (``domain/frames.py``'s ``node_id`` field, already present
   on every wire frame this station ever sends) **-- not by which
   connection happened to write last or first.** A node_id with no live
   connection is refused honestly (``write_frame`` returns ``False``,
   exactly as ``SerialGateway`` already refuses a write with no port
   open), never silently dropped while claiming success.
3. **``/health`` reports one row per node, not one boolean for all of
   them.** Folding "is any node connected" into a single flag is exactly
   docs/pending.md's own closing rule -- "state that was true once,
   presented as true now" -- reborn at the transport layer: nine
   disconnected traps and one connected one must never read as "the link
   is fine."

**Identity is bound once, from a connection's first successfully-decoded
frame, and never overridden mid-connection.** This does not authenticate
anything -- nothing here can, without a real scheme
(``docs/protocol/messages.md``'s open question 1, deliberately left
unanswered by this change) -- but it closes the narrower hole of a single
already-open connection claiming to *become* a different node partway
through its own life. See this change's own report for the security
position this does and does not cover.

**The channel budget is untouched by this module on purpose.**
``config.py``'s ``DEFAULT_CHANNEL_BUDGET_FRAMES_PER_S`` still models the
serial cable, not this transport, and this module does not raise it for
Wi-Fi: ``adapters/workers.py::send_due_commands`` still calls
``gateway.write_frame()`` while holding ``registry_lock`` (a documented,
outstanding follow-up, not fixed here), and a stalled TCP peer can block a
write exactly as a stalled serial cable already can -- raising the budget
would only make that lock-holding write happen more often. What this
module *does* add, because a stalled peer is a much more ordinary
occurrence on a network than on a dedicated cable: a bounded write
timeout, so a write against a dead peer eventually fails and frees the
lock instead of blocking on it forever, and a bounded read timeout (keyed
to ``StationConfig.silence_after`` by the constructor's own default, S11's
already-named reachability window) so a half-open connection the OS has
not noticed is retired instead of held forever.

**Remediation (real hardware, site Wi-Fi, 2026-09-26): ``nodes`` is a live
registry, exactly one entry per node ever identified, always the most
recently associated connection -- never a growing history.** Two bugs
surfaced together against a live ESP32 that a real reconnect loop and only
a real reconnect loop could show:

1. ``write_frame()`` used to call ``settimeout()`` on the *same* socket
   object the reader thread's ``recv()`` was blocked on. ``settimeout()``
   is per socket object, not per call and not per direction -- a node
   ACK'd on every heartbeat kept getting retired within the *write*
   timeout of its own ACK, nowhere near the configured read timeout, and
   the retirement message (interpolating the read timeout, not whatever
   actually happened) was flatly false for a connection that had lived
   seconds. Fixed with ``_Connection.write_socket`` -- ``socket.dup()`` of
   the same fd, so read and write each have their own, independent timeout
   state, permanently.
2. ``_associate`` only replaced a node_id's entry when the *old* one was
   still marked connected. A connection that had already retired **on its
   own** (exactly what bug 1 above caused, repeatedly) was never "stale"
   by that check, so it was never replaced -- the fresh, genuinely live
   connection for the same node was tracked nowhere ``/health`` could see
   it, and the dead row stayed forever. Fixed by making the replacement
   unconditional: whichever connection most recently identified itself as
   a given node_id is that node's one and only entry, live or dead.

Also fixed: a connection ``_associate`` force-closes to make room for a
fresher one is now marked ``superseded`` *before* it is closed, so its own
retirement is logged and reported with the real cause, not whatever
``recv()`` happened to raise or return once the socket vanished
underneath it (observed directly: sometimes a clean EOF, sometimes an
unrelated ``OSError`` -- never something a fixed string could describe
correctly by guessing).
"""

from __future__ import annotations

import contextlib
import logging
import queue
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from feeder_station.adapters.serial_gateway import BACKOFF_SCHEDULE_S
from feeder_station.domain import frames
from feeder_station.domain.serial_framing import DELIMITER, FrameStream, cobs_encode

logger = logging.getLogger(__name__)


def _shutdown_and_close(sock: socket.socket) -> None:
    """Close a socket in a way that reliably unblocks a *different* thread
    already sitting in a blocking ``recv()`` on the same fd.

    Linux (and POSIX generally) does not guarantee that ``close()`` alone
    wakes a concurrent blocking read on the same file descriptor from
    another thread -- the read can keep waiting on a descriptor that is,
    from every other thread's point of view, already gone. ``shutdown()``
    does not have that ambiguity: it tears down the connection itself
    (sending a FIN/RST to the peer) rather than merely dropping this
    process's reference to the fd, so a thread blocked in ``recv()`` on it
    wakes immediately with ``b""`` or an ``OSError``. Discovered directly
    while writing ``test_a_reconnecting_node_supersedes_its_old_connection``:
    without the ``shutdown()`` call below, the superseded connection's own
    reader thread stayed blocked on its ``recv()`` until its full read
    timeout elapsed, and the client on the other end never saw so much as
    an EOF in the meantime.
    """
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


def _force_close_connection(conn: _Connection) -> None:
    """Tear down one connection's *pair* of socket objects (see
    ``_Connection.__init__`` for why there are two) -- ``shutdown()`` on
    either affects the one underlying OS connection they both wrap, waking
    a blocked ``recv()`` on ``conn.socket`` no matter which of the two
    duplicated descriptors this call is invoked through; both still need
    their own ``close()`` to release their own fd.
    """
    _shutdown_and_close(conn.socket)
    with contextlib.suppress(OSError):
        conn.write_socket.close()

# Arbitrary, and named as such: docs/protocol/messages.md pins no port for
# any transport (it is transport-agnostic by its own opening line). Chosen
# clear of the HTTP port (8000, cli.py's own default) and of the IANA
# well-known range.
DEFAULT_TCP_PORT = 8765

# S11's own "unreachable after three missed beats" window (config.py,
# DEFAULT_SILENCE_AFTER = 15 minutes) -- reused here rather than inventing a
# second unrelated timeout: a connection this quiet already has a named
# meaning station-wide, "gone", and a half-open socket that never delivers
# so much as one HEARTBEAT within that same window is exactly that.
# Imported as a float, not the module, to avoid this adapter depending on
# StationConfig's own CLI/env parsing for a single number.
DEFAULT_READ_TIMEOUT_S = 15.0 * 60.0

# A serial write has no timeout at all (adapters/serial_gateway.py's own
# `_default_open_port`) -- the module docstring above explains why this
# transport must not repeat that shape: a stalled TCP peer is the ordinary
# case here, not the rare one, and `send_due_commands` still sends under
# `registry_lock`.
DEFAULT_WRITE_TIMEOUT_S = 2.0

# How long `read_frames()` blocks waiting for at least one frame before
# returning empty-handed -- matches `adapters/serial_gateway.py`'s own
# `_default_open_port(timeout=1.0)`, so `run_gateway_loop`'s polling cadence
# (and CPU behaviour) is identical regardless of which transport is active.
DEFAULT_POLL_TIMEOUT_S = 1.0


@dataclass(frozen=True)
class NetworkGatewayConfig:
    """What the operator told the station about its own listening socket.

    ``host="0.0.0.0"`` matches ``cli.py``'s own HTTP bind (S10: "binds
    every interface on purpose... the operator reaches it from a phone on
    the local network") -- the same LAN trust model this station already
    has, named explicitly here rather than left an accidental default (see
    this change's own report for what that trust model does and does not
    cover for an unauthenticated node link).
    """

    host: str = "0.0.0.0"
    port: int = DEFAULT_TCP_PORT


@dataclass(frozen=True)
class NodeLinkState:
    """One row of what ``/health`` reports about one node's connection.

    Never omitted for a node this station has ever identified, even once
    its connection is gone -- ``connected=False`` is the honest row,
    silently dropping the node from the response is docs/pending.md's own
    "state that was true once, presented as true now" defect wearing a
    different shape (here: presented as *never true*, which is just as
    dishonest as presenting it as still true).
    """

    node_id: int
    connected: bool
    peer_address: str | None
    since: float | None
    frames_in: int
    frames_out: int
    decode_errors: int
    last_error: str | None


@dataclass(frozen=True)
class NetworkLinkState:
    """Everything ``/health`` needs about the *listening* transport and
    every node it has ever identified -- the 1:N sibling of
    ``SerialGateway``'s own ``LinkState``, deliberately a different shape
    rather than one boolean pretending to summarise ``nodes`` (module
    docstring, point 3).
    """

    listening: bool
    host: str
    port: int
    since: float | None
    unidentified_connections: int
    payload_errors: int
    nodes: tuple[NodeLinkState, ...]
    last_error: str | None


class _Connection:
    """One accepted socket: its own frame stream, its own write lock, and
    the one fact that makes outbound routing possible at all -- which
    ``node_id``, if any, has identified itself on this connection so far.

    **Two socket objects, one OS connection.** ``socket.settimeout()`` is
    per Python socket *object*, not per call and not per direction --
    measured live: ``write_frame()`` setting the (short) write timeout on
    the same object the reader thread's ``recv()`` was blocked on silently
    shortened that thread's *next* read wait too, retiring a perfectly
    healthy connection seconds after its own ACK. ``socket.dup()`` gives
    the write path its own file descriptor, wrapping the same underlying
    connection, with its own independent timeout -- the reader's 900-second
    wait can never again be governed by the writer's 2-second one.
    """

    def __init__(self, sock: socket.socket, address: tuple[str, int], *, since: float) -> None:
        self.socket = sock  # read side -- the reader thread's own recv()
        self.write_socket = sock.dup()  # write side -- write_frame()'s own sendall()
        self.address = address
        self.since = since
        self.node_id: int | None = None
        self.frame_stream = FrameStream()
        self.write_lock = threading.Lock()
        self.frames_in = 0
        self.frames_out = 0
        self.last_error: str | None = None
        self.connected = True
        # Set by `_associate` right before it force-closes this connection
        # to make room for a fresher one for the same node_id -- read by
        # `_reader_loop`'s own `finally` block so the *reason* recorded
        # reflects the real cause, not whatever `recv()` happened to raise
        # or return once the socket was closed out from under it.
        self.superseded = False


class NetworkGateway:
    """Owns the one listening socket, every connection accepted on it, and
    the ``node_id -> connection`` routing that makes outbound writes
    possible (module docstring, point 2).

    ``sleep`` and ``clock`` are injected for the same reason
    ``SerialGateway`` injects them (DD9): the bind-retry backoff state
    machine runs in microseconds under test instead of real seconds.
    """

    def __init__(
        self,
        config: NetworkGatewayConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        write_timeout_s: float = DEFAULT_WRITE_TIMEOUT_S,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    ) -> None:
        self._config = config
        self._sleep = sleep
        self._clock = clock
        self._read_timeout_s = read_timeout_s
        self._write_timeout_s = write_timeout_s
        self._poll_timeout_s = poll_timeout_s

        self._listener: socket.socket | None = None
        self._since: float | None = None
        self._last_error: str | None = None
        self._stop = threading.Event()

        # One lock guards both of the structures below -- the accept loop
        # appending a new connection, a reader thread retiring one, and
        # `_associate` moving one from `_unidentified` into `_identified`
        # (or replacing an entry already there) all touch them, the same
        # "one small lock around shared mutable state" shape `SerialGateway`
        # already gives its own single write lock.
        self._lock = threading.Lock()
        # `nodes` (module docstring, point 3) *is* this dict's values,
        # verbatim: a live registry, exactly one entry per node_id this
        # station has ever identified, always the most recently associated
        # connection -- never a growing history of every past connection,
        # and never emptied out from under a node that is still, or is
        # once again, actually connected. `_associate` is the one place
        # that writes to it; nothing else in this class ever removes a key.
        self._identified: dict[int, _Connection] = {}
        # Connections accepted but not yet claimed by any frame's node_id
        # -- `unidentified_connections` (module docstring, point 3) is
        # this list's own live count, never folded into `nodes` (there is
        # no node_id to key a row by yet).
        self._unidentified: list[_Connection] = []

        self._queue: queue.Queue[bytes] = queue.Queue()
        self._payload_errors = 0

    @property
    def bound_port(self) -> int | None:
        """The OS-assigned port once bound -- only ever different from
        ``config.port`` when the operator (or a test) asked for port 0.
        """
        if self._listener is None:
            return None
        return int(self._listener.getsockname()[1])

    @property
    def link_state(self) -> NetworkLinkState:
        with self._lock:
            identified_snapshot = list(self._identified.values())
            unidentified_count = sum(1 for c in self._unidentified if c.connected)
        nodes = tuple(
            sorted(
                (
                    NodeLinkState(
                        node_id=c.node_id,
                        connected=c.connected,
                        peer_address=f"{c.address[0]}:{c.address[1]}",
                        since=c.since,
                        frames_in=c.frames_in,
                        frames_out=c.frames_out,
                        decode_errors=c.frame_stream.decode_errors,
                        last_error=c.last_error,
                    )
                    for c in identified_snapshot
                    if c.node_id is not None
                ),
                key=lambda n: n.node_id,
            )
        )
        return NetworkLinkState(
            listening=self._listener is not None,
            host=self._config.host,
            port=self._config.port,
            since=self._since,
            unidentified_connections=unidentified_count,
            payload_errors=self._payload_errors,
            nodes=nodes,
            last_error=self._last_error,
        )

    def record_payload_error(self) -> None:
        """WARNING C's own callback shape (``adapters/ingest.py``), reused
        unchanged: a payload-level decode failure cannot always be
        attributed to one connection by the time it is caught (a
        malformed header may not even have a readable ``node_id``), so
        this stays one station-wide counter, exactly as
        ``SerialGateway.record_payload_error`` already is.
        """
        self._payload_errors += 1

    def ensure_connected(self, *, stop: Callable[[], bool] = lambda: False) -> bool:
        """Bind and start accepting, retrying with the same capped backoff
        ``SerialGateway`` uses for device resolution -- "no device found"
        and "the configured port would not bind" are the same shape of
        fact: the link is down, and the gateway never pretends otherwise.
        """
        if self._listener is not None:
            return True

        attempt = 0
        while not stop():
            if self._bind_once():
                return True
            delay = BACKOFF_SCHEDULE_S[min(attempt, len(BACKOFF_SCHEDULE_S) - 1)]
            self._sleep(delay)
            attempt += 1
        return False

    def _bind_once(self) -> bool:
        try:
            listener = socket.create_server((self._config.host, self._config.port))
        except OSError as exc:
            self._last_error = str(exc)
            return False

        # A timeout on the listener itself, not reliance on `close()` alone
        # to unblock a concurrent `accept()`: the same Linux quirk
        # `_shutdown_and_close`'s own docstring names for `recv()` applies
        # here too (closing an fd from a different thread than the one blocked
        # in a syscall on it is not guaranteed to wake that syscall) -- the
        # accept loop instead wakes on its own, at most once a second, and
        # checks `self._stop` itself.
        listener.settimeout(1.0)
        self._listener = listener
        self._since = self._clock()
        self._last_error = None
        threading.Thread(
            target=self._accept_loop, name="feeder-station-tcp-accept", daemon=True
        ).start()
        return True

    def _accept_loop(self) -> None:
        listener = self._listener
        assert listener is not None
        while not self._stop.is_set():
            try:
                sock, address = listener.accept()
            except TimeoutError:
                continue  # the 1s wake-up above -- re-check `self._stop`
            except OSError:
                return  # the listener was closed underneath us (shutdown)
            conn = _Connection(sock, address, since=self._clock())
            with self._lock:
                self._unidentified.append(conn)
            threading.Thread(
                target=self._reader_loop,
                args=(conn,),
                name=f"feeder-station-tcp-{address[0]}:{address[1]}",
                daemon=True,
            ).start()

    def _reader_loop(self, conn: _Connection) -> None:
        # One `try` around the whole body, not just `recv()`: `settimeout()`
        # itself can raise `OSError` too (a socket another thread already
        # closed underneath this one, e.g. `_associate`'s own supersede
        # path racing this thread's own startup) -- `TimeoutError` is a
        # subclass of `OSError`, so the specific case is caught first, and
        # everything else this socket can raise is still handled, never
        # left to crash the thread and skip `_retire()`.
        #
        # `organic_error` is decided *here*, from what `recv()` itself
        # said; the actual `conn.last_error` this connection is retired
        # with is decided once, in `finally`, below -- never inline in
        # these `except` clauses. Reported live: a connection this station
        # force-closed itself (`_associate`'s own supersede path) could
        # still land in either branch below depending on exactly how the
        # closed socket's `recv()` call happened to unwind, and whichever
        # one it landed in wrote a message that was flatly false for a
        # connection seconds old. `conn.superseded` -- set by `_associate`
        # *before* it ever closes anything -- is the one fact that cannot
        # be ambiguous, so it is checked first and wins outright.
        sock = conn.socket
        organic_error: str | None = None
        try:
            sock.settimeout(self._read_timeout_s)
            while not self._stop.is_set():
                chunk = sock.recv(4096)
                if not chunk:
                    break  # the peer closed cleanly (FIN -> recv() returns b"")
                blocks = conn.frame_stream.feed(chunk)
                conn.frames_in += len(blocks)
                for block in blocks:
                    self._route_inbound(conn, block)
        except TimeoutError:
            organic_error = (
                f"no bytes for {self._read_timeout_s:.0f}s -- a silent "
                "half-open connection the OS never reported as closed, "
                "retired instead of held forever"
            )
        except OSError as exc:
            organic_error = str(exc)
        finally:
            if conn.superseded:
                conn.last_error = (
                    "reconnected on a new connection -- the station closed "
                    "this socket to replace it with a fresher one for the "
                    "same node, not because it went silent"
                )
            elif organic_error is not None:
                conn.last_error = organic_error
            logger.info(
                "network gateway: connection from %s (node %s) retired: %s",
                conn.address,
                conn.node_id,
                conn.last_error or "the peer closed the connection cleanly",
            )
            self._retire(conn)

    def _route_inbound(self, conn: _Connection, block: bytes) -> None:
        # Handed to `run_gateway_loop` unchanged, exactly as
        # `SerialGateway.read_frames()` already does -- `ingest_frame` is
        # the one place that decodes the header authoritatively and counts
        # a bad one via `record_payload_error()`. The decode below is a
        # second, best-effort attempt at the one extra fact this gateway
        # itself needs and `ingest_frame` never reports back: which
        # `node_id` this connection belongs to.
        self._queue.put(block)
        try:
            frame = frames.decode(block)
        except frames.FrameError:
            return
        if conn.node_id is None:
            self._associate(conn, frame.node_id)
        elif conn.node_id != frame.node_id:
            # Identity is fixed to a connection's first frame and never
            # overridden mid-stream (module docstring) -- this is the one
            # check this transport can make with no crypto at all: a
            # single already-open connection is not allowed to *become* a
            # different node partway through its own life.
            logger.warning(
                "network gateway: connection from %s already identified as "
                "node %s, ignoring a frame claiming node %s",
                conn.address,
                conn.node_id,
                frame.node_id,
            )

    def _associate(self, conn: _Connection, node_id: int) -> None:
        """Claim ``node_id`` for ``conn`` -- the *only* place ``self.
        _identified`` is ever written. Unconditionally replaces whatever
        was there before, live or already dead: the bug this fixes is
        exactly the old, narrower version of this method only replacing a
        prior entry when it was still marked ``connected`` -- a connection
        that had *already* retired on its own (this station's own read
        timeout, a clean close, anything) before the replacement ever
        connected left its dead row in place forever, because nothing
        found it "stale" enough to remove. One node_id, one entry, always
        the most recent connection -- no condition on the entry it
        replaces required or checked.
        """
        stale: _Connection | None = None
        with self._lock:
            if conn in self._unidentified:
                self._unidentified.remove(conn)
            existing = self._identified.get(node_id)
            if existing is not None and existing is not conn:
                stale = existing
            self._identified[node_id] = conn
            conn.node_id = node_id
        if stale is not None and stale.connected:
            # Only a connection that is *still* live needs an active
            # close -- one that already retired on its own has nothing
            # left to close, and must keep whatever honest reason it
            # already recorded for its own, earlier, unrelated end.
            stale.superseded = True
            logger.info(
                "network gateway: node %s reconnected on a new connection -- "
                "closing the station's old socket rather than leaving it open "
                "and silently unwritable",
                node_id,
            )
            _force_close_connection(stale)

    def _retire(self, conn: _Connection) -> None:
        """Mark ``conn`` dead and drop it from whichever structure still
        holds it. An **unidentified** connection is removed outright --
        nothing else will ever look for it, there being no node_id to key
        it by. An **identified** connection is deliberately left in
        ``self._identified`` -- removing it would erase the one row
        ``/health`` has for that node the instant it goes quiet, the exact
        opposite of the "still show it, honestly marked disconnected"
        contract ``NodeLinkState`` documents. `_associate` is the only
        thing that ever replaces an identified entry, later, for the same
        node_id -- never this method.
        """
        _force_close_connection(conn)
        conn.connected = False
        with self._lock:
            if conn.node_id is None and conn in self._unidentified:
                self._unidentified.remove(conn)

    def read_frames(self, *, size: int = 4096) -> list[bytes]:
        """Drain every frame block any connection has produced since the
        last call. ``size`` is accepted, never used -- kept only so this
        method's shape matches ``SerialGateway.read_frames`` exactly, the
        one call site (``adapters/workers.py::run_gateway_loop``) shared by
        both transports.

        Blocks up to ``poll_timeout_s`` waiting for the first item, then
        drains whatever else is already queued without waiting further --
        the same cadence a serial port's own ``timeout=1.0`` blocking read
        already gives that loop, so CPU behaviour does not change with the
        transport.
        """
        blocks: list[bytes] = []
        try:
            blocks.append(self._queue.get(timeout=self._poll_timeout_s))
        except queue.Empty:
            return blocks
        while True:
            try:
                blocks.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return blocks

    def write_frame(self, raw_frame: bytes) -> bool:
        """Route by the frame's own ``node_id`` (module docstring, point
        2). ``False`` -- never a fabricated success -- when the header
        cannot even be read, or no live connection is routed for that
        node.

        Writes through ``conn.write_socket`` -- the duplicated fd
        (``_Connection``'s own docstring) -- never ``conn.socket``, so
        this call's own ``settimeout()`` can never shorten the reader
        thread's unrelated, much longer wait on the same connection.
        """
        try:
            frame = frames.decode(raw_frame)
        except frames.FrameError:
            return False

        with self._lock:
            conn = self._identified.get(frame.node_id)
        if conn is None or not conn.connected:
            return False

        block = cobs_encode(raw_frame) + bytes([DELIMITER])
        with conn.write_lock:
            try:
                conn.write_socket.settimeout(self._write_timeout_s)
                conn.write_socket.sendall(block)
            except OSError as exc:
                conn.last_error = str(exc)
                self._retire(conn)
                return False
            conn.frames_out += 1
            return True

    def close(self) -> None:
        """Stop accepting, close every connection, and close the listener
        itself. Closing sockets out from under blocked ``accept()``/
        ``recv()`` calls is what unblocks those threads (both raise
        ``OSError`` on a closed fd) -- ``self._stop`` alone would not, since
        neither call polls it.
        """
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            with contextlib.suppress(OSError):
                listener.close()
        with self._lock:
            conns = list(self._identified.values()) + list(self._unidentified)
            self._identified.clear()
            self._unidentified.clear()
        for conn in conns:
            _force_close_connection(conn)
        self._since = None
