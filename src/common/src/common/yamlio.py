"""YAML loading with duplicate key detection and atomic writes."""

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


class DuplicateKeyError(yaml.YAMLError):
    pass


class _StrictLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise DuplicateKeyError(f"duplicate key at line {key_node.start_mark.line + 1}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def loads_yaml(text: str) -> Any:
    return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass


def load_yaml(path: str | os.PathLike) -> Any:
    with open(path, encoding="utf-8") as fh:
        return loads_yaml(fh.read())


def dumps_yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def atomic_write_text(path: str | os.PathLike, text: str, mode: int = 0o600) -> None:
    """Write a file atomically: temp file in the same directory, fsync, rename."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    try:
        dir_fd = os.open(target.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def atomic_write_yaml(path: str | os.PathLike, data: Any, mode: int = 0o600) -> None:
    atomic_write_text(path, dumps_yaml(data), mode=mode)
