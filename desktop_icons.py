"""桌面图标视图的适配层。

通过 explorer 的桌面图标 ListView 读取图标清单（显示名、坐标），再与
用户桌面 / 公共桌面的文件系统条目按显示名对齐，补出类型、路径、快捷方式
目标与隐藏属性。写通道只有一个原语 IconView.set_position，供
desktop_layout 的还原与落位使用；本模块不移动、重命名或删除任何文件。

跨进程读取需要在 explorer 进程内分配内存缓冲；ctypes 必须为 64 位句柄
显式声明 restype/argtypes，否则访问违例。
"""
import ctypes
import sys
import time
import unicodedata
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

LVM_FIRST = 0x1000
LVM_GETITEMCOUNT = LVM_FIRST + 4
LVM_GETITEMPOSITION = LVM_FIRST + 16
LVM_SETITEMPOSITION = LVM_FIRST + 15
LVM_GETITEMTEXTW = LVM_FIRST + 115

PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_ACCESS = (
    PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_QUERY_INFORMATION
)
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04

TEXT_CHARS = 260

CONVERGE_ROUNDS = 3
CONVERGE_DELAY = 1.5

FOLDERID_DESKTOP = uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")
CSIDL_DESKTOP = 0x0000
CSIDL_COMMON_DESKTOP = 0x0019

ATTR_HIDDEN = 0x2
ATTR_SYSTEM = 0x4

CLSCTX_INPROC_SERVER = 0x1
CLSID_ShellLink = uuid.UUID("00021401-0000-0000-C000-000000000046")
IID_IShellLinkW = uuid.UUID("000214F9-0000-0000-C000-000000000046")
IID_IPersistFile = uuid.UUID("0000010B-0000-0000-C000-000000000046")


class DesktopViewUnavailable(Exception):
    """桌面图标视图不可用（explorer 未就绪、结构变更、权限不足等）。"""


class WriteAborted(Exception):
    """落位中途失败：携带已完成数与失败项显示名，供调用方明确上报而非静默。"""

    def __init__(self, done, name, cause):
        super().__init__(cause)
        self.done = done
        self.name = name


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.ULONG),
        ("Data2", wintypes.USHORT),
        ("Data3", wintypes.USHORT),
        ("Data4", wintypes.BYTE * 8),
    ]


class _LVITEMW(ctypes.Structure):
    _fields_ = [
        ("mask", wintypes.UINT),
        ("iItem", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("state", wintypes.UINT),
        ("stateMask", wintypes.UINT),
        ("pszText", wintypes.LPWSTR),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", wintypes.LPARAM),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _guid(value):
    b = value.bytes
    g = _GUID()
    g.Data1, g.Data2, g.Data3 = int.from_bytes(b[:4], "big"), int.from_bytes(b[4:6], "big"), int.from_bytes(b[6:8], "big")
    g.Data4 = (wintypes.BYTE * 8).from_buffer_copy(b[8:])
    return g


user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.SendMessageW.restype = wintypes.LPARAM
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [_ENUMPROC, wintypes.LPARAM]

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.VirtualAllocEx.restype = ctypes.c_void_p
kernel32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
kernel32.VirtualFreeEx.restype = wintypes.BOOL
kernel32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.WriteProcessMemory.restype = wintypes.BOOL
kernel32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetFileAttributesW.restype = wintypes.DWORD
kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]

shell32 = ctypes.WinDLL("shell32", use_last_error=True)
shell32.SHGetFolderPathW.restype = ctypes.HRESULT
shell32.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR]
ole32 = ctypes.OleDLL("ole32")


@dataclass(frozen=True)
class DesktopItem:
    index: int
    name: str
    x: int
    y: int
    kind: str  # shortcut | url | folder | file | system | special
    path: str | None
    target: str | None
    hidden: bool
    mtime: float | None = None  # 文档区组内排序用；快照路径（bare_items）无此值


