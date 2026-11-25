# SPDX-License-Identifier: GPL-3.0-or-later
"""maxmount.v1 Protobuf messages.

The Python code is generated from schemas/maxmount/v1/*.proto on first import with the
protoc bundled in grpcio-tools, so generated code always matches the installed protobuf
runtime. It is written to maxmount_svc/_gen (not committed).
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

_PKG = Path(__file__).resolve().parent
_GEN = _PKG / "_gen"
_SCHEMAS = _PKG.parents[2] / "schemas"  # svc/src/maxmount_svc -> repo/schemas
_PROTO_DIR = _SCHEMAS / "maxmount" / "v1"


def _protos() -> list[Path]:
    files = sorted(_PROTO_DIR.glob("*.proto"))
    if not files:
        raise RuntimeError(f"no .proto files in {_PROTO_DIR}; run from a maxmount checkout")
    return files


def _generate() -> None:
    from grpc_tools import protoc  # type: ignore[import-untyped]

    _GEN.mkdir(exist_ok=True)
    rc = protoc.main(["protoc", f"-I{_SCHEMAS}", f"--python_out={_GEN}", *map(str, _protos())])
    if rc != 0:
        raise RuntimeError(f"protoc failed ({rc}) for {_PROTO_DIR}")


def _load():
    out = _GEN / "maxmount" / "v1"
    stale = any(
        not (out / f"{p.stem}_pb2.py").exists() or p.stat().st_mtime > (out / f"{p.stem}_pb2.py").stat().st_mtime
        for p in _protos()
    )
    if stale:
        _generate()
    if str(_GEN) not in sys.path:
        sys.path.insert(0, str(_GEN))
    ns = types.SimpleNamespace()
    for p in _protos():
        mod = importlib.import_module(f"maxmount.v1.{p.stem}_pb2")
        for name in dir(mod):
            if not name.startswith("_"):
                setattr(ns, name, getattr(mod, name))
    return ns


# All maxmount.v1 messages and enums (mount.proto, target.proto, ...) in one namespace.
mpb = _load()

__all__ = ["mpb"]
