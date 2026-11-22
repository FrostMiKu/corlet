"""Configuration abstraction used by the container.

The core only knows two things:

* a `ConfigSource` produces a nested mapping; the container reads the
  section ``components.<component-id>`` for each component;
* a component's ``config_schema`` turns that mapping into a typed object.

Supported schemas (detected structurally, so Pydantic stays optional):

* Pydantic v2 models (``model_validate``) -- recommended
* dataclasses (nested dataclasses, ``list[...]``, ``Optional`` and basic
  scalar coercion are supported)
* ``dict`` / ``None`` -- the raw mapping is passed through

Richer, layered sources (files, environment, CLI overrides, reloading) live in
`corlet.std.config`.
"""

from __future__ import annotations

import dataclasses
import enum
import types
import typing
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol, runtime_checkable

from .errors import ConfigError

__all__ = [
    "ConfigSource",
    "DictConfigSource",
    "config_section",
    "deep_merge",
    "validate_config",
]

COMPONENTS_KEY = "components"


@runtime_checkable
class ConfigSource(Protocol):
    """Anything that can produce the full configuration tree."""

    def load(self) -> Mapping[str, Any]:
        """Return the complete configuration tree."""
        ...


class DictConfigSource:
    """Configuration from an in-memory mapping.

    The mapping is referenced, not copied: mutate it (or assign ``.data``) and
    call `Container.reload_config()` to apply changes.
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = data if data is not None else {}

    def load(self) -> Mapping[str, Any]:
        """Return the referenced mapping."""
        return self.data


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def config_section(
    tree: Mapping[str, Any], component_id: str, known_ids: Iterable[str] = ()
) -> dict[str, Any]:
    """Return the configuration section of ``component_id`` (empty dict if absent).

    Both the flat form ``{"components": {"acme.db": {...}}}`` and the nested
    form ``{"components": {"acme": {"db": {...}}}}`` are accepted (flat keys
    win on conflict within one mapping; `LayeredConfig`
    canonicalizes layers so precedence between layers is preserved).

    In the nested form a parent section contains its children's sections;
    keys naming another component in ``known_ids`` (e.g. ``db`` inside
    ``acme`` when ``acme.db`` exists) are therefore removed.
    """
    comps = tree.get(COMPONENTS_KEY) or {}
    if not isinstance(comps, Mapping):
        raise ConfigError(None, f"'{COMPONENTS_KEY}' must be a mapping")
    nested: Any = comps
    for part in component_id.split("."):
        nested = nested.get(part) if isinstance(nested, Mapping) else None
        if nested is None:
            break
    flat = comps.get(component_id) if "." in component_id else None
    if flat is not None and not isinstance(flat, Mapping):
        raise ConfigError(component_id, "configuration section must be a mapping")
    if nested is not None and not isinstance(nested, Mapping):
        raise ConfigError(component_id, "configuration section must be a mapping")
    result: dict[str, Any] = dict(nested or {})
    if flat:
        result = deep_merge(result, flat)
    prefix = component_id + "."
    children = {k[len(prefix) :].split(".", 1)[0] for k in known_ids if k.startswith(prefix)}
    return {k: v for k, v in result.items() if k not in children}


def canonicalize(tree: Mapping[str, Any]) -> dict[str, Any]:
    """Expand dotted keys of the ``components`` section into the nested form.

    ``{"components": {"acme.db": {"port": 1}}}`` becomes
    ``{"components": {"acme": {"db": {"port": 1}}}}``.  Merging canonical
    layers keeps the precedence of later layers regardless of which form each
    layer used.
    """
    out = dict(tree)
    comps = tree.get(COMPONENTS_KEY)
    if not isinstance(comps, Mapping):
        return out
    nested: dict[str, Any] = {}
    for key, value in comps.items():
        node: Any = value
        for part in reversed(str(key).split(".")):
            node = {part: node}
        nested = deep_merge(nested, node)
    out[COMPONENTS_KEY] = nested
    return out


def validate_config(schema: Any, data: Mapping[str, Any], component_id: str) -> Any:
    """Validate ``data`` against ``schema`` and return the typed config object."""
    if schema is None or schema is dict:
        return dict(data)
    validate = getattr(schema, "model_validate", None)
    if callable(validate):  # Pydantic v2
        try:
            return validate(dict(data))
        except Exception as exc:
            raise ConfigError(component_id, _pydantic_message(exc)) from exc
    if dataclasses.is_dataclass(schema) and isinstance(schema, type):
        try:
            return _build_dataclass(schema, data, path="")
        except ConfigError:
            raise
        except (TypeError, ValueError) as exc:
            raise ConfigError(component_id, str(exc)) from exc
    if callable(schema):
        try:
            return schema(dict(data))
        except Exception as exc:
            raise ConfigError(component_id, str(exc)) from exc
    raise ConfigError(component_id, f"unsupported config schema {schema!r}")


def default_config(schema: Any, component_id: str) -> Any:
    return validate_config(schema, {}, component_id)


def _pydantic_message(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    parts = []
    for err in errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        parts.append(f"{loc}: {err.get('msg')}")
    return "; ".join(parts)


# -- minimal dataclass coercion ---------------------------------------------

_SCALARS: dict[type, Callable[[Any], Any]] = {
    int: int,
    float: float,
    str: str,
}


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return bool(v)
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off", ""):
            return False
    raise ValueError(f"cannot interpret {v!r} as bool")


def _coerce(tp: Any, value: Any, path: str) -> Any:
    if tp is Any or tp is object:
        return value
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is typing.Union or origin is types.UnionType:
        if value is None and type(None) in args:
            return None
        errors = []
        for arg in args:
            if arg is type(None):
                continue
            try:
                return _coerce(arg, value, path)
            except (TypeError, ValueError) as exc:
                errors.append(str(exc))
        raise ValueError(f"{path}: {value!r} does not match {tp} ({'; '.join(errors)})")
    if origin is typing.Literal:
        if value in args:
            return value
        raise ValueError(f"{path}: {value!r} is not one of {list(args)}")
    if origin in (list, tuple, set, frozenset):
        if isinstance(value, str | bytes) or not hasattr(value, "__iter__"):
            raise ValueError(f"{path}: expected a sequence, got {value!r}")
        items = list(value)
        if origin is tuple and args and args[-1] is not Ellipsis:
            if len(args) != len(items):
                raise ValueError(f"{path}: expected {len(args)} items")
            return tuple(
                _coerce(a, v, f"{path}[{i}]")
                for i, (a, v) in enumerate(zip(args, items, strict=True))
            )
        inner = args[0] if args else Any
        conv = [_coerce(inner, v, f"{path}[{i}]") for i, v in enumerate(items)]
        return origin(conv)
    if origin in (dict, Mapping) or tp in (dict, Mapping):
        if not isinstance(value, Mapping):
            raise ValueError(f"{path}: expected a mapping, got {value!r}")
        if not args:
            return dict(value)
        kt, vt = args
        return {_coerce(kt, k, path): _coerce(vt, v, f"{path}.{k}") for k, v in value.items()}
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        if isinstance(value, tp):
            return value
        if not isinstance(value, Mapping):
            raise ValueError(f"{path}: expected a mapping for {tp.__name__}, got {value!r}")
        return _build_dataclass(tp, value, path)
    if tp is bool:
        try:
            return _to_bool(value)
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from None
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        try:
            return tp(value)
        except ValueError:
            raise ValueError(f"{path}: {value!r} is not a valid {tp.__name__}") from None
    scalar = _SCALARS.get(tp) if isinstance(tp, type) else None
    if scalar is not None:
        if isinstance(value, tp) and not (tp is int and isinstance(value, bool)):
            return value
        if tp in (int, float) and isinstance(value, bool):
            raise ValueError(f"{path}: expected {tp.__name__}, got bool")
        if tp is str and not isinstance(value, str | int | float):
            raise ValueError(f"{path}: expected str, got {type(value).__name__}")
        try:
            return scalar(value)
        except (TypeError, ValueError):
            raise ValueError(f"{path}: expected {tp.__name__}, got {value!r}") from None
    if isinstance(tp, type):
        if isinstance(value, tp):
            return value
        try:
            return tp(value)
        except Exception:
            raise ValueError(f"{path}: expected {tp.__name__}, got {value!r}") from None
    return value


def _build_dataclass(cls: type, data: Mapping[str, Any], path: str) -> Any:
    if not isinstance(data, Mapping):
        raise ValueError(f"{path or '<root>'}: expected a mapping")
    hints = typing.get_type_hints(cls)
    fields = {f.name: f for f in dataclasses.fields(cls) if f.init}
    unknown = set(data) - set(fields)
    if unknown:
        raise ValueError(
            f"{path or '<root>'}: unknown field(s) {sorted(unknown)}; expected {sorted(fields)}"
        )
    kwargs: dict[str, Any] = {}
    for name, f in fields.items():
        sub = f"{path}.{name}" if path else name
        if name in data:
            kwargs[name] = _coerce(hints.get(name, Any), data[name], sub)
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise ValueError(f"{sub}: field required")
    return cls(**kwargs)
