"""REST API：上传 / 文件浏览 / 任务管理 / 日志流 / 包管理。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import List

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from .manager import (DATA_DIR, JOBS_DIR, MOUNT_DIR, PANEL_PORT, PORT_END,
                      PORT_START, Job, manager, validate_schedule)

router = APIRouter(prefix="/api")

MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "512")) * 1024 * 1024


def _safe_relpath(p: str) -> str:
    p = (p or "").replace("\\", "/").lstrip("/")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise HTTPException(400, f"非法路径: {p}")
    return "/".join(parts)


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


# ---------------- 健康 / 配置 ----------------
@router.get("/health")
def health():
    return {"ok": True, "time": time.time()}


@router.get("/config")
def config():
    return {
        "port_range": [PORT_START, PORT_END],
        "panel_port": PANEL_PORT,
        "mount_dir": str(MOUNT_DIR),
        "max_upload_mb": MAX_UPLOAD // 1024 / 1024,
    }


# ---------------- 文件浏览 ----------------
@router.get("/fs")
def list_fs(root: str = Query("mount"), path: str = ""):
    base = MOUNT_DIR if root == "mount" else JOBS_DIR
    rel = _safe_relpath(path)
    target = (base / rel) if rel else base
    if not _under(target, base):
        raise HTTPException(400, "路径越界")
    if not target.exists():
        raise HTTPException(404, "目录不存在")
    if not target.is_dir():
        return {"path": rel, "entries": []}

    entries = []
    for child in sorted(target.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
        if child.name.startswith("."):
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        entries.append({
            "name": child.name,
            "path": f"{rel}/{child.name}".lstrip("/") if rel else child.name,
            "type": "dir" if child.is_dir() else "file",
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    return {"path": rel, "entries": entries}


@router.get("/fs/download")
def download(root: str = "upload", path: str = ""):
    base = JOBS_DIR if root == "upload" else MOUNT_DIR
    rel = _safe_relpath(path)
    target = base / rel
    if not rel or not _under(target, base) or not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target, filename=target.name)


# ---------------- 上传 ----------------
@router.post("/upload")
async def upload(
    name: str = Form(""),
    entry: str = Form(""),
    cmd: str = Form(""),
    port: str = Form(""),                     # 指定端口，空=自动
    proxy_to: str = Form(""),                 # 反代内部端口，空=不反代
    schedule: str = Form(""),                 # cron 定时，空=不启用
    run_mode: str = Form("auto"),             # PHP 运行模式 auto|cli|web（run_mode 避开下面的 mode）
    mode: str = Form("single"),               # single | dir | zip
    files: List[UploadFile] = File(...),
):
    if not files:
        raise HTTPException(400, "没有收到文件")
    total = 0
    for f in files:
        total += len(await _read(f))
        if total > MAX_UPLOAD:
            raise HTTPException(413, f"超过大小限制 {MAX_UPLOAD // 1024 // 1024}MB")
    try:
        sched = validate_schedule(schedule)
    except ValueError as e:
        raise HTTPException(400, str(e))

    job = manager.create(name="", source="upload", workdir=str(JOBS_DIR / "pending"),
                         entry=entry, cmd=cmd, port_pref=_port_pref(port),
                         proxy_to=_proxy_to(proxy_to),
                         schedule=sched, mode=_run_mode(run_mode))
    workdir = JOBS_DIR / job.id
    workdir.mkdir(parents=True, exist_ok=True)
    job.workdir = str(workdir)
    job.name = name.strip() or _first_stem(files) or job.id

    written = 0
    for f in files:
        rel = _safe_relpath(f.filename or "")
        if not rel:
            continue
        dest = workdir / rel
        if not _under(dest, workdir):
            raise HTTPException(400, f"非法路径: {rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)
        written += 1
        f.file.close()

    if mode in ("zip", "dir") or any(_safe_relpath(f.filename or "").endswith(".zip") for f in files):
        try:
            _extract_zip(workdir)
        except zipfile.BadZipFile:
            raise HTTPException(400, "zip 文件损坏")

    if not (any(workdir.rglob("*.php")) or any(workdir.rglob("*.py"))
            or any(workdir.rglob("*.js"))) and not cmd:
        job.error = "上传内容中没有 .php / .py / .js 文件"

    _save_meta(job)
    return {"job": job.public(), "written": written}


async def _read(f: UploadFile) -> bytes:
    data = await f.read()
    f.file.seek(0)
    return data


def _first_stem(files) -> str:
    for f in files:
        n = (f.filename or "").replace("\\", "/").split("/")[0]
        if n:
            return Path(n).stem
    return ""


def _extract_zip(dest: Path) -> None:
    zips = list(dest.rglob("*.zip"))
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            for info in zf.infolist():
                rel = _safe_relpath(info.filename)
                if not rel:
                    continue
                out = dest / rel
                if not _under(out, dest):
                    continue
                if info.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(out, "wb") as fh:
                    shutil.copyfileobj(src, fh)
        z.unlink(missing_ok=True)


def _port_pref(raw) -> int:
    """解析用户填写的指定端口：空/0=自动；否则必须落在面板端口段内。"""
    s = "" if raw is None else str(raw).strip()
    if not s:
        return 0
    try:
        p = int(s)
    except ValueError:
        raise HTTPException(400, f"端口必须是数字：{raw!r}")
    if p and p not in range(PORT_START, PORT_END + 1):
        raise HTTPException(400, f"端口 {p} 不在面板端口段 {PORT_START}-{PORT_END} 内")
    return max(p, 0)


def _proxy_to(raw) -> int:
    """反代目标内部端口：空/0=不反代；否则 1-65535（脚本自身监听的端口）。"""
    s = "" if raw is None else str(raw).strip()
    if not s or s == "0":
        return 0
    try:
        p = int(s)
    except ValueError:
        raise HTTPException(400, f"反代端口必须是数字：{raw!r}")
    if p not in range(1, 65536):
        raise HTTPException(400, "反代端口范围 1-65535")
    return p


def _sched(raw) -> str:
    """校验 cron 定时表达式，空=不启用。"""
    try:
        return validate_schedule("" if raw is None else str(raw))
    except ValueError as e:
        raise HTTPException(400, str(e))


def _run_mode(raw) -> str:
    """PHP 运行模式：auto | cli | web。"""
    s = ("" if raw is None else str(raw)).strip().lower() or "auto"
    if s not in ("auto", "cli", "web"):
        raise HTTPException(400, f"运行模式只能是 auto / cli / web：{raw!r}")
    return s


def _save_meta(job: Job) -> None:
    if job.source == "upload" and job.workdir.startswith(str(JOBS_DIR)):
        meta = {k: v for k, v in job.public().items()}
        try:
            (Path(job.workdir) / ".job.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    manager.persist()


# ---------------- 挂载目录运行 ----------------
@router.post("/mounted/run")
def run_mounted(payload: dict):
    rel = _safe_relpath(payload.get("path", ""))
    if not rel:
        raise HTTPException(400, "缺少 path")
    target = MOUNT_DIR / rel
    if not _under(target, MOUNT_DIR) or not target.exists():
        raise HTTPException(404, "路径不存在")
    workdir = target if target.is_dir() else target.parent
    job = manager.create(
        name=payload.get("name") or target.stem,
        source="mount",
        workdir=str(workdir),
        entry=_safe_relpath(payload.get("entry", "")) if payload.get("entry") else (target.name if target.is_file() else ""),
        cmd=payload.get("cmd", ""),
        port_pref=_port_pref(payload.get("port")),
        proxy_to=_proxy_to(payload.get("proxy_to")),
        schedule=_sched(payload.get("schedule")),
        mode=_run_mode(payload.get("mode")),
    )
    _save_meta(job)
    return {"job": job.public()}


# ---------------- 任务 ----------------
@router.get("/jobs")
def jobs():
    return {"jobs": manager.list()}


@router.post("/jobs/{jid}/start")
def start_job(jid: str):
    try:
        return {"job": manager.start(jid).public()}
    except (KeyError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@router.post("/jobs/{jid}/stop")
def stop_job(jid: str):
    try:
        return {"job": manager.stop(jid).public()}
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.post("/jobs/{jid}/restart")
def restart_job(jid: str):
    try:
        return {"job": manager.restart(jid).public()}
    except (KeyError, RuntimeError) as e:
        raise HTTPException(400, str(e))


@router.delete("/jobs/{jid}")
def delete_job(jid: str):
    try:
        manager.remove(jid)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@router.patch("/jobs/{jid}")
def update_job(jid: str, payload: dict):
    job = manager.get(jid)
    if "auto_start" in payload:
        job.auto_start = bool(payload.get("auto_start"))
    if "port" in payload:
        job.port_pref = _port_pref(payload.get("port"))
    if "proxy_to" in payload:
        job.proxy_to = _proxy_to(payload.get("proxy_to"))
    if "schedule" in payload:
        job.schedule = _sched(payload.get("schedule"))
        job._cron_cache = None
    if "mode" in payload:
        job.mode = _run_mode(payload.get("mode"))
    if any(k in payload for k in ("entry", "cmd", "name")):
        if job.status == "running":
            raise HTTPException(400, "任务运行中，请先停止再修改")
        if "entry" in payload:
            job.entry = _safe_relpath(payload.get("entry") or "")
        if "cmd" in payload:
            job.cmd = payload.get("cmd") or ""
        if payload.get("name"):
            job.name = payload["name"]
    _save_meta(job)
    return {"job": job.public()}


# ---------------- 日志 ----------------
@router.get("/jobs/{jid}/logs")
def job_logs(jid: str, tail: int = 500, offset: int = 0):
    try:
        return manager.logs(jid, tail=tail, offset=offset)
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.delete("/jobs/{jid}/logs")
def clear_logs(jid: str):
    try:
        manager.clear_logs(jid)
        return {"ok": True}
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.get("/jobs/{jid}/logs/stream")
async def stream_logs(jid: str, request: Request, token: str = ""):
    try:
        manager.get(jid)
    except KeyError as e:
        raise HTTPException(404, str(e))

    async def gen():
        cursor = 0
        yield f"data: {json.dumps({'init': True})}\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                data = manager.logs(jid, offset=cursor)
            except KeyError:
                break
            lines = data["lines"]
            if lines:
                cursor = data["next"]
                yield f"data: {json.dumps({'lines': lines, 'status': data['status']}, ensure_ascii=False)}\n\n"
            elif data["status"] not in ("running", "starting", "stopping"):
                yield f"data: {json.dumps({'status': data['status']})}\n\n"
            await asyncio.sleep(0.6)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------- 包管理（composer，按任务的 workdir） ----------------
_SPEC_TOKEN = re.compile(r"^[A-Za-z0-9^~*][A-Za-z0-9._\-/:^~*]*$")


def _pkg_tokens(spec: str) -> List[str]:
    """把 'monolog/monolog ^2.0 --dev' 拆成参数列表并校验（不走 shell，天然免疫注入）。"""
    tokens = spec.split()
    if not tokens:
        raise HTTPException(400, "请输入包名，例如 monolog/monolog 或 guzzlehttp/guzzle:^7.0")
    for t in tokens:
        if t == "--dev":
            continue
        if not _SPEC_TOKEN.match(t):
            raise HTTPException(400, f"包名含有非法字符：{t!r}")
    return tokens


def _json_object(text: str) -> dict:
    """从可能夹带告警/提示行的输出里抠出第一个 JSON 对象。"""
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        return json.loads(text[a:b + 1])
    return json.loads(text)


def _job_project(job) -> Path:
    """任务的 composer 项目目录（含 composer.json 的那层；解析不出入口就退回 workdir）。"""
    try:
        entry = manager.resolve_entry(job)
    except Exception:                                        # noqa: BLE001
        entry = Path(job.workdir)
    return manager.composer_project(job, entry)


def _pkg_workdir(jid: str = "") -> Path:
    """选中的任务项目目录；没选则用最近创建的任务，一个任务都没有就用共享目录。"""
    jid = (jid or "").strip()
    if jid:
        try:
            return _job_project(manager.get(jid))
        except KeyError:
            raise HTTPException(404, "任务不存在")
    jobs = manager.list()
    if jobs:
        try:
            return _job_project(manager.get(jobs[0]["id"]))
        except KeyError:
            return Path(jobs[0]["workdir"])
    shared = DATA_DIR / "composer-project"
    shared.mkdir(parents=True, exist_ok=True)
    return shared


@router.get("/packages")
def packages(jid: str = Query("")):
    workdir = _pkg_workdir(jid)
    cj = workdir / "composer.json"
    installed: List[dict] = []
    err = ""
    code, out = manager.run_composer(
        workdir, ["show", "--no-ansi", "--no-interaction", "--format=json"], timeout=120)
    if code == 0:
        try:
            data = _json_object(out or "")
            installed = [{"name": p.get("name", ""), "version": p.get("version", ""),
                          "description": p.get("description", "")}
                         for p in data.get("installed", [])]
        except Exception:                                    # noqa: BLE001
            installed = []
    else:
        text = (out or "").strip()
        low = text.lower()
        if not cj.is_file() and "composer.json" in low:
            # 还没建 composer 项目不是错误：前端按「无 composer.json」给中文提示
            err = ""
        elif "no dependencies installed" in low:
            err = "已识别 composer.json，但还没执行 composer install（启动任务会自动装）"
        else:
            lines = [ln for ln in text.splitlines() if ln.strip()]
            err = lines[-1] if lines else "composer show 失败"

    requires: dict = {}
    if cj.is_file():
        try:
            doc = json.loads(cj.read_text(encoding="utf-8"))
            requires = {**doc.get("require", {}), **doc.get("require-dev", {})}
        except Exception:                                    # noqa: BLE001
            requires = {}
    have = {p["name"].lower() for p in installed}

    def _platform(key: str) -> bool:                # php / ext-mbstring 等不是 composer 包
        k = key.lower()
        return k in ("php", "hhvm") or k.startswith(
            ("ext-", "lib-", "composer-plugin-api", "composer-runtime-api"))

    missing = sorted(k for k in requires
                     if k.lower() not in have and not _platform(k))

    return {
        "workdir": str(workdir),
        "composer_json": cj.is_file(),
        "packages": installed,
        "requires": requires,
        "missing": missing,
        "error": err,
        "projects": [{"id": j["id"], "name": j["name"], "workdir": j["workdir"]}
                     for j in manager.list()],
    }


@router.post("/packages/install")
async def install_package(request: Request):
    payload = await request.json()
    spec = (payload.get("spec") or "").strip()
    workdir = _pkg_workdir(payload.get("jid") or "")
    tokens = _pkg_tokens(spec)
    code, out = manager.run_composer(
        workdir, ["require"] + tokens + ["--no-interaction", "--no-ansi",
                                         "--no-progress", "--prefer-dist"], timeout=600)
    return {"ok": code == 0, "returncode": code, "log": out[-8000:], "workdir": str(workdir)}


@router.post("/packages/remove")
async def remove_package(request: Request):
    payload = await request.json()
    spec = (payload.get("spec") or "").strip()
    workdir = _pkg_workdir(payload.get("jid") or "")
    tokens = _pkg_tokens(spec)
    code, out = manager.run_composer(
        workdir, ["remove"] + tokens + ["--no-interaction", "--no-ansi",
                                        "--no-progress"], timeout=600)
    return {"ok": code == 0, "returncode": code, "log": out[-8000:], "workdir": str(workdir)}


# ---------------- 包管理（pip，镜像全局环境，所有任务共用） ----------------
_PIP_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-+*=<>!~\[\]]*$")


def _pip_tokens(spec: str) -> List[str]:
    """'requests==2.31.0' → 参数列表；拒绝 -r/--index-url 之类选项，不走 shell。"""
    tokens = spec.split()
    if not tokens:
        raise HTTPException(400, "请输入包名，例如 requests 或 requests==2.31.0")
    for t in tokens:
        if t.startswith("-") or "," in t or not _PIP_TOKEN.match(t):
            raise HTTPException(400, f"包名含有非法字符：{t!r}")
    return tokens


def _pip_run(argv: List[str], timeout: int = 900):
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pip"] + argv + [
                "--disable-pip-version-check", "--no-input"],
            capture_output=True, text=True, timeout=timeout, errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"pip {argv[0] if argv else ''} 超时（{timeout // 60} 分钟）"
    except Exception as e:                                   # noqa: BLE001
        return 1, f"pip 执行失败: {e}"


@router.get("/pip-packages")
def pip_packages():
    """pip list —— 镜像全局 site-packages（与任务无关，所有脚本共用）。"""
    code, out = _pip_run(["list", "--format=json"], timeout=60)
    if code != 0:
        raise HTTPException(500, f"pip list 失败: {out.strip()[-400:]}")
    data: list = []
    # stdout 是 JSON 数组，但可能夹带 [notice] 之类的提示行：逐个起点尝试解析
    for i, ch in enumerate(out):
        if ch != "[":
            continue
        try:
            cand = json.loads(out[i:])
        except Exception:                                    # noqa: BLE001
            continue
        if isinstance(cand, list):
            data = cand
            break
    packages = [{"name": str(d.get("name", "")), "version": str(d.get("version", ""))}
                for d in data if isinstance(d, dict)]
    packages.sort(key=lambda x: x["name"].lower())
    return {"packages": packages, "count": len(packages)}


@router.post("/pip/install")
async def pip_install(request: Request):
    payload = await request.json()
    spec = (payload.get("spec") or "").strip()
    tokens = _pip_tokens(spec)
    code, out = _pip_run(["install"] + tokens, timeout=900)
    return {"ok": code == 0, "returncode": code, "log": out[-8000:]}


@router.post("/pip/remove")
async def pip_remove(request: Request):
    payload = await request.json()
    spec = (payload.get("spec") or "").strip()
    tokens = _pip_tokens(spec)
    code, out = _pip_run(["uninstall", "-y"] + tokens, timeout=300)
    return {"ok": code == 0, "returncode": code, "log": out[-8000:]}


# ---------------- 运行环境信息 ----------------
def _run_version(argv) -> str:
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=20, errors="replace")
        return (p.stdout or p.stderr or "").strip().splitlines()[0] if (
            p.stdout or p.stderr) else ""
    except (FileNotFoundError, IndexError, subprocess.TimeoutExpired, OSError):
        return ""
    except Exception as e:                                   # noqa: BLE001
        return str(e)


@router.get("/env")
def env_info():
    php_head = _run_version(["php", "-v"]).splitlines()
    try:
        p = subprocess.run(["php", "-m"], capture_output=True, text=True,
                           timeout=20, errors="replace")
        mods = [ln.strip() for ln in (p.stdout or "").splitlines()
                if ln.strip() and not ln.startswith("[")]
    except Exception:                                        # noqa: BLE001
        mods = []
    return {
        "php": {"version": php_head[0] if php_head else "",
                "sapi": php_head[1] if len(php_head) > 1 else "",
                "modules": mods},
        "composer": _run_version(["composer", "--version"]),
        "node": _run_version(["node", "-v"]),
        "python": _run_version([sys.executable, "-V"]),
        "pip": _run_version([sys.executable, "-m", "pip", "--version"]),
    }
