"""Dependency resolution: from manifests to an ordered, layered DAG.

The resolver is a pure function of

* every installed manifest,
* the set of component ids that are *available* (enabled and not failed),
* the contracts registered externally (by the application, not a component).

It is re-run whenever intent changes (enable/disable/failure), which keeps the
container a simple *reconciler*: compute the wanted set, then converge.

A component is **wanted** when it is available and all of its *required*
dependencies are wanted, transitively:

* a component requirement (``requires=["acme.db>=1.2"]``) needs that component
  to be wanted and to satisfy the version constraint;
* a service requirement (``requires_services=[Database]``) needs at least one
  wanted provider (a component declaring ``provides=[Database]``) or an
  external registration.  The consumer is ordered after *every* wanted
  provider, and is restarted whenever one of them is.

Optional requirements never make a component unwanted; they only add ordering
edges.  Optional edges that would close a cycle are dropped with a warning;
cycles made of required edges are errors.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from ._util import contract_name
from .errors import Diagnostic
from .manifest import Manifest

__all__ = ["Edge", "Resolution", "Resolver"]


@dataclass(frozen=True)
class Edge:
    """``source`` depends on ``target``."""

    source: str
    target: str
    optional: bool
    via: str  # "component" or the service contract key

    @property
    def kind(self) -> str:
        return "component" if self.via == "component" else "service"


@dataclass
class Resolution:
    """Result of resolving the component graph.

    Attributes:
        wanted: Components that can run (enabled, dependencies satisfiable).
        unsatisfied: Available components that cannot run, with reasons.
        edges: Dependency edges among wanted components, by source.
        layers: Wanted components grouped so that every dependency is in
            an earlier layer; components of one layer start concurrently.
        diagnostics: Errors and warnings found while resolving.
    """

    wanted: set[str] = field(default_factory=set)
    unsatisfied: dict[str, list[str]] = field(default_factory=dict)
    edges: dict[str, list[Edge]] = field(default_factory=dict)
    layers: list[list[str]] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        """Error diagnostics only."""
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def ok(self) -> bool:
        """``True`` if there are no error diagnostics."""
        return not self.errors

    @property
    def order(self) -> list[str]:
        """Wanted components in start order (layers flattened)."""
        return [cid for layer in self.layers for cid in layer]

    def layer_of(self, cid: str) -> int | None:
        """Index of the layer containing ``cid``, or ``None``."""
        for i, layer in enumerate(self.layers):
            if cid in layer:
                return i
        return None

    def dependencies(self, cid: str, *, required_only: bool = False) -> list[str]:
        """Wanted components ``cid`` depends on."""
        return sorted(
            {e.target for e in self.edges.get(cid, ()) if not (required_only and e.optional)}
        )

    def dependents(self, cid: str, *, required_only: bool = False) -> list[str]:
        """Wanted components that depend on ``cid``."""
        return sorted(
            src
            for src, es in self.edges.items()
            if any(e.target == cid and not (required_only and e.optional) for e in es)
        )

    def closure(self, roots: Iterable[str], *, required_only: bool = True) -> set[str]:
        """``roots`` plus everything they (transitively) depend on."""
        seen: set[str] = set()
        stack = [r for r in roots if r in self.wanted]
        while stack:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            stack.extend(self.dependencies(cid, required_only=required_only))
        return seen


class Resolver:
    """Stateless dependency resolver; see the module documentation."""

    def resolve(
        self,
        manifests: Mapping[str, Manifest],
        available: Collection[str] | None = None,
        external_services: Collection[str] = (),
        *,
        unavailable_reasons: Mapping[str, str] | None = None,
    ) -> Resolution:
        """Resolve the component graph.

        Args:
            manifests: Every installed manifest, by id.
            available: Ids that may run (enabled and not failed); default all.
            external_services: Contract keys registered by the application.
            unavailable_reasons: ``id -> reason`` used in messages about
                unavailable dependencies (e.g. ``"disabled"``).
        """
        avail = set(manifests) if available is None else set(available) & set(manifests)
        reasons = dict(unavailable_reasons or {})
        external = set(external_services)
        res = Resolution()
        diags = res.diagnostics

        providers: dict[str, list[str]] = {}
        for cid in sorted(manifests):
            for key in manifests[cid].provides_keys:
                providers.setdefault(key, []).append(cid)

        # 1. static problems (independent of availability) --------------------
        for cid in sorted(avail):
            m = manifests[cid]
            for req in m.requires:
                if req.id == cid:
                    diags.append(Diagnostic("error", "self-dependency", "requires itself", cid))
                    continue
                target = manifests.get(req.id)
                if target is None:
                    diags.append(
                        Diagnostic(
                            "warning" if req.optional else "error",
                            "missing-component",
                            f"requires {req.id!r} which is not installed",
                            cid,
                        )
                    )
                elif not req.constraint.allows(target.version):
                    diags.append(
                        Diagnostic(
                            "warning" if req.optional else "error",
                            "version-mismatch",
                            f"requires {req.id}{req.constraint} but {target.version} is installed",
                            cid,
                        )
                    )
            for sreq in m.requires_services:
                if not providers.get(sreq.key) and sreq.key not in external:
                    diags.append(
                        Diagnostic(
                            "warning" if sreq.optional else "error",
                            "missing-service",
                            f"requires service {contract_name(sreq.contract)} but no installed "
                            "component provides it",
                            cid,
                        )
                    )

        # 2. fix-point: drop available components whose required deps fail -----
        wanted = set(avail)
        why: dict[str, list[str]] = {}
        changed = True
        while changed:
            changed = False
            for cid in sorted(wanted):
                problems = self._unmet(cid, manifests, wanted, providers, external, avail, reasons)
                if problems:
                    wanted.discard(cid)
                    why[cid] = problems
                    changed = True

        # 3. edges among wanted components ---------------------------------
        edges: dict[str, list[Edge]] = {cid: [] for cid in wanted}
        for cid in sorted(wanted):
            m = manifests[cid]
            for req in m.requires:
                if (
                    req.id in wanted
                    and req.id != cid
                    and req.constraint.allows(manifests[req.id].version)
                ):
                    edges[cid].append(Edge(cid, req.id, req.optional, "component"))
            for sreq in m.requires_services:
                for p in providers.get(sreq.key, ()):
                    if p in wanted and p != cid:
                        edges[cid].append(Edge(cid, p, sreq.optional, sreq.key))

        # 4. cycles ----------------------------------------------------------
        cyclic: set[str] = set()
        for scc in _sccs(wanted, edges):
            if len(scc) == 1:
                continue
            inner = set(scc)
            req_only = {
                c: [e for e in edges[c] if e.target in inner and not e.optional] for c in scc
            }
            hard = [s for s in _sccs(inner, req_only) if len(s) > 1]
            if hard:
                for comp in hard:
                    path = _cycle_path(comp, req_only)
                    diags.append(
                        Diagnostic(
                            "error",
                            "cycle",
                            "circular dependency between components",
                            path[0],
                            tuple(path),
                        )
                    )
                    cyclic.update(comp)
            # break the remaining (optional) cycles
            for c in scc:
                dropped = [e for e in edges[c] if e.target in inner and e.optional]
                if dropped:
                    edges[c] = [e for e in edges[c] if e not in dropped]
                    for e in dropped:
                        diags.append(
                            Diagnostic(
                                "warning",
                                "optional-cycle",
                                f"ignoring optional dependency on {e.target!r} (it would "
                                "create a cycle); start order is not guaranteed",
                                c,
                            )
                        )
        if cyclic:
            # cyclic components and whatever requires them are unwanted
            for cid in cyclic:
                wanted.discard(cid)
                why.setdefault(cid, []).append("part of a dependency cycle")
            changed = True
            while changed:
                changed = False
                for cid in sorted(wanted):
                    if any(not e.optional and e.target not in wanted for e in edges[cid]):
                        wanted.discard(cid)
                        bad = [e.target for e in edges[cid] if e.target not in wanted]
                        why.setdefault(cid, []).append(
                            f"depends on unavailable component(s) {', '.join(sorted(set(bad)))}"
                        )
                        changed = True
            edges = {
                c: [e for e in es if e.target in wanted] for c, es in edges.items() if c in wanted
            }

        # 5. extension sanity --------------------------------------------------
        defined = {p.id for m in manifests.values() for p in m.defines}
        for cid in sorted(avail):
            for spec in manifests[cid].extensions:
                if spec.point_id not in defined and isinstance(spec.point, str):
                    diags.append(
                        Diagnostic(
                            "warning",
                            "unknown-extension-point",
                            f"contributes to {spec.point_id!r} which no installed component "
                            "defines",
                            cid,
                        )
                    )

        res.wanted = wanted
        res.unsatisfied = why
        res.edges = edges
        res.layers = _layers(wanted, edges)
        return res

    @staticmethod
    def _unmet(
        cid: str,
        manifests: Mapping[str, Manifest],
        wanted: set[str],
        providers: Mapping[str, list[str]],
        external: set[str],
        avail: set[str],
        reasons: Mapping[str, str],
    ) -> list[str]:
        m = manifests[cid]
        problems: list[str] = []

        def state_of(dep: str) -> str:
            if dep not in manifests:
                return "not installed"
            if dep not in avail:
                return reasons.get(dep, "disabled")
            return "unsatisfied"

        for req in m.requires:
            if req.optional:
                continue
            if req.id == cid:
                problems.append("requires itself")
            elif req.id not in manifests:
                problems.append(f"required component {req.id!r} is not installed")
            elif not req.constraint.allows(manifests[req.id].version):
                problems.append(
                    f"required component {req.id!r} has version {manifests[req.id].version}, "
                    f"needs {req.constraint}"
                )
            elif req.id not in wanted:
                problems.append(f"required component {req.id!r} is {state_of(req.id)}")
        for sreq in m.requires_services:
            if sreq.optional or sreq.key in external:
                continue
            ps = [p for p in providers.get(sreq.key, ()) if p != cid]
            if not any(p in wanted for p in ps):
                name = contract_name(sreq.contract)
                if not ps:
                    problems.append(f"no component provides required service {name}")
                else:
                    detail = ", ".join(f"{p} ({state_of(p)})" for p in ps)
                    problems.append(f"no available provider of {name}: {detail}")
        return problems


def _sccs(nodes: Iterable[str], edges: Mapping[str, list[Edge]]) -> list[list[str]]:
    """Tarjan's strongly connected components (iterative, deterministic)."""
    node_set = set(nodes)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in sorted(node_set):
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on_stack.add(v)
            succ = sorted({e.target for e in edges.get(v, ()) if e.target in node_set})
            recurse = False
            for j in range(i, len(succ)):
                w = succ[j]
                if w not in index:
                    work.append((v, j + 1))
                    work.append((w, 0))
                    recurse = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if recurse:
                continue
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(sorted(comp))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return out


