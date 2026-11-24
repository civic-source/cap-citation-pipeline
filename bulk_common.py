"""Small deterministic contracts shared by retained-source operations."""

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Literal


@dataclass(frozen=True)
class Ok[T]:
    value: T
    ok: Literal[True] = True


@dataclass(frozen=True)
class Err:
    error: str
    ok: Literal[False] = False


type Result[T] = Ok[T] | Err


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


@dataclass(frozen=True)
class Blob:
    sha256: str
    byte_length: int

    def __post_init__(self):
        if (
            not re.fullmatch(r"[a-f0-9]{64}", self.sha256)
            or type(self.byte_length) is not int
            or self.byte_length < 0
        ):
            raise ValueError("Invalid artifact identity")

    @classmethod
    def of(cls, value: bytes) -> "Blob":
        return cls(hashlib.sha256(value).hexdigest(), len(value))

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def load_json(raw: str | bytes) -> object:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("Non-finite JSON number")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
