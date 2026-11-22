from corlet import Manifest, Requirement, Resolver, ServiceRequirement, Version
from corlet.core.manifest import Activation


class DB: ...


class Cache: ...


def m(cid: str, version: str = "1.0.0", **kw: object) -> Manifest:
    if "requires" in kw:
        kw["requires"] = tuple(Requirement.parse(r) for r in kw["requires"])  # type: ignore[attr-defined]
    if "requires_services" in kw:
        kw["requires_services"] = tuple(
            r if isinstance(r, ServiceRequirement) else ServiceRequirement(r)
            for r in kw["requires_services"]  # type: ignore[attr-defined]
        )
    return Manifest(cid, Version.parse(version), **kw)  # type: ignore[arg-type]


def resolve(*manifests: Manifest, available: set[str] | None = None, **kw: object):  # type: ignore[no-untyped-def]
    return Resolver().resolve({x.id: x for x in manifests}, available, **kw)  # type: ignore[arg-type]


def test_layers_and_order() -> None:
    res = resolve(
        m("app", requires=["svc", "util"]),
        m("svc", requires=["db"]),
        m("db"),
        m("util"),
    )
    assert res.ok
    assert res.layers == [["db", "util"], ["svc"], ["app"]]
    assert res.dependencies("app") == ["svc", "util"]
    assert res.dependents("db") == ["svc"]
    assert res.closure(["svc"]) == {"svc", "db"}


def test_service_requirements_map_to_all_providers() -> None:
    res = resolve(
        m("app", requires_services=[DB]),
        m("pg", provides_services=(DB,)),
        m("sqlite", provides_services=(DB,)),
    )
    assert res.ok
    assert res.dependencies("app") == ["pg", "sqlite"]
    # one provider unavailable: still satisfied
    res = resolve(
        m("app", requires_services=[DB]),
        m("pg", provides_services=(DB,)),
        m("sqlite", provides_services=(DB,)),
        available={"app", "sqlite"},
    )
    assert "app" in res.wanted and res.dependencies("app") == ["sqlite"]


def test_missing_and_version_errors() -> None:
    res = resolve(m("a", requires=["b>=2"]), m("b", "1.5.0"), m("c", requires=["nope"]))
    codes = sorted(d.code for d in res.errors)
    assert codes == ["missing-component", "version-mismatch"]
    assert res.wanted == {"b"}
    assert "has version 1.5.0" in res.unsatisfied["a"][0]


def test_optional_dependencies() -> None:
    res = resolve(m("a", requires=["?b", "?missing"]), m("b"))
    assert res.ok  # missing optional -> warning only
    assert [d.code for d in res.diagnostics] == ["missing-component"]
    assert res.layers == [["b"], ["a"]]
    res = resolve(m("a", requires=["?b"]), m("b"), available={"a"})
    assert res.wanted == {"a"}


def test_transitive_unavailability_with_reason() -> None:
    res = resolve(
        m("a", requires=["b"]),
        m("b", requires=["c"]),
        m("c"),
        available={"a", "b"},
        unavailable_reasons={"c": "disabled"},
    )
    assert res.wanted == set()
    assert res.unsatisfied["b"] == ["required component 'c' is disabled"]
    assert res.unsatisfied["a"] == ["required component 'b' is unsatisfied"]


def test_hard_cycle_is_error_with_path() -> None:
    res = resolve(m("a", requires=["b"]), m("b", requires=["c"]), m("c", requires=["a"]), m("d"))
    (err,) = res.errors
    assert err.code == "cycle"
    assert err.path[0] == err.path[-1] and len(err.path) == 4
    assert res.wanted == {"d"}


def test_optional_edge_breaks_cycle() -> None:
    res = resolve(m("a", requires=["b"]), m("b", requires=["?a"]))
    assert res.ok
    assert res.wanted == {"a", "b"}
    assert [d.code for d in res.diagnostics] == ["optional-cycle"]
    assert res.layers == [["b"], ["a"]]


def test_external_services_satisfy() -> None:
    res = resolve(m("a", requires_services=[Cache]), external_services={"test_resolver.Cache"})
    assert res.ok and res.wanted == {"a"}


def test_lazy_manifest_does_not_change_resolution() -> None:
    res = resolve(m("a", requires=["b"]), m("b", activation=Activation.LAZY))
    assert res.wanted == {"a", "b"}