def known_folder(csidl):
    buf = ctypes.create_unicode_buffer(TEXT_CHARS)
    hr = shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    if hr < 0 or not buf.value:
        raise DesktopViewUnavailable(f"无法解析桌面目录 CSIDL=0x{csidl:04x}")
    return Path(buf.value)


def desktop_dirs():
    return [known_folder(CSIDL_DESKTOP), known_folder(CSIDL_COMMON_DESKTOP)]


def _find_defview():
    progman = user32.FindWindowW("Progman", None)
    if progman:
        view = user32.FindWindowExW(progman, None, "SHELLDLL_DefView", None)
        if view:
            return view
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum(hwnd, _):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value == "WorkerW":
            view = user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None)
            if view:
                found.append(view)
                return False
        return True

    user32.EnumWindows(enum, 0)
    return found[0] if found else None


class IconView:
    """explorer 桌面图标 ListView 的只读句柄，持有跨进程内存缓冲。"""

    def __init__(self):
        self.defview = _find_defview()
        if not self.defview:
            raise DesktopViewUnavailable("找不到 SHELLDLL_DefView（explorer 桌面图标视图）")
        self.listview = user32.FindWindowExW(self.defview, None, "SysListView32", None)
        if not self.listview:
            raise DesktopViewUnavailable("SHELLDLL_DefView 下找不到 SysListView32")
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(self.listview, ctypes.byref(pid))
        self.proc = kernel32.OpenProcess(PROCESS_ACCESS, False, pid.value)
        if not self.proc:
            raise DesktopViewUnavailable(f"无法打开 explorer 进程 {pid.value}")
        self._lvitem_size = ctypes.sizeof(_LVITEMW)
        self._off_text = self._lvitem_size
        self._off_point = self._off_text + TEXT_CHARS * 2
        size = self._off_point + ctypes.sizeof(_POINT)
        self._buf = kernel32.VirtualAllocEx(self.proc, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not self._buf:
            kernel32.CloseHandle(self.proc)
            raise DesktopViewUnavailable("无法在 explorer 进程内分配内存")

    def close(self):
        if getattr(self, "_buf", None):
            kernel32.VirtualFreeEx(self.proc, self._buf, 0, MEM_RELEASE)
            self._buf = None
        if getattr(self, "proc", None):
            kernel32.CloseHandle(self.proc)
            self.proc = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _write(self, offset, data):
        written = ctypes.c_size_t()
        if not kernel32.WriteProcessMemory(self.proc, self._buf + offset, data, len(data), ctypes.byref(written)):
            raise DesktopViewUnavailable("跨进程写入失败")

    def _read(self, offset, size):
        out = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t()
        if not kernel32.ReadProcessMemory(self.proc, self._buf + offset, out, size, ctypes.byref(read)):
            raise DesktopViewUnavailable("跨进程读取失败")
        return out.raw

    def count(self):
        return user32.SendMessageW(self.listview, LVM_GETITEMCOUNT, 0, 0)

    def bare_items(self):
        """当前图标清单的最小记录（kind 留 unknown），供快照与还原使用。"""
        return [
            DesktopItem(i, self.text(i), *self.position(i), "unknown", None, None, False)
            for i in range(self.count())
        ]

    def position(self, index):
        user32.SendMessageW(self.listview, LVM_GETITEMPOSITION, index, self._buf + self._off_point)
        raw = self._read(self._off_point, ctypes.sizeof(_POINT))
        pt = _POINT.from_buffer_copy(raw)
        return (pt.x, pt.y)

    def set_position(self, index, x, y):
        """落位单个图标。坐标须为非负且小于 65536（LPARAM 打包为两个 16 位字）。"""
        lParam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
        if not user32.SendMessageW(self.listview, LVM_SETITEMPOSITION, index, lParam):
            raise DesktopViewUnavailable(f"LVM_SETITEMPOSITION 失败 index={index}")

    def write_moves(self, moves):
        """落位 [(index, 显示名, x, y)]；任一项失败即抛 WriteAborted，后续不再写。"""
        for done, (index, _name, x, y) in enumerate(moves):
            try:
                self.set_position(index, x, y)
            except DesktopViewUnavailable as exc:
                raise WriteAborted(done, moves[done][1], exc) from exc

    def write_named_moves(self, moves):
        """落位 [(显示名, x, y)]。

        explorer 应用位置是异步且带动画的，批量写入时末几项可能被吞：
        写后读回、把没落上的补写，最多 CONVERGE_ROUNDS 轮收敛。
        每轮都重新解析名字到 index 的映射——视图可能在轮与轮之间刷新重排，
        拿旧映射补写会落到别的图标上。
        """
        pending = list(moves)
        for _ in range(CONVERGE_ROUNDS):
            index = {self.text(i): i for i in range(self.count())}
            resolved = [(index[name], name, x, y) for name, x, y in pending if name in index]
            if not resolved:
                return
            self.write_moves(resolved)
            time.sleep(CONVERGE_DELAY)
            actual = {self.text(i): self.position(i) for i in range(self.count())}
            pending = [(n, x, y) for _i, n, x, y in resolved if actual.get(n) != (x, y)]
            if not pending:
                return

    def text(self, index):
        item = _LVITEMW()
        item.pszText = ctypes.cast(self._buf + self._off_text, wintypes.LPWSTR)
        item.cchTextMax = TEXT_CHARS
        self._write(0, bytes(item))
        user32.SendMessageW(self.listview, LVM_GETITEMTEXTW, index, self._buf)
        raw = self._read(self._off_text, TEXT_CHARS * 2)
        return raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]


