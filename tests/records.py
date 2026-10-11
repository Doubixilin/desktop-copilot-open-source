"""测试共用的桌面项记录类型，避免各测试文件各自克隆。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Rec:
    name: str
    kind: str
    index: int
    mtime: float | None = 0.0
    x: int = 0
    y: int = 0
    target: str | None = None


def shortcut(name, index=0, target=None):
    return Rec(name=name, kind="shortcut", index=index, target=target)


def doc(name, index=0, mtime=0.0, x=0, y=0):
    return Rec(name=name, kind="file", index=index, mtime=mtime, x=x, y=y)
