"""Semantic versions and version constraints (dependency-free).

Supported constraint syntax (comma separated clauses are AND-ed)::

    *            any version
    1.2.3        exactly 1.2.3   (same as ==1.2.3)
    ==1.2 !=1.3  >=1.2  >1.2  <=2  <2
    ^1.2.3       >=1.2.3,<2.0.0  (caret: compatible with; ^0.2.3 -> <0.3.0)
    ~1.2.3       >=1.2.3,<1.3.0  (tilde: patch-level changes)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering
from typing import ClassVar

from .errors import VersionError

__all__ = ["Version", "VersionConstraint"]

_VERSION_RE = re.compile(
    r"""^v?
    (?P<major>0|[1-9]\d*)
    (?:\.(?P<minor>0|[1-9]\d*))?
    (?:\.(?P<patch>0|[1-9]\d*))?
    (?:-(?P<pre>[0-9A-Za-z.-]+))?
    (?:\+(?P<build>[0-9A-Za-z.-]+))?
    $""",
    re.VERBOSE,
)


@total_ordering
@dataclass(frozen=True)
class Version:
    """A semantic version. Build metadata is kept but ignored in comparisons."""

    major: int
    minor: int = 0
    patch: int = 0
    pre: tuple[int | str, ...] = ()
    build: str = ""

    @classmethod
    def parse(cls, value: str | Version) -> Version:
        """Parse ``1.2.3``, ``v1.2`` or ``1.0.0-rc.1+build`` (raises `VersionError`)."""
        if isinstance(value, Version):
            return value
        m = _VERSION_RE.match(value.strip())
        if not m:
            raise VersionError(f"invalid version: {value!r}")
        pre: tuple[int | str, ...] = ()
        if m["pre"]:
            pre = tuple(int(p) if p.isdigit() else p for p in m["pre"].split("."))
        return cls(
            int(m["major"]),
            int(m["minor"] or 0),
            int(m["patch"] or 0),
            pre,
            m["build"] or "",
        )

    def _key(self) -> tuple[object, ...]:
        # A release sorts after all of its pre-releases.  Numeric identifiers
        # sort before alphanumeric ones (SemVer 2.0 section 11).
        pre_key: tuple[object, ...]
        if not self.pre:
            pre_key = (1,)
        else:
            pre_key = (0, *((0, p, "") if isinstance(p, int) else (1, 0, p) for p in self.pre))
        return (self.major, self.minor, self.patch, pre_key)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() < other._key()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __str__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.pre:
            s += "-" + ".".join(str(p) for p in self.pre)
        if self.build:
            s += "+" + self.build
        return s

    def __repr__(self) -> str:
        return f"Version('{self}')"


_CLAUSE_RE = re.compile(r"^(?P<op>\^|~|==|!=|>=|<=|>|<|=)?\s*(?P<ver>.+)$")


@dataclass(frozen=True)
class VersionConstraint:
    """A conjunction of ``(operator, version)`` clauses."""

    clauses: tuple[tuple[str, Version], ...] = ()
    text: str = "*"

    ANY: ClassVar[VersionConstraint]  # assigned below

    @classmethod
    def parse(cls, text: str | VersionConstraint | None) -> VersionConstraint:
        """Parse a constraint string (see the module documentation)."""
        if isinstance(text, VersionConstraint):
            return text
        if text is None or not text.strip() or text.strip() == "*":
            return cls.ANY
        clauses: list[tuple[str, Version]] = []
        for raw in text.split(","):
            raw = raw.strip()
            if not raw or raw == "*":
                continue
            m = _CLAUSE_RE.match(raw)
            if not m:
                raise VersionError(f"invalid version constraint: {text!r}")
            op = m["op"] or "=="
            ver = Version.parse(m["ver"])
            if op == "=":
                op = "=="
            if op == "^":
                clauses += [(">=", ver), ("<", _caret_upper(ver))]
            elif op == "~":
                clauses += [(">=", ver), ("<", Version(ver.major, ver.minor + 1, 0))]
            else:
                clauses.append((op, ver))
        return cls(tuple(clauses), text.strip())

    def allows(self, version: str | Version) -> bool:
        """Whether ``version`` satisfies every clause."""
        v = Version.parse(version)
        for op, bound in self.clauses:
            ok = {
                "==": v == bound,
                "!=": v != bound,
                ">=": v >= bound,
                "<=": v <= bound,
                ">": v > bound,
                "<": v < bound,
            }[op]
            if not ok:
                return False
        return True

    def __contains__(self, version: str | Version) -> bool:
        return self.allows(version)

    def __str__(self) -> str:
        return self.text


VersionConstraint.ANY = VersionConstraint((), "*")


def _caret_upper(v: Version) -> Version:
    if v.major > 0:
        return Version(v.major + 1, 0, 0)
    if v.minor > 0:
        return Version(0, v.minor + 1, 0)
    return Version(0, 0, v.patch + 1)
