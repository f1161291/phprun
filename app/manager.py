"""任务管理：端口池、子进程生命周期、日志采集。"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Deque, Dict, List, Optional

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
JOBS_DIR = DATA_DIR / "jobs"
LOGS_DIR = DATA_DIR / "logs"
UPLOADS_DIR = DATA_DIR / "uploads"
PIDS_DIR = DATA_DIR / "pids"
MOUNT_DIR = Path(os.environ.get("MOUNT_DIR", "/mount"))

PORT_START = int(os.environ.get("PORT_RANGE_START", "8001"))
PORT_END = int(os.environ.get("PORT_RANGE_END", "8099"))
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8000"))

ENTRY_CANDIDATES = ("main.py", "app.py", "run.py", "server.py", "start.py")
JS_ENTRY_CANDIDATES = ("main.js", "app.js", "index.js", "server.js", "start.js", "run.js")
PHP_ENTRY_CANDIDATES = ("index.php", "main.php", "app.php", "run.php", "server.php",
                        "start.php", "cli.php", "console.php")
ENTRY_BY_SUFFIX = {".php": PHP_ENTRY_CANDIDATES, ".py": ENTRY_CANDIDATES, ".js": JS_ENTRY_CANDIDATES}
RING_SIZE = 3000

# 磁盘日志是单文件 data/logs/<任务ID>.log：行首时间标记分段，每天 04:00 裁掉 LOG_KEEP_DAYS 天前的内容
# （面板启动时也先裁一次）
LOG_KEEP_DAYS = max(1, int(os.environ.get("LOG_KEEP_DAYS", "5")))
_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\]")   # 行首 [日期 时间] 标记


def _log_file(jid: str) -> Path:
    """任务日志单文件：data/logs/<任务ID>.log（一直追加，按段裁剪由 prune_logs 负责）。"""
    return LOGS_DIR / f"{jid}.log"


def _last_marker_day(head: str) -> str:
    """文本里最后一个时间标记的日期（YYYY-MM-DD），没有返回空串。"""
    day = ""
    for ln in head.splitlines():
        m = _TS_RE.match(ln)
        if m:
            day = m.group(1)
    return day


def _ensure_day_marker(path: Path) -> None:
    """跨天后写第一条内容前补一行日期标记，让清理能按天分段裁剪。"""
    today = time.strftime("%Y-%m-%d")
    try:
        if not path.exists():
            return
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            tail = fh.read().decode("utf-8", "replace")
        if _last_marker_day(tail) == today:
            return
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ---- 跨天日志分段 ----\n")
    except OSError:
        pass

# ---------------- cron 定时表达式（标准 5 段：分 时 日 月 周） ----------------
CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
CRON_NAMES = ("分钟", "小时", "日", "月", "周")


def _cron_field(tok: str, lo: int, hi: int, name: str, expr: str) -> set:
    res: set = set()
    for one in tok.split(","):
        one = one.strip()
        if not one:
            raise ValueError(f"cron {name}字段有空项：{expr}")
        step = 1
        if "/" in one:
            one, _, st = one.partition("/")
            try:
                step = int(st)
            except ValueError:
                raise ValueError(f"cron {name}字段步长非法（{st}）：{expr}")
            if step < 1:
                raise ValueError(f"cron {name}字段步长必须 >=1：{expr}")
        if one == "*":
            a, b = lo, hi
        elif "-" in one:
            x, _, y = one.partition("-")
            try:
                a, b = int(x), int(y)
            except ValueError:
                raise ValueError(f"cron {name}字段非法（{one}）：{expr}")
        else:
            try:
                a = int(one)
            except ValueError:
                raise ValueError(f"cron {name}字段非法（{one}）：{expr}")
            b = hi if step > 1 else a          # 5/15 => 5,20,35,50
        if a < lo or b > hi or a > b:
            raise ValueError(f"cron {name}字段越界（{one}，允许 {lo}-{hi}）：{expr}")
        res.update(range(a, b + 1, step))
    if hi == 7:                                 # 周：7 等价于 0（周日）
        if 7 in res:
            res.discard(7)
            res.add(0)
    if not res:
        raise ValueError(f"cron {name}字段为空：{expr}")
    return res


def parse_cron(expr: str):
    """解析 5 段 cron，返回 (5个int集合, 是否为*的标记列表)。非法抛 ValueError。"""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(
            f"cron 需要 5 段（分 时 日 月 周），收到 {len(parts)} 段：{expr}")
    sets = [_cron_field(p, lo, hi, n, expr)
            for p, (lo, hi), n in zip(parts, CRON_RANGES, CRON_NAMES)]
    stars = [p.strip() == "*" for p in parts]
    return sets, stars


def cron_match(parsed, t) -> bool:
    """parsed = parse_cron(...) 或 (表达式, sets, stars) 缓存元组，t = time.localtime()"""
    if len(parsed) == 3:
        _, sets, stars = parsed
    else:
        sets, stars = parsed
    if t.tm_min not in sets[0] or t.tm_hour not in sets[1]:
        return False
    if t.tm_mon not in sets[3]:
        return False
    dom_ok = t.tm_mday in sets[2]
    dow_ok = (t.tm_wday + 1) % 7 in sets[4]      # Python 周一=0 -> cron 周日=0
    if stars[2] and stars[4]:
        day_ok = True
    elif stars[2]:
        day_ok = dow_ok
    elif stars[4]:
        day_ok = dom_ok
    else:
        day_ok = dom_ok or dow_ok                # 标准 cron：日/周都受限时取或
    return day_ok


def validate_schedule(expr: str) -> str:
    """校验定时表达式，空串=不启用。非法抛 ValueError。"""
    s = (expr or "").strip()
    if not s:
        return ""
    parse_cron(s)
    return s


# 缺失依赖自动安装（日志里出现 ModuleNotFoundError / Class not found 时自动安装后自动重启一次）
AUTO_DEPS = os.environ.get("AUTO_INSTALL_DEPS", "1").strip().lower() not in ("0", "false", "no", "off")
PIP_INDEX_URL = os.environ.get("PIP_INDEX_URL", "").strip()
AUTO_DEPS_MAX = 3

# 指定端口释放等待：stop 之后残余进程（如 php -S 的 worker）可能还占着监听，
# 分配前最多等这么久；超过才报「已被占用」
PORT_RELEASE_WAIT = float(os.environ.get("PORT_RELEASE_WAIT", "5"))

# composer（PHP 依赖）
COMPOSER_MIRROR = os.environ.get("COMPOSER_MIRROR", "").strip()
COMPOSER_HOME = os.environ.get("COMPOSER_HOME", "").strip()
AUTO_COMPOSER = AUTO_DEPS            # composer 自动 install / require 跟随同一开关

# import 名 -> PyPI 包名（不在这张表里就直接拿 import 名当包名）
PYPI_ALIASES = {
    "PIL": "Pillow", "cv2": "opencv-python", "yaml": "PyYAML", "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn", "skimage": "scikit-image", "dateutil": "python-dateutil",
    "Crypto": "pycryptodomex", "Cryptodome": "pycryptodomex", "dotenv": "python-dotenv",
    "websocket": "websocket-client", "serial": "pyserial", "wx": "wxPython",
    "usb": "pyusb", "gi": "PyGObject", "jwt": "PyJWT", "MySQLdb": "mysqlclient",
    "psycopg2": "psycopg2-binary", "pptx": "python-pptx", "docx": "python-docx",
    "fitz": "PyMuPDF", "tesseract": "pytesseract", "pypdf": "pypdf",
    "PyPDF2": "PyPDF2", "IPython": "ipython", "cairosvg": "cairosvg",
    "cairo": "pycairo", "dbus": "dbus-python", "ldap": "python-ldap",
    "magic": "python-magic", "slugify": "python-slugify", "markdown": "Markdown",
    "numpy": "numpy", "pandas": "pandas", "requests": "requests", "httpx": "httpx",
    "aiohttp": "aiohttp", "flask": "Flask", "fastapi": "fastapi", "uvicorn": "uvicorn",
    "django": "Django", "sqlalchemy": "SQLAlchemy", "lxml": "lxml", "tqdm": "tqdm",
    "jieba": "jieba", "selenium": "selenium", "playwright": "playwright",
    "openpyxl": "openpyxl", "matplotlib": "matplotlib", "pyecharts": "pyecharts",
    "redis": "redis", "pymongo": "pymongo", "paramiko": "paramiko",
}
_MISSING_RE = re.compile(r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]")
# 合法 PyPI 项目名（pip/PEP 508 要求以字母或数字开头）——下划线开头的多是本地模块名，装不了也别装
_PYPI_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# PHP：缺 vendor/autoload.php 或类未找到
#   两种真实报错形态：
#   Warning: require(vendor/autoload.php): failed to open stream: No such file or directory
#   Fatal error: require_once(...): Failed opening required 'vendor/autoload.php'
_PHP_AUTOLOAD_RE = re.compile(r"vendor[/\\]autoload\.php", re.I)
_PHP_FAIL_WORDS = ("failed", "no such file", "not found")
_PHP_CLASS_RE = re.compile(r"Class ['\"]([^'\"]+)['\"] not found")

# import/use 类名前缀 -> composer 包名（不在表里则按 Vendor\Package 同名规则猜测）
PHP_PKG_ALIASES = {
    "GuzzleHttp": "guzzlehttp/guzzle",
    "Monolog": "monolog/monolog",
    "Twig": "twig/twig",
    "Smarty": "smarty/smarty",
    "PHPMailer": "phpmailer/phpmailer",
    "PHPUnit": "phpunit",
    "Carbon": "nesbot/carbon",
    "Dompdf": "dompdf/dompdf",
    "PhpOffice\\PhpSpreadsheet": "phpoffice/phpspreadsheet",
    "PhpSpreadsheet": "phpoffice/phpspreadsheet",
    "Intervention\\Image": "intervention/image",
    "Intervention": "intervention/image",
    "Firebase\\JWT": "firebase/php-jwt",
    "Doctrine\\ORM": "doctrine/orm",
    "Doctrine\\DBAL": "doctrine/dbal",
    "Symfony\\Component\\Console": "symfony/console",
    "Symfony\\Component\\HttpFoundation": "symfony/http-foundation",
    "Symfony\\Component\\HttpClient": "symfony/http-client",
    "Symfony\\Component\\Routing": "symfony/routing",
    "Symfony\\Component\\EventDispatcher": "symfony/event-dispatcher",
    "Symfony\\Component\\DependencyInjection": "symfony/dependency-injection",
    "Symfony\\Component\\Validator": "symfony/validator",
    "Symfony\\Component\\Serializer": "symfony/serializer",
    "Symfony\\Component\\Security\\Csrf": "symfony/security-csrf",
    "Illuminate": "laravel/framework",
}


def php_class_to_pkg(cls: str) -> str:
    """PHP 类名 → 猜测的 composer 包名；猜不出返回空串。"""
    segs = [s for s in cls.split("\\") if s]
    if not segs:
        return ""
    for n in (4, 3, 2, 1):                      # 长前缀优先：Symfony\Component\Security\Csrf
        if len(segs) >= n:
            pkg = PHP_PKG_ALIASES.get("\\".join(segs[:n]))
            if pkg:
                return pkg
    # Symfony\Component\Xxx → symfony/xxx（CamelCase 转连字符）
    if segs[0] == "Symfony" and len(segs) >= 3 and segs[1] in ("Component", "Contracts"):
        pkg = "symfony/" + re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", segs[2]).lower()
        return pkg
    # Vendor\Package\Rest 且 vendor == package（忽略大小写）→ vendor/package
    if len(segs) >= 2 and segs[0].lower() == segs[1].lower():
        return f"{segs[0].lower()}/{segs[1].lower()}"
    return ""


def _ensure_dirs() -> None:
    for d in (JOBS_DIR, LOGS_DIR, UPLOADS_DIR, PIDS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if COMPOSER_HOME:
        Path(COMPOSER_HOME).mkdir(parents=True, exist_ok=True)


def port_free(port: int) -> bool:
    if port == PANEL_PORT:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def port_pids(port: int) -> List[int]:
    """找出正在 LISTEN 该端口的进程 pid（解析 /proc，容器里没有 ss/lsof 也能用）。"""
    inodes = _listen_inodes(port)
    if not inodes:
        return []
    pids: List[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return []
    for p in entries:
        if not p.name.isdigit():
            continue
        try:
            fds = list((p / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                tgt = os.readlink(fd)
            except OSError:
                continue
            if tgt.startswith("socket:[") and tgt[8:-1] in inodes:
                pids.append(int(p.name))
                break
    return pids


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _listen_inodes(port: int) -> set:
    inodes: set = set()
    for name in ("tcp", "tcp6"):
        try:
            lines = Path(f"/proc/net/{name}").read_text(encoding="utf-8", errors="replace").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            cols = line.split()
            if len(cols) < 10 or cols[3] != "0A":          # 0A = LISTEN
                continue
            try:
                if int(cols[1].rsplit(":", 1)[1], 16) != port:
                    continue
            except (ValueError, IndexError):
                continue
            inodes.add(cols[9])
    return inodes


def _tcp_states(port: int) -> List[str]:
    """该端口上非 LISTEN 的 TCP 连接状态（用来解释「没有监听者却 bind 失败」）。"""
    names = {"01": "ESTABLISHED", "02": "SYN_SENT", "04": "FIN_WAIT1", "05": "FIN_WAIT2",
             "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT", "09": "LAST_ACK",
             "0B": "CLOSING"}
    out: List[str] = []
    for name in ("tcp", "tcp6"):
        try:
            lines = Path(f"/proc/net/{name}").read_text(encoding="utf-8", errors="replace").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            cols = line.split()
            if len(cols) < 4 or cols[3] == "0A":
                continue
            try:
                if int(cols[1].rsplit(":", 1)[1], 16) == port:
                    out.append(names.get(cols[3], cols[3]))
            except (ValueError, IndexError):
                continue
    return out


def port_occupant(port: int) -> str:
    """占用端口的进程描述（报错时告诉用户到底是谁占着）；查不到就退回「系统进程」。"""
    pids = port_pids(port)
    if pids:
        bits = []
        for pid in pids[:3]:
            cmd = _cmdline(pid)
            bits.append(f"pid {pid}" + (f"（{cmd[:70]}）" if cmd else ""))
        return "、".join(bits)
    states = _tcp_states(port)
    if states:
        return "残留连接（" + "/".join(sorted(set(states))) \
            + "，多为反代刚断开的长连接，稍等片刻会自行释放）"
    return "系统进程"


def _close_own_listeners(port: int) -> int:
    """关掉本进程自己 LISTEN 在该端口上的 socket（反代对象丢了引用时的自愈），返回关闭数量。"""
    inodes = _listen_inodes(port)
    if not inodes:
        return 0
    n = 0
    try:
        fds = list(Path("/proc/self/fd").iterdir())
    except OSError:
        return 0
    for fd in fds:
        try:
            tgt = os.readlink(fd)
        except OSError:
            continue
        if tgt.startswith("socket:[") and tgt[8:-1] in inodes:
            try:
                os.close(int(fd.name))
                n += 1
            except (OSError, ValueError):
                pass
    return n


# ---------- 残留进程清理 ----------
def _pid_path(jid: str) -> Path:
    return PIDS_DIR / f"{jid}.pid"


def _alive(pid: int) -> bool:
    """存活判断：僵尸进程视为已死亡，避免无谓等待。"""
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            state = fh.read().split(")")[-1].split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return False


def _proc_matches(pid: int, workdir: str) -> bool:
    """确认 pid 确实是我们启动的任务进程，避免误杀复用 pid 的无关进程。"""
    if not workdir:
        return False
    wd = str(Path(workdir))
    try:
        cl = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "replace").replace("\0", " ")
    except OSError:
        return False
    if wd in cl:
        return True
    try:
        cwd = os.path.realpath(f"/proc/{pid}/cwd")
    except OSError:
        return False
    return cwd == wd or cwd.startswith(wd.rstrip("/") + "/")


def _kill_tree(pid: int, timeout: float = 4.0) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return
        time.sleep(0.2)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def reap_stale() -> List[int]:
    """面板启动时回收上次遗留的任务进程。"""
    killed = []
    for f in sorted(PIDS_DIR.glob("*.pid")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        pid = int(data.get("pid") or 0)
        wd = str(data.get("workdir") or "")
        if pid and pid != os.getpid() and _alive(pid) and _proc_matches(pid, wd):
            _kill_tree(pid)
            killed.append(pid)
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
    return killed


# ---------------- 轻量 TCP 反代（脚本固定端口 → 面板对外端口） ----------------
class TcpProxy:
    """把对外端口的 TCP 流量转发到 127.0.0.1:内部端口，脚本无需改监听端口。"""

    def __init__(self, listen_port: int, target_port: int, host: str = "127.0.0.1") -> None:
        self.listen_port = listen_port
        self.target = (host, target_port)
        self._lsock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._conns: List[socket.socket] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.listen_port))
        sock.listen(64)
        sock.settimeout(0.5)
        self._lsock = sock
        self._stopping.clear()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            lsock = self._lsock
            if lsock is None:
                break
            try:
                client, _addr = lsock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(self.target, timeout=10)
        except OSError:
            try:
                client.close()
            except OSError:
                pass
            return
        for s in (client, upstream):
            try:
                s.settimeout(None)
            except OSError:
                pass
        with self._lock:
            self._conns.extend((client, upstream))
        threading.Thread(target=self._pipe, args=(client, upstream), daemon=True).start()
        self._pipe(upstream, client)
        for s in (client, upstream):
            try:
                s.close()
            except OSError:
                pass
            with self._lock:
                if s in self._conns:
                    self._conns.remove(s)

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def stop(self) -> None:
        self._stopping.set()
        if self._lsock is not None:
            try:
                self._lsock.close()
            except OSError:
                pass
            self._lsock = None
        with self._lock:
            conns, self._conns = self._conns, []
        for s in conns:
            try:
                s.close()
            except OSError:
                pass


@dataclass
class Job:
    id: str
    name: str
    source: str                      # upload | mount
    workdir: str                     # 任务 cwd（绝对路径）
    entry: str = ""                  # 相对 workdir 的入口文件
    cmd: str = ""                    # 自定义命令，为空则 python -u <entry>
    auto_start: bool = False         # 面板启动时是否自动拉起
    port: int = 0
    port_pref: int = 0               # 指定端口，0=自动分配（在端口段内优先占用）
    proxy_to: int = 0                 # 反代目标：脚本自身监听的内部端口，0=不反代
    schedule: str = ""               # cron 定时表达式（5 段），空=不启用
    mode: str = "auto"               # PHP 运行模式 auto|cli|web（对 .py/.js 无效）
    status: str = "created"          # created/starting/running/stopped/exited/error
    pid: int = 0
    exit_code: Optional[int] = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    env: Dict[str, str] = field(default_factory=dict)
    _proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    _tail: Deque[str] = field(default_factory=lambda: collections.deque(maxlen=RING_SIZE), repr=False)
    _deps_done: bool = field(default=False, repr=False)       # 本轮运行是否已触发过自动装依赖
    _deps_attempts: int = field(default=0, repr=False)        # 自动装依赖累计次数（防死循环）
    _deps_notes: set = field(default_factory=set, repr=False)  # 已提示过的「跳过安装」话术（防刷屏）
    _last_fire: str = field(default="", repr=False)           # 定时任务上次触发的 "年-月 日:分"
    _cron_cache: object = field(default=None, repr=False)     # 解析后的 cron（表达式, sets, stars）
    _last_argv: List[str] = field(default_factory=list, repr=False)  # 上次启动的完整命令行（用于清理残余）
    _proxy: Optional[TcpProxy] = field(default=None, repr=False)  # 反代监听器
    _composer_dir: str = field(default="", repr=False)          # 含 composer.json 的目录

    def public(self) -> dict:
        uptime = round(time.time() - self.started_at) if self.started_at and self.status == "running" else 0
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "entry": self.entry,
            "cmd": self.cmd,
            "auto_start": self.auto_start,
            "schedule": self.schedule,
            "mode": self.mode or "auto",
            "workdir": self.workdir,
            "port": self.port,
            "port_pref": self.port_pref,
            "proxy_to": self.proxy_to,
            "url": f"http://<host>:{self.port}" if self.port else "",
            "status": self.status,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "uptime": uptime,
        }


class JobManager:
    def __init__(self) -> None:
        _ensure_dirs()
        self.jobs: Dict[str, Job] = {}
        self.lock = threading.RLock()
        self._free_ports: List[int] = [p for p in range(PORT_START, PORT_END + 1)]
        self._restore()
        self._reaped = reap_stale()
        threading.Thread(target=self._ensure_composer_mirror, daemon=True).start()
        try:
            self.prune_logs()                          # 启动时先清一次（补上停机期间漏掉的清理）
        except Exception:                              # noqa: BLE001
            pass
        threading.Thread(target=self._log_sweeper, daemon=True).start()
        self._watcher = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()

    def _restore(self) -> None:
        """面板重启后，从磁盘恢复任务登记（进程已不在，标记为 stopped）。"""
        import json
        seen: set = set()
        reg = DATA_DIR / "registry.json"
        sources = []
        if reg.exists():
            try:
                sources.append(json.loads(reg.read_text(encoding="utf-8")))
            except Exception:
                pass
        sources.append([json.loads(p.read_text(encoding="utf-8"))
                        for p in sorted(JOBS_DIR.glob("*/.job.json"))
                        if p.read_text(encoding="utf-8").strip()])
        for group in sources:
            if isinstance(group, dict):
                group = [group]
            for data in group:
                try:
                    jid = data["id"]
                    if jid in seen:
                        continue
                    if not Path(data.get("workdir", "")).exists():
                        continue
                    job = Job(
                        id=jid, name=data.get("name") or jid,
                        source=data.get("source", "upload"),
                        workdir=data["workdir"],
                        entry=data.get("entry", ""), cmd=data.get("cmd", ""),
                        auto_start=bool(data.get("auto_start")),
                        port_pref=int(data.get("port_pref") or 0),
                        proxy_to=int(data.get("proxy_to") or 0),
                        schedule=str(data.get("schedule") or ""),
                        mode=str(data.get("mode") or "auto"),
                    )
                    job.status = "stopped"
                    job.created_at = data.get("created_at") or time.time()
                    self.jobs[jid] = job
                    seen.add(jid)
                except Exception:
                    continue

    def persist(self) -> None:
        import json
        with self.lock:
            data = [{k: v for k, v in j.public().items()
                     if k in ("id", "name", "source", "workdir", "entry", "cmd",
                              "auto_start", "port_pref", "proxy_to", "schedule",
                              "mode", "created_at")}
                    for j in self.jobs.values()]
        try:
            (DATA_DIR / "registry.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ---------- 端口池 ----------
    def _alloc_port(self, prefer: int = 0) -> int:
        if prefer:
            if prefer not in range(PORT_START, PORT_END + 1):
                raise RuntimeError(
                    f"指定端口 {prefer} 不在面板端口段 {PORT_START}-{PORT_END} 内")
            with self.lock:
                if prefer not in self._free_ports:
                    raise RuntimeError(f"指定端口 {prefer} 已被其他任务占用")
            # 刚停止的任务可能还有残余（php -S 的 worker 等）没放开监听，最多等一会让它退
            deadline = time.time() + PORT_RELEASE_WAIT
            while not port_free(prefer):
                if time.time() >= deadline:
                    raise RuntimeError(
                        f"指定端口 {prefer} 已被占用：{port_occupant(prefer)}")
                time.sleep(0.2)
            with self.lock:
                if prefer not in self._free_ports:
                    raise RuntimeError(f"指定端口 {prefer} 已被其他任务占用")
                self._free_ports.remove(prefer)
                return prefer
        with self.lock:
            skipped: List[int] = []
            got = 0
            while self._free_ports:
                p = self._free_ports.pop(0)
                if port_free(p):
                    got = p
                    break
                skipped.append(p)
            self._free_ports.extend(skipped)      # 被外部占着的放回池里，别丢
            if got:
                return got
        raise RuntimeError(f"端口池已耗尽 ({PORT_START}-{PORT_END})")

    def _release_port(self, port: int) -> None:
        if not port:
            return
        with self.lock:
            if port in range(PORT_START, PORT_END + 1) and port not in self._free_ports:
                self._free_ports.append(port)
                self._free_ports.sort()

    def _stop_proxy(self, job: Job) -> None:
        proxy = job._proxy
        if proxy is not None:
            try:
                proxy.stop()
            except Exception:
                pass
            job._proxy = None

    def _reap_leftover(self, job: Job, port: int) -> None:
        """清理上一轮本任务残留、还占着指定端口的监听进程。

        只对「正在 LISTEN 该端口」的进程动手，且满足其一：
          - 命令行与上次启动完全一致（残余主进程）
          - cwd 在本任务工作目录内（脚本自己 fork/setsid 出来的子进程）
        其他任务的主进程、以及不属于本工作目录的进程一律不碰，只在报错里指出来。
        """
        if not port:
            return
        # 1) 本任务自己的反代还占着（stop 没走到）→ 正常停掉并归还端口
        proxy = job._proxy
        if proxy is not None and getattr(proxy, "listen_port", 0) == port:
            self._stop_proxy(job)
            self._release_port(port)
        if port_free(port):
            return
        # 2) 面板自己还 LISTEN 着但对象引用丢了（登记上端口是空闲的）→ 关掉泄漏的 fd 自愈
        with self.lock:
            free_by_registry = port in self._free_ports
        if free_by_registry and _close_own_listeners(port) and port_free(port):
            return
        want = " ".join(job._last_argv) if job._last_argv else ""
        try:
            wd = str(Path(job.workdir).resolve())
        except OSError:
            wd = job.workdir
        others = {j._proc.pid for j in list(self.jobs.values())
                  if j._proc is not None and j.id != job.id}

        def _mine(pid: int) -> bool:
            if pid in others or pid == os.getpid():
                return False
            if want and _cmdline(pid) == want:
                return True
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                return False
            return cwd == wd or cwd.startswith(wd + "/")

        targets = [pid for pid in port_pids(port) if _mine(pid)]
        if not targets:
            return

        def _kill(sig: int) -> None:
            for pid in targets:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass

        _kill(signal.SIGTERM)
        deadline = time.time() + 2.0
        while not port_free(port) and time.time() < deadline:
            time.sleep(0.2)
        if not port_free(port):
            _kill(signal.SIGKILL)
            deadline = time.time() + 2.0
            while not port_free(port) and time.time() < deadline:
                time.sleep(0.2)

    # ---------- 创建 ----------
    def create(self, *, name: str, source: str, workdir: str, entry: str = "",
               cmd: str = "", env: Optional[Dict[str, str]] = None,
               port_pref: int = 0, proxy_to: int = 0, schedule: str = "",
               mode: str = "auto") -> Job:
        jid = uuid.uuid4().hex[:12]
        job = Job(id=jid, name=name or jid, source=source, workdir=str(workdir),
                  entry=entry, cmd=cmd, env=env or {}, port_pref=port_pref,
                  proxy_to=proxy_to, schedule=schedule, mode=mode or "auto")
        with self.lock:
            self.jobs[jid] = job
        self.persist()
        return job

    def get(self, jid: str) -> Job:
        with self.lock:
            job = self.jobs.get(jid)
        if not job:
            raise KeyError(f"任务不存在: {jid}")
        return job

    def list(self) -> List[dict]:
        with self.lock:
            items = [j.public() for j in self.jobs.values()]
        return sorted(items, key=lambda x: -x["created_at"])

    # ---------- 入口解析 ----------
    def resolve_entry(self, job: Job) -> Path:
        wd = Path(job.workdir)
        if not wd.is_dir():
            raise RuntimeError(f"工作目录不存在: {wd}")
        if job.cmd:
            return wd
        if job.entry:
            p = (wd / job.entry).resolve()
            if not str(p).startswith(str(wd.resolve())):
                raise RuntimeError("入口文件必须位于工作目录内")
            if not p.is_file():
                raise RuntimeError(f"入口文件不存在: {job.entry}")
            return p

        def _scan(pattern: str) -> List[Path]:
            return [p for p in sorted(wd.rglob(pattern))
                    if not any(part.startswith(".") for part in p.relative_to(wd).parts)]

        phps, pys, jss = _scan("*.php"), _scan("*.py"), _scan("*.js")
        if not phps and not pys and not jss:
            raise RuntimeError("工作目录中没有 .php / .py / .js 文件")

        # 语言优先级：PHP > Python > Node
        files = phps or pys or jss
        suffix = files[0].suffix
        cands = ENTRY_BY_SUFFIX[suffix]

        # 1) 顶层惯用入口名
        for cand in cands:
            if (wd / cand).is_file():
                return wd / cand
        # 2) 顶层唯一的脚本
        top = [p for p in files if p.parent == wd]
        if len(top) == 1:
            return top[0]
        # 3) 全目录内唯一的惯用入口名（zip/目录包裹一层的情况）
        hits = [p for p in files if p.name in cands]
        if hits:
            hits.sort(key=lambda p: (len(p.relative_to(wd).parts), str(p)))
            return hits[0]
        # 4) 全目录唯一的脚本
        if len(files) == 1:
            return files[0]
        rels = ", ".join(str(p.relative_to(wd)) for p in files[:8])
        raise RuntimeError(
            f"无法确定入口：找到 {len(files)} 个 {suffix}（{rels}...）。"
            "请在任务编辑框的「入口文件」里填写相对工作目录的路径，如 index.php 或 myapp/main.py")

    # ---------- composer（PHP 依赖） ----------
    @staticmethod
    def _composer_bin() -> str:
        return shutil.which("composer") or "composer"

    @classmethod
    def _composer_env(cls) -> Dict[str, str]:
        env = dict(os.environ)
        env["COMPOSER_ALLOW_SUPERUSER"] = "1"
        if COMPOSER_HOME:
            env["COMPOSER_HOME"] = COMPOSER_HOME
        if COMPOSER_MIRROR:
            env["COMPOSER_MIRROR"] = COMPOSER_MIRROR
        return env

    @classmethod
    def run_composer(cls, workdir, args, timeout: int = 600):
        """在 workdir 执行 composer <args>，返回 (returncode, 输出)。"""
        cmd = [cls._composer_bin()] + [str(a) for a in args]
        try:
            p = subprocess.run(cmd, cwd=str(workdir), env=cls._composer_env(),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, errors="replace", timeout=timeout)
            return p.returncode, (p.stdout or "")
        except FileNotFoundError:
            return 127, "composer 未安装；请重建镜像：docker compose up -d --build"
        except subprocess.TimeoutExpired:
            return 124, f"composer 超时（{timeout} 秒）"
        except Exception as e:                               # noqa: BLE001
            return 1, str(e)

    @classmethod
    def composer_needed(cls, workdir) -> bool:
        """有 composer.json 且 vendor/autoload.php 缺失或比 composer.lock 旧。"""
        wd = Path(workdir)
        if not wd.is_dir() or not (wd / "composer.json").is_file():
            return False
        autoload = wd / "vendor/autoload.php"
        if not autoload.is_file():
            return True
        lock = wd / "composer.lock"
        try:
            return lock.is_file() and lock.stat().st_mtime > autoload.stat().st_mtime
        except OSError:
            return False

    @classmethod
    def _ensure_composer_mirror(cls) -> None:
        """面板启动时把 packagist 镜像源写进全局配置（幂等，/data 是挂载卷）。"""
        if not COMPOSER_MIRROR or not shutil.which("composer"):
            return
        try:
            if COMPOSER_HOME:
                Path(COMPOSER_HOME).mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["composer", "config", "-g", "repos.packagist", "composer", COMPOSER_MIRROR],
                env=cls._composer_env(), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=60)
            subprocess.run(
                ["composer", "config", "-g", "process-timeout", "600"],
                env=cls._composer_env(), stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=60)
        except Exception:                                    # noqa: BLE001
            pass

    @staticmethod
    def composer_project(job: Job, entry: Path) -> Path:
        """含 composer.json 的目录：优先入口所在层，再退回工作目录（兼容 zip 包一层目录）。"""
        wd = Path(job.workdir)
        cands = []
        if entry.is_file():
            cands.append(entry.parent)
        cands.append(wd)
        for d in cands:
            try:
                d.resolve().relative_to(wd.resolve())
            except (ValueError, OSError):
                continue
            if (d / "composer.json").is_file():
                return d
        return wd

    def composer_ensure(self, job: Job, project: Optional[Path] = None) -> bool:
        """启动前：vendor 缺失/过期则 composer install。失败只记日志，不阻断启动。"""
        project = Path(project or job.workdir)
        if not AUTO_COMPOSER or not self.composer_needed(project):
            return True
        self._log_note(job, f"[composer] 检测到 {project}/composer.json，先执行 composer install ...")
        code, out = self.run_composer(
            project, ["install", "--no-interaction", "--no-ansi",
                      "--prefer-dist", "--no-progress"], timeout=900)
        tail = "\n".join((out or "").strip().splitlines()[-25:])
        if tail:
            self._log_note(job, tail)
        if code != 0:
            self._log_note(
                job,
                f"[composer] composer install 失败 (exit {code})，继续尝试启动；"
                f"若运行报缺类，去「包管理」页处理")
            return False
        self._log_note(job, "[composer] 依赖安装完成")
        return True

    # ---------- 启动 ----------
    def start(self, jid: str, *, fresh: bool = True) -> Job:
        job = self.get(jid)
        with self.lock:
            if job.status == "running" and job._proc and job._proc.poll() is None:
                raise RuntimeError("任务已在运行")
        if fresh:
            job._deps_attempts = 0          # 手动启动 → 重置自动装依赖计数
        job._deps_done = False              # 每轮运行最多触发一次
        job._deps_notes.clear()             # 跳过安装的提示下一轮可再报

        entry = self.resolve_entry(job)
        if not job.cmd and entry.is_file():
            if entry.suffix == ".js" and not shutil.which("node"):
                raise RuntimeError(
                    "该任务是 .js 脚本，但环境里没有 Node.js；请重建镜像：docker compose up -d --build")
            if entry.suffix == ".php" and not shutil.which("php"):
                raise RuntimeError(
                    "该任务是 .php 脚本，但环境里没有 PHP；请重建镜像：docker compose up -d --build")

        # PHP 项目：启动前自动 composer install（可能耗时，日志里可见）
        job.status = "starting"
        job._composer_dir = str(self.composer_project(job, entry))
        self.composer_ensure(job, Path(job._composer_dir))

        # 上一轮本任务的残余进程（脚本自己 fork/setsid 的那种）可能还占着指定端口，先清掉
        self._reap_leftover(job, job.port_pref)

        try:
            port = self._alloc_port(job.port_pref)
        except Exception as e:
            job.status = "error"
            job.error = str(e)
            raise RuntimeError(job.error) from e

        env = dict(os.environ)
        env.update({k: str(v) for k, v in job.env.items()})
        env.update({
            "PORT": str(port),
            "HOST": env.get("DEFAULT_HOST", "0.0.0.0"),
            "JOB_ID": job.id,
            "JOB_NAME": job.name,
            "PYTHONUNBUFFERED": "1",
        })

        run_note = ""                     # 写进日志头的运行方式说明
        if job.cmd:
            argv = ["/bin/sh", "-c", job.cmd]
            cwd = str(entry)
        elif entry.is_file() and entry.suffix == ".js":
            argv = [shutil.which("node") or "node", str(entry)]
            cwd = str(entry.parent)
        elif entry.is_file() and entry.suffix == ".php":
            mode = (job.mode or "auto").strip().lower()
            if mode not in ("auto", "cli", "web"):
                mode = "auto"
            wd = Path(job.workdir)
            if mode == "auto":
                # 有 index.php（顶层或 public/）→ Web 内置服务器；否则当 CLI 脚本跑
                mode = "web" if (wd / "index.php").is_file() \
                    or (wd / "public" / "index.php").is_file() else "cli"
            if mode == "web":
                docroot = wd / "public" if (wd / "public" / "index.php").is_file() else wd
                argv = ["php", "-S", f"{env['HOST']}:{port}", "-t", str(docroot)]
                # 入口不是 index.php 时，把它当路由器（所有请求交它处理）
                if entry.name != "index.php":
                    argv.append(str(entry))
                cwd = str(docroot)
                run_note = f" | PHP web {env['HOST']}:{port} → {docroot}"
            else:
                argv = ["php", "-d", "display_errors=1", "-d", "log_errors=0",
                        "-d", "error_reporting=E_ALL", str(entry)]
                cwd = str(entry.parent)
                run_note = " | PHP cli"
        else:
            argv = [sys.executable, "-u", str(entry)]
            cwd = str(entry.parent) if entry.is_file() else str(entry)

        log_path = _log_file(job.id)
        job.exit_code = None
        job.error = ""
        job.port = port
        job.status = "starting"
        job._last_argv = list(argv)

        # 反代：对外端口 → 脚本自身监听的内部端口（脚本不用改端口）
        self._stop_proxy(job)
        if job.proxy_to and job.proxy_to != port:
            try:
                proxy = TcpProxy(port, job.proxy_to)
                proxy.start()
                job._proxy = proxy
            except OSError as e:
                self._release_port(port)
                job.status = "error"
                job.error = f"反代监听端口 {port} 失败: {e}"
                raise RuntimeError(job.error) from e

        try:
            fh: IO[str] = open(log_path, "a", encoding="utf-8", buffering=1)
        except OSError as e:
            self._stop_proxy(job)
            self._release_port(port)
            job.status = "error"
            job.error = f"无法写日志: {e}"
            raise

        proxy_note = (f" | 反代 {port} → 127.0.0.1:{job.proxy_to}"
                      if job.proxy_to and job.proxy_to != port else "")
        header = (f"\n{'='*70}\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                  f"启动 {job.name} | 端口 {port}{proxy_note}{run_note} | {' '.join(argv)}\n{'='*70}\n")
        fh.write(header)
        self._push(job, header)

        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1,
                start_new_session=True,
            )
        except Exception as e:
            fh.close()
            self._stop_proxy(job)
            self._release_port(port)
            job.status = "error"
            job.error = str(e)
            raise

        job._proc = proc
        job.pid = proc.pid
        job.started_at = time.time()
        job.status = "running"
        try:
            _pid_path(job.id).write_text(
                json.dumps({"pid": proc.pid, "workdir": job.workdir}), encoding="utf-8")
        except OSError:
            pass

        threading.Thread(target=self._pump, args=(job, proc, fh), daemon=True).start()
        return job

    def _push(self, job: Job, line: str) -> None:
        job._tail.append(line)

    def _pump(self, job: Job, proc: subprocess.Popen, fh: IO[str]) -> None:
        day = time.strftime("%Y-%m-%d")
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                cur = time.strftime("%Y-%m-%d")
                if cur != day:                       # 跨天：先落一行日期标记，再继续追加
                    try:
                        fh.write(
                            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ---- 跨天日志分段 ----\n")
                    except Exception:                # noqa: BLE001
                        pass
                    day = cur
                fh.write(line)
                self._push(job, line)
                if AUTO_DEPS and not job._deps_done:
                    self._auto_deps_probe(job, line)
        except Exception:
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass

    # ---------- 缺失依赖自动安装 ----------
    def _log_note(self, job: Job, text: str) -> None:
        """把提示写进内存日志环 + 磁盘日志文件。"""
        for ln in text.splitlines():
            self._push(job, ln)
        path = _log_file(job.id)
        _ensure_day_marker(path)
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(text if text.endswith("\n") else text + "\n")
        except OSError:
            pass

    def _note_once(self, job: Job, text: str) -> None:
        """「跳过安装」类提示每轮只写一次，避免循环 import 把日志刷爆。"""
        if text in job._deps_notes:
            return
        job._deps_notes.add(text)
        self._log_note(job, text)

    def _local_module(self, job: Job, top: str) -> str:
        """本地是否已有同名模块：工作目录或入口所在目录下的 top.py / top/__init__.py。"""
        bases = [Path(job.workdir)]
        try:
            entry = self.resolve_entry(job)
            if entry.is_file() and entry.parent not in bases:
                bases.append(entry.parent)
        except Exception:                                    # noqa: BLE001
            pass
        for b in bases:
            py = b / f"{top}.py"
            if py.is_file():
                return str(py)
            pkg = b / top / "__init__.py"
            if pkg.is_file():
                return str(pkg.parent)
        return ""

    def _auto_install(self, job: Job, mod: str) -> None:
        top = mod.split(".")[0]
        pkg = PYPI_ALIASES.get(top, top)
        if not pkg:
            return
        # 闸1：工作目录/入口目录里本来就有它 → 是本地模块/路径问题，不是缺包
        local = self._local_module(job, top)
        if local:
            self._note_once(
                job,
                f"[自动装依赖] 发现本地模块 {local}，跳过 pip install"
                f" —— 请把入口脚本与它放同一目录，或检查 sys.path / 工作目录")
            return
        # 闸2：不是合法 PyPI 项目名（如 _credentials 这种下划线开头的）→ 装不了也别装
        if not _PYPI_NAME_RE.match(pkg):
            self._note_once(
                job,
                f"[自动装依赖] {top} 不是合法的 PyPI 包名（需以字母/数字开头），跳过安装"
                f" —— 这通常是本地模块名或 import 拼写错误，不是第三方包")
            return
        if job._deps_done:
            return
        job._deps_done = True
        if job._deps_attempts >= AUTO_DEPS_MAX:
            self._log_note(
                job,
                f"[自动装依赖] 已达最大重试次数 {AUTO_DEPS_MAX}，跳过 {mod}；"
                f"请到「包管理」页手动 pip install {pkg} 再启动")
            return
        job._deps_attempts += 1
        self._log_note(
            job,
            f"[自动装依赖] 缺失模块 {mod} → pip install {pkg}"
            f"（第 {job._deps_attempts}/{AUTO_DEPS_MAX} 次，可能需要几十秒）")
        threading.Thread(
            target=self._install_worker, args=(job.id, mod, pkg), daemon=True).start()

    def _install_worker(self, jid: str, mod: str, pkg: str) -> None:
        try:
            job = self.get(jid)
        except KeyError:
            return
        env = dict(os.environ)
        if PIP_INDEX_URL:
            env["PIP_INDEX_URL"] = PIP_INDEX_URL
        cmd = [sys.executable, "-m", "pip", "install",
               "--disable-pip-version-check", "--no-input", pkg]
        cwd = job.workdir if Path(job.workdir).is_dir() else None
        try:
            p = subprocess.run(
                cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, errors="replace", timeout=900)
        except subprocess.TimeoutExpired:
            self._log_note(job, "[自动装依赖] pip install 超时（15 分钟），放弃")
            return
        except Exception as e:
            self._log_note(job, f"[自动装依赖] pip 执行失败: {e}")
            return

        out = (p.stdout or "").strip().splitlines()
        if out:
            self._log_note(job, "\n".join(out[-15:]))
        if p.returncode != 0:
            self._log_note(
                job,
                f"[自动装依赖] pip install {pkg} 失败 (exit {p.returncode})，"
                f"请到「包管理」页手动安装后重启")
            try:
                job = self.get(jid)
                if job.status not in ("running", "starting"):
                    job.status = "error"
                    job.error = f"缺少依赖 {pkg}，自动安装失败"
            except KeyError:
                pass
            return

        try:
            job = self.get(jid)
        except KeyError:
            return
        if job.status in ("stopping", "stopped"):
            self._log_note(job, "[自动装依赖] 任务已被停止，跳过自动重启")
            return
        proc = job._proc
        if proc and proc.poll() is None:
            self._log_note(job, "[自动装依赖] 任务仍在运行，跳过自动重启")
            return
        self._log_note(job, f"[自动装依赖] {pkg} 安装成功，自动重启任务")
        try:
            self.start(jid, fresh=False)
        except Exception as e:
            self._log_note(job, f"[自动装依赖] 自动重启失败: {e}")

    # ---------- 缺失依赖自动安装（PHP / composer） ----------
    def _auto_deps_probe(self, job: Job, line: str) -> None:
        """按日志行匹配 Python 缺模块 / PHP 缺 autoload / PHP 缺类。"""
        m = _MISSING_RE.search(line)
        if m:
            self._auto_install(job, m.group(1))
            return
        m = _PHP_AUTOLOAD_RE.search(line)
        if m and any(w in line.lower() for w in _PHP_FAIL_WORDS):
            self._php_autoload_missing(job)
            return
        m = _PHP_CLASS_RE.search(line)
        if m:
            self._php_class_missing(job, m.group(1))

    def _php_autoload_missing(self, job: Job) -> None:
        if job._deps_done:
            return
        job._deps_done = True
        if not AUTO_COMPOSER:
            return
        if job._deps_attempts >= AUTO_DEPS_MAX:
            self._log_note(
                job,
                f"[自动装依赖] 已达最大重试次数 {AUTO_DEPS_MAX}，跳过 composer install；"
                f"请到「包管理」页手动 composer install 后启动")
            return
        job._deps_attempts += 1
        self._log_note(
            job,
            f"[自动装依赖] 缺少 vendor/autoload.php → composer install"
            f"（第 {job._deps_attempts}/{AUTO_DEPS_MAX} 次，可能需要几十秒）")
        threading.Thread(
            target=self._composer_worker,
            args=(job.id,
                  ["install", "--no-interaction", "--no-ansi",
                   "--prefer-dist", "--no-progress"],
                  "composer install"),
            daemon=True).start()

    def _php_class_missing(self, job: Job, cls: str) -> None:
        if job._deps_done:
            return
        job._deps_done = True
        if not AUTO_COMPOSER:
            self._log_note(job, f"[自动装依赖] 缺少类 {cls}（自动安装已关闭）")
            return
        cdir = Path(job._composer_dir or job.workdir)
        if not (cdir / "composer.json").is_file():
            self._log_note(
                job,
                f"[自动装依赖] 缺少类 {cls}，工作目录没有 composer.json；"
                f"请先 composer init，再 composer require <包名> 后重启")
            return
        pkg = php_class_to_pkg(cls)
        if not pkg:
            self._log_note(
                job,
                f"[自动装依赖] 缺少类 {cls}，无法自动确定包名；"
                f"请到「包管理」页执行 composer require <vendor>/<包名> 后重启")
            return
        if job._deps_attempts >= AUTO_DEPS_MAX:
            self._log_note(
                job,
                f"[自动装依赖] 已达最大重试次数 {AUTO_DEPS_MAX}，跳过 {pkg}；"
                f"请到「包管理」页手动 composer require {pkg} 再启动")
            return
        job._deps_attempts += 1
        self._log_note(
            job,
            f"[自动装依赖] 缺少类 {cls} → composer require {pkg}"
            f"（第 {job._deps_attempts}/{AUTO_DEPS_MAX} 次，可能需要几十秒）")
        threading.Thread(
            target=self._composer_worker,
            args=(job.id,
                  ["require", pkg, "--no-interaction", "--no-ansi",
                   "--prefer-dist", "--no-progress"],
                  f"composer require {pkg}"),
            daemon=True).start()

    def _composer_worker(self, jid: str, args: List[str], desc: str) -> None:
        try:
            job = self.get(jid)
        except KeyError:
            return
        code, out = self.run_composer(job._composer_dir or job.workdir, args, timeout=900)
        tail = "\n".join((out or "").strip().splitlines()[-20:])
        if tail:
            self._log_note(job, tail)
        if code != 0:
            self._log_note(job, f"[自动装依赖] {desc} 失败 (exit {code})，"
                                f"请到「包管理」页手动处理后重启")
            try:
                job = self.get(jid)
                if job.status not in ("running", "starting"):
                    job.status = "error"
                    job.error = f"{desc} 失败，缺少依赖"
            except KeyError:
                pass
            return
        try:
            job = self.get(jid)
        except KeyError:
            return
        if job.status in ("stopping", "stopped"):
            self._log_note(job, "[自动装依赖] 任务已被停止，跳过自动重启")
            return
        proc = job._proc
        if proc and proc.poll() is None:
            self._log_note(job, "[自动装依赖] 任务仍在运行，跳过自动重启")
            return
        self._log_note(job, f"[自动装依赖] {desc} 完成，自动重启任务")
        try:
            self.start(jid, fresh=False)
        except Exception as e:
            self._log_note(job, f"[自动装依赖] 自动重启失败: {e}")

    # ---------- 停止 ----------
    def stop(self, jid: str, timeout: float = 8.0) -> Job:
        job = self.get(jid)
        proc = job._proc
        if not proc or proc.poll() is not None:
            job.status = "stopped" if job.status != "error" else "error"
            self._stop_proxy(job)
            self._release_port(job.port)
            _pid_path(job.id).unlink(missing_ok=True)
            return job

        job.status = "stopping"
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        job.status = "stopped"
        self._stop_proxy(job)
        self._release_port(job.port)
        _pid_path(job.id).unlink(missing_ok=True)
        return job

    def remove(self, jid: str) -> None:
        job = self.get(jid)
        try:
            if job.status == "running":
                self.stop(jid)
        except Exception:
            pass
        with self.lock:
            self.jobs.pop(jid, None)
        self.persist()
        _pid_path(jid).unlink(missing_ok=True)
        try:
            _log_file(jid).unlink(missing_ok=True)
        except OSError:
            pass
        if job.source == "upload":
            shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)

    def restart(self, jid: str) -> Job:
        job = self.get(jid)
        # 状态可能滞后（exited 但进程还活着、starting 中…），以进程存活为准
        if job.status in ("running", "starting") or (job._proc and job._proc.poll() is None):
            self.stop(jid)
        return self.start(jid)

    def autostart(self, delay: float = 0.8) -> List[str]:
        """容器/面板启动时，拉起所有勾选了「自启动」的任务。"""
        if delay:
            time.sleep(delay)
        with self.lock:
            targets = sorted((j for j in self.jobs.values() if j.auto_start),
                             key=lambda x: x.created_at)
            targets = [j.id for j in targets]
        started: List[str] = []
        for jid in targets:
            try:
                self.start(jid)
                started.append(jid)
            except Exception as e:
                try:
                    job = self.get(jid)
                    job.error = str(e)
                    msg = f"[自启动失败] {e}\n"
                    job._tail.append(msg)
                    path = _log_file(jid)
                    _ensure_day_marker(path)
                    with path.open("a", encoding="utf-8") as fh:
                        fh.write(msg)
                except Exception:
                    pass
        return started

    def stop_all(self) -> int:
        """面板退出时优雅停止所有运行中的任务。"""
        with self.lock:
            running = [j.id for j in self.jobs.values() if j.status == "running"]
        for jid in running:
            try:
                self.stop(jid, timeout=5.0)
            except Exception:
                pass
        return len(running)

    # ---------- 后台巡检 ----------
    # ---------- 定时运行 ----------
    def _schedule_tick(self, job: Job) -> None:
        """每秒检查一次 cron 表达式，命中且未在运行则自动启动（每分钟最多触发一次）。"""
        if not job.schedule:
            return
        now = time.localtime()
        key = time.strftime("%Y-%m-%d %H:%M", now)
        if job._last_fire == key:
            return
        expr = job.schedule.strip()
        try:
            if not job._cron_cache or job._cron_cache[0] != expr:
                parsed = parse_cron(expr)
                job._cron_cache = (expr, parsed[0], parsed[1])
        except ValueError as e:
            job.schedule = ""
            job._cron_cache = None
            self.persist()
            self._log_note(job, f"[定时] 表达式无效，已自动停用：{e}")
            return
        if not cron_match(job._cron_cache, now):
            return
        job._last_fire = key
        if job.status in ("running", "starting", "stopping"):
            self._log_note(job, f"[定时] {expr} 命中，任务正在运行，跳过本轮")
            return
        self._log_note(job, f"[定时] {expr} 命中，自动启动")
        try:
            self.start(job.id)
        except Exception as e:
            self._log_note(job, f"[定时] 启动失败: {e}")

    def _watch(self) -> None:
        while True:
            time.sleep(1.0)
            with self.lock:
                jobs = list(self.jobs.values())
            for job in jobs:
                try:
                    self._schedule_tick(job)
                except Exception:
                    pass
                proc = job._proc
                if not proc or job.status in ("stopped", "created", "error", "exited"):
                    continue
                code = proc.poll()
                if code is None:
                    continue
                job.exit_code = code
                job.status = "exited" if code == 0 else "error"
                if code != 0 and not job.error:
                    job.error = f"进程退出码 {code}"
                self._stop_proxy(job)
                self._release_port(job.port)
                _pid_path(job.id).unlink(missing_ok=True)
                msg = f"[进程退出 code={code}]\n"
                self._push(job, msg)
                path = _log_file(job.id)
                _ensure_day_marker(path)
                try:
                    with path.open("a", encoding="utf-8") as fh:
                        fh.write(msg)
                except OSError:
                    pass

    # ---------- 日志清理 ----------
    def prune_logs(self) -> int:
        """裁掉单个日志文件里 LOG_KEEP_DAYS 天之前的内容。

        分段依据是行首 `[YYYY-MM-DD HH:MM:SS]` 时间标记（每次启动的头部行 + 跨天标记行）；
        第一个标记是新的就整个保留（含它之前无时间的前缀），否则从第一个新标记处截断。
        完全没有时间标记的历史文件按 mtime 处理：超过 LOG_KEEP_DAYS 天没写过就整个删除。
        正在运行的任务的文件只裁剪、不整删（避免它的写句柄指向被删的 inode）。
        返回被处理（裁剪或删除）的文件数。
        """
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - LOG_KEEP_DAYS * 86400))
        now = time.time()
        touched = 0
        try:
            files = [p for p in LOGS_DIR.iterdir() if p.is_file() and p.suffix == ".log"]
        except OSError:
            return 0
        if not files:
            return 0
        active = {j.id for j in list(self.jobs.values())
                  if j._proc is not None and j._proc.poll() is None}
        for p in files:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if not text:
                continue
            lines = text.splitlines(keepends=True)
            marks = [(i, m.group(1)) for i, ln in enumerate(lines)
                     if (m := _TS_RE.match(ln))]
            if not marks:
                if p.stem not in active and mtime <= now - LOG_KEEP_DAYS * 86400:
                    try:
                        p.unlink(missing_ok=True)
                        touched += 1
                    except OSError:
                        pass
                continue
            if marks[0][1] > cutoff:
                keep_from = 0           # 首个标记就是新的：连同前面无时间的行一起保留
            else:
                keep_from = next((i for i, d in marks if d > cutoff), None)
                if keep_from is None:   # 所有段落都过期
                    if p.stem in active:
                        continue        # 运行中：只等下次写入补上新标记，不整删
                    try:
                        p.unlink(missing_ok=True)
                        touched += 1
                    except OSError:
                        pass
                    continue
            if keep_from == 0:
                continue
            new = "".join(lines[keep_from:])
            if len(new) >= len(text):
                continue
            try:
                with p.open("w", encoding="utf-8") as fh:
                    fh.write(new)
                touched += 1
            except OSError:
                pass
        return touched

    def _log_sweeper(self) -> None:
        """每天 04:00（容器时区）清理一次过期日志。"""
        while True:
            try:
                now = time.time()
                lt = time.localtime(now)
                target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 4, 0, 0, 0, 0, -1))
                if target <= now:
                    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1,
                                          4, 0, 0, 0, 0, -1))
                time.sleep(max(1.0, target - now))
                n = self.prune_logs()
                if n:
                    print(f"[日志清理] 处理 {n} 个日志文件（保留最近 {LOG_KEEP_DAYS} 天）")
            except Exception:                          # noqa: BLE001
                time.sleep(3600)

    # ---------- 日志 ----------
    def logs(self, jid: str, tail: int = 500, offset: int = 0) -> dict:
        job = self.get(jid)
        with self.lock:
            lines = list(job._tail)
        if offset:
            return {"lines": lines[offset:], "next": len(lines), "status": job.status}
        return {"lines": lines[-tail:], "next": len(lines), "status": job.status}

    def clear_logs(self, jid: str) -> None:
        job = self.get(jid)
        job._tail.clear()
        try:
            _log_file(jid).write_text("", encoding="utf-8")
        except OSError:
            pass


manager = JobManager()
