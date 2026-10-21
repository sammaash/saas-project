"""Patching a stored JSON configuration document without losing what it already holds.

``tenant_settings`` stores the bot configuration as four jsonb documents. An administrator
editing one setting must not lose any other setting, so every write in this area is a
*merge* of a validated patch over the stored document rather than a replacement.

The patch vocabulary is deliberately small and explicit:

* an ordinary value (including ``None`` and ``""``) replaces the stored value,
* an object is merged key by key, so sibling keys the patch does not mention survive,
* :data:`UNSET` removes the key, which is how "clear this setting" is expressed,
* :class:`ItemUpdate` and :class:`ItemRemoval` perform single-item list surgery, which a
  recursive merge cannot express (editing or removing one FAQ, one document).

Nothing here knows what the fields mean: validation happens in the section builders, so
this module stays a pure, easily testable function library.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from . import validation as v


class _Unset:
    """Marker meaning "remove this key": the setting goes back to being unset."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"


UNSET = _Unset()


@dataclass(frozen=True)
class ItemUpdate:
    """Replace one item of a stored list (``item=None`` removes the item at ``index``)."""

    index: int
    item: Any | None = None


@dataclass(frozen=True)
class ItemRemoval:
    """Remove every item of a stored list equal to ``value`` (explicit removal only)."""

    value: Any


def deep_merge(stored: Any, patch: Any) -> Any:
    """Merge ``patch`` over ``stored``, recursing into objects.

    Every other value replaces the stored one -- including ``None`` and ``""``, which is how
    an explicit clear is expressed. ``UNSET`` removes the key instead, and an empty object is
    a no-op (no section needs to blank an object wholesale).
    """
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    merged: dict[str, Any] = copy.deepcopy(stored) if isinstance(stored, dict) else {}
    for key, value in patch.items():
        if value is UNSET:
            merged.pop(key, None)
        elif isinstance(value, ItemUpdate):
            merged[key] = _apply_item_update(merged.get(key), value, key)
        elif isinstance(value, ItemRemoval):
            merged[key] = _apply_item_removal(merged.get(key), value, key)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _stored_list(stored: Any, key: str) -> list[Any]:
    if stored is None:
        return []
    if not isinstance(stored, list):
        raise v.ServiceError(f"The saved {key} is not a list, so it cannot be edited here.")
    return list(stored)


def _apply_item_update(stored: Any, update: ItemUpdate, key: str) -> list[Any]:
    items = _stored_list(stored, key)
    if not 0 <= update.index < len(items):
        raise v.ServiceError(f"That entry in {key} no longer exists. Reload the page.")
    if update.item is None:
        items.pop(update.index)
    else:
        items[update.index] = copy.deepcopy(update.item)
    return items


def _apply_item_removal(stored: Any, removal: ItemRemoval, key: str) -> list[Any]:
    items = _stored_list(stored, key)
    remaining = [item for item in items if item != removal.value]
    if len(remaining) == len(items):
        raise v.ServiceError(f"That entry in {key} is no longer listed. Reload the page.")
    return remaining


def prune(patch: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    """Keep only the branch of ``patch`` named by ``path`` (field-level saves)."""
    if not path:
        return patch
    head, *rest = path
    if head not in patch:
        return {}
    if not rest:
        return {head: patch[head]}
    branch = patch[head]
    nested = prune(branch, tuple(rest)) if isinstance(branch, dict) else {}
    return {head: nested} if nested else {}


def prune_columns(patch: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    """Apply :func:`prune` inside every column patch (paths never name the column)."""
    pruned: dict[str, Any] = {}
    for column, column_patch in patch.items():
        branch = prune(column_patch, path) if isinstance(column_patch, dict) else {}
        if branch:
            pruned[column] = branch
    return pruned


def field_paths(patch: dict[str, Any], prefix: str = "") -> list[str]:
    """Flatten a patch into ``a.b`` paths, for audit metadata and success messages."""
    paths: list[str] = []
    for key, value in patch.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            paths.extend(field_paths(value, f"{name}."))
        else:
            paths.append(name)
    return paths
