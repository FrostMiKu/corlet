import pytest

from corlet import Version, VersionConstraint, VersionError


@pytest.mark.parametrize(
    "a,b",
    [
        ("1.0.0", "2.0.0"),
        ("1.2.3", "1.2.10"),
        ("1.0.0-alpha", "1.0.0"),
        ("1.0.0-alpha", "1.0.0-alpha.1"),
        ("1.0.0-alpha.1", "1.0.0-beta"),
        ("1.0.0-2", "1.0.0-10"),
        ("1.0.0-9", "1.0.0-a"),
    ],
)
def test_ordering(a: str, b: str) -> None:
    assert Version.parse(a) < Version.parse(b)


def test_parse_partial_and_build() -> None:
    v = Version.parse("v2.1+build.5")
    assert (v.major, v.minor, v.patch) == (2, 1, 0)
    assert v == Version.parse("2.1.0+other")
    assert str(v) == "2.1.0+build.5"


def test_invalid() -> None:
    with pytest.raises(VersionError):
        Version.parse("one.two")
    with pytest.raises(VersionError):
        VersionConstraint.parse(">=")


@pytest.mark.parametrize(
    "constraint,yes,no",
    [
        ("*", ["0.0.1", "99.0.0"], []),
        (">=1.2,<2", ["1.2.0", "1.9.9"], ["1.1.9", "2.0.0"]),
        ("^1.2.3", ["1.2.3", "1.9.0"], ["2.0.0", "1.2.2"]),
        ("^0.2.3", ["0.2.3", "0.2.9"], ["0.3.0"]),
        ("^0.0.3", ["0.0.3"], ["0.0.4"]),
        ("~1.2.3", ["1.2.3", "1.2.9"], ["1.3.0"]),
        ("1.2.3", ["1.2.3"], ["1.2.4"]),
        ("!=1.5.0", ["1.4.0"], ["1.5.0"]),
    ],
)
def test_constraints(constraint: str, yes: list[str], no: list[str]) -> None:
    c = VersionConstraint.parse(constraint)
    for v in yes:
        assert v in c
    for v in no:
        assert v not in c
