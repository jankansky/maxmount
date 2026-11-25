# SPDX-License-Identifier: GPL-3.0-or-later
"""maxmount.v1 Protobuf messages.

The Python code is generated from schemas/maxmount/v1/mount.proto on first import with the
protoc bundled in grpcio-tools, so generated code always matches the installed protobuf
runtime. It is written to maxmount_svc/_gen (not committed).
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parent
_GEN = _PKG / "_gen"
_SCHEMAS = _PKG.parents[2] / "schemas"  # svc/src/maxmount_svc -> repo/schemas
_PROTO = _SCHEMAS / "maxmount" / "v1" / "mount.proto"


def _generate() -> None:
    from grpc_tools import protoc  # type: ignore[import-untyped]

    if not _PROTO.exists():
        raise RuntimeError(f"cannot find {_PROTO}; run from a maxmount checkout")
    _GEN.mkdir(exist_ok=True)
    rc = protoc.main(["protoc", f"-I{_SCHEMAS}", f"--python_out={_GEN}", str(_PROTO)])
    if rc != 0:
        raise RuntimeError(f"protoc failed ({rc}) for {_PROTO}")


def _load():
    target = _GEN / "maxmount" / "v1" / "mount_pb2.py"
    if not target.exists() or (_PROTO.exists() and _PROTO.stat().st_mtime > target.stat().st_mtime):
        _generate()
    if str(_GEN) not in sys.path:
        sys.path.insert(0, str(_GEN))
    return importlib.import_module("maxmount.v1.mount_pb2")


mpb = _load()

__all__ = ["mpb"]
