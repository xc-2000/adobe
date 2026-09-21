from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


class CookieFileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CookieRecord:
    line_number: int
    fingerprint: str
    names: frozenset[str]
    _value: str = field(repr=False)

    @property
    def value(self) -> str:
        return self._value

    @property
    def has_ims_sid(self) -> bool:
        return "ims_sid" in self.names

    def safe_summary(self) -> dict[str, int | str | bool]:
        return {
            "line_number": self.line_number,
            "fingerprint": self.fingerprint,
            "cookie_count": len(self.names),
            "has_ims_sid": self.has_ims_sid,
        }


def _parse_cookie_line(raw: str, line_number: int) -> CookieRecord:
    value = raw.strip()
    if not value:
        raise CookieFileError(f"line {line_number}: empty cookie")

    names: set[str] = set()
    for part in value.split(";"):
        name, separator, cookie_value = part.strip().partition("=")
        if not separator or not name or not cookie_value:
            raise CookieFileError(f"line {line_number}: invalid Cookie header")
        names.add(name)

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return CookieRecord(
        line_number=line_number,
        fingerprint=f"sha256:{digest}",
        names=frozenset(names),
        _value=value,
    )


def load_cookie_file(path: str | Path) -> list[CookieRecord]:
    cookie_path = Path(path).expanduser()
    if not cookie_path.is_file():
        raise CookieFileError(f"cookie file not found: {cookie_path}")

    records: list[CookieRecord] = []
    with cookie_path.open(encoding="utf-8-sig") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if raw.strip():
                records.append(_parse_cookie_line(raw, line_number))

    if not records:
        raise CookieFileError("cookie file contains no records")
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a Cookie header file safely")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    records = load_cookie_file(args.path)
    print(json.dumps([item.safe_summary() for item in records], ensure_ascii=False))


if __name__ == "__main__":
    main()
