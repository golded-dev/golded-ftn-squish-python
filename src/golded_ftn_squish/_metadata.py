"""Local MSG-compatible address and FTSC date rules; no MSG dependency."""

import re
from collections.abc import Sequence
from datetime import datetime

from golded_ftn import ControlLine, FtnAddress

MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def uint16(value: int) -> int:
    if not 0 <= value <= 65535:
        raise ValueError(f"Value outside unsigned 16-bit range: {value}")
    return value


def parse_date(raw: bytes) -> datetime | None:
    try:
        match = re.fullmatch(
            r"(\d{1,2}) ([A-Za-z]{3}) (\d{2})\s+(\d{2}):(\d{2}):(\d{2})",
            raw.decode("ascii").strip(),
        )
        if match is None:
            return None
        day, month, year, hour, minute, second = match.groups()
        month_number = next(
            i for i, name in enumerate(MONTHS, 1) if name.lower() == month.lower()
        )
        y = int(year)
        return datetime(
            2000 + y if y <= 69 else 1900 + y,
            month_number,
            int(day),
            int(hour),
            int(minute),
            int(second),
        )
    except (ValueError, UnicodeError, StopIteration):
        return None


class AddressError(ValueError):
    def __init__(self, message: str, offset: int) -> None:
        super().__init__(message)
        self.offset = offset


def resolve_addresses(
    words: dict[str, int], controls: Sequence[tuple[ControlLine, int]]
) -> tuple[FtnAddress | None, FtnAddress | None]:
    parts: dict[str, list[int | None]] = {}
    for side in ("from", "to"):
        zone, net, node, point = (
            words[f"{side}_{key}"] for key in ("zone", "net", "node", "point")
        )
        # A nonzero net establishes the header node, including coordinator node 0.
        # Zero zone/net/point words remain absent for legacy kludge supplementation.
        parts[side] = [
            zone or None,
            net or None,
            node if node or net else None,
            point or None,
        ]

    declared: dict[tuple[str, int], int] = {}

    def merge(
        side: str, values: Sequence[int], offset: int, *, point_only: bool = False
    ) -> None:
        for index, value in enumerate(values):
            if point_only and index != 3:
                continue
            key = (side, index)
            if key in declared and declared[key] != value:
                raise AddressError(f"Conflicting {side} address kludges", offset)
            declared[key] = value
            try:
                uint16(value)
            except ValueError as error:
                raise AddressError(str(error), offset) from error
            old = parts[side][index]
            if old is not None and old != value:
                raise AddressError(f"Conflicting {side} address metadata", offset)
            parts[side][index] = value

    for control, control_offset in controls:
        if control.name.upper() == "INTL":
            tokens = control.value.split()
            if len(tokens) != 2:
                continue
            addresses = [FtnAddress.try_from_string(token) for token in tokens]
            if any(
                address is None
                or address.domain is not None
                or address.point is not None
                for address in addresses
            ):
                continue
            for side, address in zip(("to", "from"), addresses, strict=True):
                assert address is not None
                merge(side, (address.zone, address.net, address.node), control_offset)
        elif control.name.upper() in ("FMPT", "TOPT") and re.fullmatch(
            r"[0-9]+", control.value
        ):
            side = "from" if control.name.upper() == "FMPT" else "to"
            merge(side, (0, 0, 0, int(control.value)), control_offset, point_only=True)
    result: list[FtnAddress | None] = []
    for side in ("from", "to"):
        resolved_zone, resolved_net, resolved_node, resolved_point = parts[side]
        result.append(
            FtnAddress(
                zone=resolved_zone,
                net=resolved_net,
                node=resolved_node,
                point=resolved_point or None,
            )
            if resolved_zone is not None
            and resolved_net is not None
            and resolved_node is not None
            else None
        )
    return result[0], result[1]