def file_attributes(path):
    attrs = kernel32.GetFileAttributesW(str(path))
    return attrs


def scan_files():
    """两个桌面目录下的文件系统条目（含隐藏系统项，它们不出现在图标视图里）。

    键同时收录全名与去扩展名 stem：Explorer 永不显示 .lnk/.url 的扩展名，
    图标视图里的显示名是 stem，而文档类显示名带扩展名。
    """
    entries = {}
    for directory in desktop_dirs():
        for entry in directory.iterdir():
            info = {
                "path": str(entry),
                "is_dir": entry.is_dir(),
                "hidden": False,
                "system": False,
            }
            attrs = file_attributes(entry)
            info["hidden"] = bool(attrs & ATTR_HIDDEN)
            info["system"] = bool(attrs & ATTR_SYSTEM)
            for key in {entry.name, entry.stem}:
                entries.setdefault(key, info)
    return entries


def display_names(names):
    """文件系统名集合 → 图标视图显示名集合（.lnk/.url 显示 stem，其余带后缀）。

    看门狗的监控/基线集合是文件系统名，而视图条目与编排动作用显示名；
    两套身份的换算必须收口在这里——散落各处的裸比较会造成"已有应用被
    当成新项重新归位"一类保护失效（审计 B01）。
    """
    out = set()
    for name in names:
        p = Path(name)
        out.add(p.stem if p.suffix.lower() in (".lnk", ".url") else name)
    return out


def _vtable(iface):
    """COM 接口指针 -> 可按下标取方法地址的 vtable 数组。"""
    addr = ctypes.cast(iface, ctypes.POINTER(ctypes.c_void_p))[0]
    return ctypes.cast(addr, ctypes.POINTER(ctypes.c_void_p))


