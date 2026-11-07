"""What each stored event means, in the operator's words."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from feeder_station.domain import payloads
from feeder_station.domain.events import (
    OPERATOR_ARM,
    OPERATOR_CANCEL_COMMAND,
    OPERATOR_CAPTURE_ACKNOWLEDGED,
    OPERATOR_DISARM,
    OPERATOR_SET_TARGETS,
    STATION_TIME_SYNC_WITHHELD,
)

KIND_MSG_TYPES: dict[str, tuple[int, ...]] = {
    "reads": (payloads.READS,),
    "captures": (payloads.CAPTURED, OPERATOR_CAPTURE_ACKNOWLEDGED),
    "commands": (
        OPERATOR_ARM,
        OPERATOR_DISARM,
        OPERATOR_SET_TARGETS,
        OPERATOR_CANCEL_COMMAND,
        payloads.TARGETS,
    ),
    "faults": (payloads.FAULT, STATION_TIME_SYNC_WITHHELD),
    "connections": (payloads.HELLO,),
}
LOGGED_MSG_TYPES: tuple[int, ...] = tuple(t for types in KIND_MSG_TYPES.values() for t in types)

# components/station_proto/station_proto.h, numbered there.
_FAULT_TEXT = {
    0: "El lector no responde",
    1: "El servo se atascó",
    2: "Falla en la memoria SD",
    3: "La trampa no tiene hora",
    4: "No alcanza el módulo de disparo",
    5: "La trampa rechazó armar",
    6: "La lista de interés es demasiado grande",
    7: "Canal de radio distinto en el disparo",
    8: "El armado expiró",
    9: "No se pudo verificar la expiración del armado",
    10: "Se reinició mientras armaba: revisa la alimentación del servo",
}


@dataclass(frozen=True)
class LogEntry:
    kind: str
    text: str


def _fault_text(fields: Mapping[str, object]) -> str:
    code = fields.get("code")
    base = _FAULT_TEXT.get(code, f"Fallo {code}") if isinstance(code, int) else "Fallo"
    detail = fields.get("detail")
    return f"{base}: {detail}" if isinstance(detail, str) and detail else base


def describe(msg_type: int, fields: Mapping[str, object], who: str) -> LogEntry | None:
    """``who`` is how the event's tag should read. ``None`` for what is not news."""
    if msg_type == payloads.READS:
        return LogEntry("reads", f"Lectura de {who}")
    if msg_type == payloads.CAPTURED:
        return LogEntry("captures", f"Se soltó por {who}")
    if msg_type == OPERATOR_CAPTURE_ACKNOWLEDGED:
        result = fields.get("result")
        text = "Resultado: sin colibrí" if result == "sin_colibri" else "Resultado: capturado"
        return LogEntry("captures", text)
    if msg_type == payloads.FAULT:
        return LogEntry("faults", _fault_text(fields))
    if msg_type == STATION_TIME_SYNC_WITHHELD:
        return LogEntry("faults", "Hora no enviada: el reloj de la estación no es confiable")
    if msg_type == payloads.HELLO:
        return LogEntry(
            "connections", f"La trampa se conectó (arranque {fields.get('boot_count')})"
        )
    if msg_type == OPERATOR_ARM:
        if fields.get("pause") is False:
            return LogEntry("commands", "Activar todas: armar enviado")
        return LogEntry("commands", "Armar: enviado")
    if msg_type == OPERATOR_DISARM:
        if fields.get("test_fire"):
            return LogEntry("commands", "Disparo de prueba: enviado")
        if fields.get("pause"):
            return LogEntry("commands", "Trampas desactivadas: desarmar enviado")
        return LogEntry("commands", "Desarmar para mantenimiento: enviado")
    if msg_type == OPERATOR_CANCEL_COMMAND:
        return LogEntry("commands", "Comando cancelado")
    if msg_type == OPERATOR_SET_TARGETS:
        tags = fields.get("tags")
        count = len(tags) if isinstance(tags, list) else 0
        return LogEntry("commands", f"Lista de interés enviada: {count} sujetos")
    if msg_type == payloads.TARGETS:
        return LogEntry(
            "commands", f"La trampa reportó su lista, versión {fields.get('targets_version')}"
        )
    return None