def _cycle_path(scc: list[str], edges: Mapping[str, list[Edge]]) -> list[str]:
    """A concrete cycle inside ``scc`` for error messages: a -> b -> a."""
    inner = set(scc)
    start = scc[0]
    path = [start]
    seen = {start}
    cur = start
    while True:
        nxt = sorted(e.target for e in edges.get(cur, ()) if e.target in inner)
        if not nxt:
            return path
        if start in nxt and len(path) > 1:
            return [*path, start]
        candidates = [n for n in nxt if n not in seen]
        if not candidates:
            target = nxt[0]
            return [*path[path.index(target) :], target] if target in path else [*path, target]
        cur = candidates[0]
        path.append(cur)
        seen.add(cur)


def _layers(nodes: set[str], edges: Mapping[str, list[Edge]]) -> list[list[str]]:
    """Group nodes so that every dependency is in an earlier layer."""
    depth: dict[str, int] = {}

    def visit(n: str, trail: frozenset[str]) -> int:
        if n in depth:
            return depth[n]
        deps = [e.target for e in edges.get(n, ()) if e.target in nodes and e.target not in trail]
        d = 0 if not deps else 1 + max(visit(t, trail | {n}) for t in deps)
        depth[n] = d
        return d

    for n in sorted(nodes):
        visit(n, frozenset())
    layers: list[list[str]] = [[] for _ in range(max(depth.values(), default=-1) + 1)]
    for n in sorted(nodes):
        layers[depth[n]].append(n)
    return layers