def resolve_link_target(path):
    """解析 .lnk 的目标路径；失败或路径不存在返回 None。纯 ctypes COM，不依赖 pywin32。"""
    ole32.CoInitializeEx(None, 0x2)
    link = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(_guid(CLSID_ShellLink)), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(_guid(IID_IShellLinkW)), ctypes.byref(link),
        )
    except OSError:
        return None
    if hr < 0 or not link:
        return None
    release = None
    try:
        vt = _vtable(link)
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt[2])
        query = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)
        )(vt[0])
        get_path = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
        )(vt[3])
        persist = ctypes.c_void_p()
        hr = query(link, ctypes.byref(_guid(IID_IPersistFile)), ctypes.byref(persist))
        if hr < 0 or not persist:
            return None
        pvt = _vtable(persist)
        prelease = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(pvt[2])
        load = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD)(pvt[5])
        try:
            if load(persist, path, 0) < 0:
                return None
            buf = ctypes.create_unicode_buffer(TEXT_CHARS)
            if get_path(link, buf, TEXT_CHARS, None, 0) < 0:
                return None
            return buf.value or None
        except OSError:
            return None  # shell 别名路径（如 {guid}\TaskBar\x.lnk）在磁盘上不存在
        finally:
            prelease(persist)
    finally:
        if release:
            release(link)


def _kind(name, fs):
    if fs is None:
        return "special"
    if fs["hidden"] and fs["system"]:
        return "system"
    if fs["is_dir"]:
        return "folder"
    # 后缀必须取自文件系统路径：.lnk/.url 的显示名不带扩展名
    suffix = Path(fs["path"]).suffix.lower()
    if suffix == ".lnk":
        return "shortcut"
    if suffix == ".url":
        return "url"
    return "file"


def list_items():
    """读取桌面图标清单并与文件系统对齐。只读。"""
    fs = scan_files()
    with IconView() as view:
        items = []
        for i in range(view.count()):
            name = view.text(i)
            x, y = view.position(i)
            entry = fs.get(name)
            kind = _kind(name, entry)
            target = None
            mtime = None
            if entry:
                # 枚举与 stat 之间文件可能消失（含云占位符），退路为 mtime=None
                try:
                    mtime = Path(entry["path"]).stat().st_mtime
                except OSError:
                    mtime = None
            if kind == "shortcut" and entry:
                target = resolve_link_target(entry["path"])
            items.append(
                DesktopItem(
                    index=i,
                    name=name,
                    x=x,
                    y=y,
                    kind=kind,
                    path=entry["path"] if entry else None,
                    target=target,
                    hidden=bool(entry and entry["hidden"]),
                    mtime=mtime,
                )
            )
    return items


def name_counts(items):
    """显示名 -> 出现次数。同名项（用户桌面与公共桌面）无法按名唯一定位。"""
    counts = {}
    for i in items:
        counts[i.name] = counts.get(i.name, 0) + 1
    return counts


def display_width(text):
    """终端显示宽度：东亚宽字符计 2，其余计 1。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text, width):
    return text + " " * max(0, width - display_width(text))


def report(items, steps):
    lines = [f"desktop icons: {len(items)}  steps(col,row)={steps[0]},{steps[1]} (observed, design constants in zones_geometry)"]
    for item in items:
        target = f"  -> {item.target}" if item.target else ""
        lines.append(
            f"{item.index:>2}  {pad(item.name, 34)} {item.kind:<8} ({item.x:>4},{item.y:>4}){target}"
        )
    return "\n".join(lines)


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        items = list_items()
    except DesktopViewUnavailable as exc:
        print(f"desktop-icons: {exc}", file=sys.stderr)
        return 2
    import zones_geometry as geo

    steps = geo.observed_steps([(i.x, i.y) for i in items])
    print(report(items, steps))
    if "--check-geometry" in argv:
        col, row = steps
        problems = []
        if row is not None and row != geo.ROW_STEP:
            problems.append(f"行步长实测 {row} != 常量 {geo.ROW_STEP}")
        if col is not None and col != geo.COL_STEP:
            problems.append(f"列步长实测 {col} != 常量 {geo.COL_STEP}")
        if problems:
            print("geometry drift: " + "; ".join(problems), file=sys.stderr)
            return 1
        print(f"geometry ok: observed col={col} row={row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
