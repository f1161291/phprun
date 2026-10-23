# Phprun — 带控制面板的 PHP / Python / Node 运行容器

从 [pyrun](../pyrun/) 复制改造的**三语言版**：一个容器同时提供：

1. **PHP 8.2 + Composer + 常用扩展**（mysql/gd/zip/mbstring/xml/curl/bcmath/intl/redis/sqlite3/opcache）
2. **Python 3.12 + Node 18** 照常可用（`.py` / `.js` 任务与 pyrun 行为一致）
3. **Web 控制面板**：上传文件 / 目录 / zip 运行，或直接运行挂载目录里的脚本
4. **多任务并行**：每个任务独占一个端口（默认 8600-8699，可指定固定端口、可反代到脚本自身监听的端口），另有 `/proxy/{任务}/` 统一反代
5. **依赖自动安装**：Python 缺包自动 `pip install`；PHP 缺 `vendor/autoload.php` 自动 `composer install`、缺类自动 `composer require`，装好自动重启一次，日志全程可见
6. **定时运行**：每个任务可填 5 段 cron 表达式，到点自动启动

> 与 pyrun 的唯一本质区别：多了 PHP（入口识别、运行模式、composer）；端口改成 **8100 / 8600-8699**，两个容器可同时跑。其余功能（端口池、反代、cron、自启动、SSE 日志、令牌）完全一致。

## 快速开始

```bash
docker compose up -d --build    # 首次或 Dockerfile 有改动时要 --build
# 打开 http://localhost:8100
# 仅改了 app/ 下的代码：docker compose restart 即可（./app 已挂载）
```

- `./scripts` 挂载到容器 `/mount`，内置 `demo/web`（PHP Web）、`demo/cli`（PHP CLI）、`demo/composer-demo`（composer 依赖）、`demo/app.py`（Python）
- `./data` 持久化上传文件、任务工作区、日志、任务登记、composer 全局镜像源配置

## 从 GHCR 拉取镜像（GitHub Actions 自动构建）

推送代码到 GitHub 后，`.github/workflows/docker-image.yml` 会自动构建镜像并推送到 GitHub Container Registry，无需本机安装 Docker：

```bash
# 替换 <owner> 为你的 GitHub 用户名
docker pull ghcr.io/<owner>/phprun:latest
```

镜像同时提供 **amd64（x86_64）与 arm64** 两个架构，Docker 会根据宿主机 CPU 架构自动拉取对应版本。

```bash
# 运行（挂载目录 / 数据目录换成你的路径）
docker run -d --name phprun \
  -p 8100:8100 -p 8600-8699:8600-8699 \
  -v /path/to/scripts:/mount \
  -v /path/to/data:/data \
  -e PANEL_TOKEN=你的令牌 \
  ghcr.io/<owner>/phprun:latest
```

`docker-compose.yml` 已默认配置为直接使用该镜像（`image: ghcr.io/<owner>/phprun:latest`），`docker compose up -d` 即可；需要本地重新构建时再取消 `build:` 段注释。

## 使用方式

### 1. 网页上传运行
「上传运行」页可：单文件 / 多文件（保留相对路径）/ 整个目录 / zip（自动解压）/ 拖拽。创建后自动启动并打开日志。批处理跑完显示 `exited`，Web 服务保持 `running`。

### 2. 挂载目录运行
「挂载目录」页浏览 `/mount`（宿主机 `./scripts`），点目录或 `.php / .py / .js` 文件右侧的「运行」即可。页面上也可直接**上传文件 / 目录 / zip** 到当前目录（勾选后 zip 自动解压），写入的是宿主机 `./scripts` 对应位置。

### 3. 端口约定
启动任务时注入环境变量：

| 变量 | 含义 |
|---|---|
| `PORT` | 分配给该任务的端口 |
| `HOST` | 监听地址，默认 `0.0.0.0` |
| `JOB_ID` / `JOB_NAME` | 任务标识 |

三种语言读法：PHP `getenv("PORT")` · Python `os.environ["PORT"]` · Node `process.env.PORT`。

**指定端口**：创建或「编辑」时填「指定端口」，该端口被固定占用（重启、面板重启、`auto_start` 都沿用）；留空按启动顺序自动分配。必须落在 `PORT_RANGE_START-END` 内。

**端口占用**：启动/重启指定端口时，面板先清理**本任务自己的残余监听**（上次启动的同一命令行、或本工作目录里 fork/setsid 出来的孤儿进程，以及面板自己泄漏的反代监听），再最多等 `PORT_RELEASE_WAIT`（默认 5 秒）让刚停下的进程放开端口；仍然被占则报错并**指认占用进程**（`pid + 命令行`），照着它手动停止、或换个端口即可。自动分配的端口不会因被外部占用而从池里丢失。

**反代内部端口**：脚本写死了监听端口（如 `8766`）时填「反代内部端口」，面板把对外端口（如 `8600`）TCP 反代到 `127.0.0.1:8766`，脚本一个字不用改；任务停止时反代自动关闭。另有路径反代 `http://主机:8100/proxy/{任务ID}/`。

### 4. 入口文件识别顺序（三语言）
1. 任务里的**自定义命令**（最高优先）
2. 任务里的**入口文件**
3. 顶层惯用名：`index.php / main.php / app.php / run.php / server.php / start.php / cli.php / console.php`，其次 `main.py / app.py / ...`，再次 `main.js / app.js / ...`
4. 顶层唯一的脚本
5. 任意深度的惯用入口名（处理 zip 包一层目录的情况）
6. 全目录唯一的脚本
7. 仍无法确定 → 报错，去任务「编辑」里指定

**语言优先级 PHP > Python > Node**（同一目录既有 `.php` 又有 `.py` 时按 PHP 找）。解释器按扩展名自动选：`.php` → `php`，`.py` → 面板同款 Python，`.js` → `node`，自定义命令 → `/bin/sh -c`。

### 5. PHP 运行模式（auto / web / cli）
每个 PHP 任务可选运行模式（上传页或「编辑」弹窗，仅对 `.php` 生效）：

| 模式 | 启动命令 | 适用 |
|---|---|---|
| `auto`（默认） | 目录里有 `index.php` 或 `public/index.php` → `web`，否则 `cli` | 不想操心时 |
| `web` | `php -S 0.0.0.0:$PORT -t 文档根` | Laravel / ThinkPHP / 纯 index.php 站点 |
| `cli` | `php -d display_errors=1 脚本.php` | 批处理、常驻脚本 |

细节：
- 文档根优先取 `public/`（有 `public/index.php` 时），否则任务根目录 —— 对应 Laravel/ThinkPHP 的 `public/` 约定
- 入口不是 `index.php` 时，会把它作为 `php -S` 的**路由器**传入（所有请求交给它处理）
- `php -S` 默认单进程，镜像里设了 `PHP_CLI_SERVER_WORKERS=4` 并发处理
- 想用框架自带的开发服务器（如 `php think run`、`php artisan serve`），填**自定义命令**即可，优先级最高
- 需要改模式先停任务，改完再启动（运行中改不会热切换）

### 6. composer 依赖
- **启动前自动 install**：任务里有 `composer.json` 且 `vendor/autoload.php` 缺失（或比 `composer.lock` 旧）→ 面板先执行 `composer install` 再启动，日志有 `[composer]` 标记；失败不阻断启动，只记日志
- **项目目录自动定位**：从入口文件所在层向上找 `composer.json`（zip 解出来常带一层 `myapp/` 目录时也能找对），包管理页与自动安装都用同一个目录
- **缺 autoload 自动装**：运行报 `failed to open stream ... vendor/autoload.php` → 自动 `composer install` 并重启一次
- **缺类自动 require**：报 `Class "Xxx\Yyy" not found` → 查下表猜包名，猜中就 `composer require` 并重启一次；猜不出只写提示，去「包管理」页手动装（防装错包）

| 类前缀 | 自动装的包 |
|---|---|
| `GuzzleHttp` | guzzlehttp/guzzle |
| `Monolog` | monolog/monolog |
| `Twig` | twig/twig |
| `Carbon` | nesbot/carbon |
| `PhpOffice\PhpSpreadsheet` | phpoffice/phpspreadsheet |
| `Dompdf` | dompdf/dompdf |
| `Intervention\Image` | intervention/image |
| `Firebase\JWT` | firebase/php-jwt |
| `Doctrine\ORM` / `Doctrine\DBAL` | doctrine/orm · doctrine/dbal |
| `Symfony\Component\*` | symfony/<kebab 组件名> |
| `Illuminate` | laravel/framework |
| `Vendor\Package\...` 且两段同名（如 `Carbon\Carbon`） | vendor/package |

限制与 pip 那套相同：单任务单轮只触发一次、累计最多 3 次（手动启动重置）、`AUTO_INSTALL_DEPS=0` 整体关闭；**包装在该任务的 `vendor/` 里**（不是全局）。

### 7. 包管理页（Python·pip ／ PHP·composer，页内标签切换）
页顶两个标签，右侧常驻「运行环境」（PHP / Composer / Node / Python 版本；PHP 扩展列表见 `GET /api/env` 的 `php.modules`）。

**Python · pip（镜像全局环境）**
- 列出镜像全局 `site-packages` 里已装的 pip 包（`pip list`），可搜索过滤；这些包装一次**所有任务共用**，脚本直接 `import` 即可
- 预装清单见 `requirements.txt`：flask / django / requests / numpy / pandas / openpyxl / pyyaml / jieba / beautifulsoup4 / lxml / selenium / parsel / loguru / click / pymysql / redis / pika 等
- 输入 `pip install requests==2.31.0`（或 `uninstall`）在页面里直接装/卸，日志框回显 pip 输出
- 只接受包名与版本约束，**拒绝** `-r`、`--index-url`、逗号连写等（不走 shell，无注入面）

**PHP · composer（按任务 `vendor/`）**
- 顶部选**作用于哪个任务**（默认最近创建的任务），下方显示该任务 `workdir`、`composer.json` 声明但未安装的依赖
- `composer require monolog/monolog ^3.0` / `guzzlehttp/guzzle:^7.0 --dev` 都行（支持 `^` `~` `*`、冒号约束、`--dev`），装进该任务 `vendor/`；`remove` 同理
- 表格列出已装包（名称 / 版本 / 说明，来自 `composer show --format=json`）
- PHP **扩展**不在这里装：镜像已带 mysql/gd/zip/mbstring/xml/curl/bcmath/intl/redis/sqlite3，要加别的需改 Dockerfile 重建

### 8. 定时运行（cron）
任务「编辑」或「上传运行」里填 **5 段 cron**（分 时 日 月 周），支持 `*`、`a`、`a-b`、`/步长`、逗号列表。面板每秒检查、每分钟最多触发一次；命中时任务在运行则跳过；表达式非法自动停用并写日志；状态存 `data/registry.json`，重启仍有效。

### 9. 缺失依赖自动安装（Python，同 pyrun）
日志出现 `ModuleNotFoundError: No module named 'xxx'` → 查 import 名→PyPI 包名映射 → `pip install` → 自动重启一次，日志有 `[自动装依赖]` 标记。走 `PIP_INDEX_URL`（默认阿里源）。

**这两种情况不会去装**（既不浪费 3 次额度，也不再提示「去包管理页手动装」）：
- **本地模块**：工作目录或入口目录里已有 `xxx.py` / `xxx/__init__.py` → 属路径问题，日志提示把入口与它放同一目录或检查 `sys.path`
- **非法包名**：如下划线开头的 `_credentials`（pip 要求以字母/数字开头）→ 日志提示是本地模块名或 import 拼写错误

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` `/api/config` | 健康检查 / 端口范围 |
| GET | `/api/env` | PHP/Composer/Node/Python 版本与 PHP 扩展列表 |
| POST | `/api/upload` | multipart 上传（`files` 多值；`port`、`proxy_to`、`schedule`、`run_mode`，`mode`=single/dir/zip） |
| GET | `/api/fs?root=mount\|upload&path=` | 目录列表 |
| GET | `/api/fs/download` | 下载 |
| POST | `/api/fs/upload` | 上传到挂载目录/上传目录（`root`=mount\|upload、`path`=目标子目录、`unzip`=1 自动解压 zip、`files` 多值保留相对路径） |
| POST | `/api/mounted/run` | 运行挂载路径，body: `{path,name,entry,cmd,port,proxy_to,schedule,mode}` |
| GET | `/api/jobs` | 任务列表（含 `mode`、`port_pref`、`proxy_to`、`schedule`） |
| POST | `/api/jobs/{id}/start\|stop\|restart` | 启停重启 |
| PATCH | `/api/jobs/{id}` | 改 `name/entry/cmd`（需先停止）；`auto_start`、`port`、`proxy_to`、`schedule`、`mode` 随时可改 |
| DELETE | `/api/jobs/{id}` | 删除（上传类连带删文件） |
| GET | `/api/jobs/{id}/logs?tail=&offset=` | 日志 |
| GET | `/api/jobs/{id}/logs/stream` | SSE 实时日志 |
| GET | `/api/packages?jid=` | 指定任务的 composer 包列表 + 缺失依赖 |
| POST | `/api/packages/install` `/remove` | `{spec, jid}` → `composer require/remove` |
| GET | `/api/pip-packages` | 镜像全局已装 pip 包（`pip list`） |
| POST | `/api/pip/install` `/remove` | `{spec}` → `pip install` / `pip uninstall -y`（全局） |
| ANY | `/proxy/{id}/...` | 反代到该任务端口 |

## 安全

默认**无鉴权**，仅建议内网使用。公网暴露请设置令牌：

```yaml
environment:
  PANEL_TOKEN: "换成你的长随机串"
```

之后所有 `/api/*` 需带 `X-Token` 头（或 `?token=`）。令牌在浏览器里存 `pyrun_token`（与 pyrun **共用一个 key**，两个面板填一次即可）。

## 配置项（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PANEL_PORT` | 8100 | 面板端口 |
| `PORT_RANGE_START` / `END` | 8600 / 8699 | 任务端口池，改了要同步改 compose 端口映射 |
| `PANEL_TOKEN` | 空 | 访问令牌 |
| `MOUNT_DIR` / `DATA_DIR` | `/mount` `/data` | 挂载 / 数据目录 |
| `MAX_UPLOAD_MB` | 512 | 单次上传体积上限 |
| `PIP_INDEX_URL` | 阿里源 | 运行期 `pip install` 索引 |
| `PIP_BREAK_SYSTEM_PACKAGES` | `1` | 镜像带 PEP 668 `EXTERNALLY-MANAGED` 时（如 Alpine/Debian 系统 Python）允许 `pip install` 进全局环境；换成非官方 Python 基础镜像若装包报 `externally-managed-environment`，就是它没生效 |
| `COMPOSER_MIRROR` | 阿里 composer | 运行期 `composer install/require` 的 packagist 镜像（面板启动时写进 `/data/composer/config.json`） |
| `AUTO_INSTALL_DEPS` | `1` | 缺依赖自动安装开关（pip + composer 共用），`0` 关闭 |
| `LOG_KEEP_DAYS` | `5` | 磁盘日志保留天数：单文件 `data/logs/<任务ID>.log` 只保留最近 N 天内容，每天 04:00（容器时区）按行首日期标记裁掉更早的段落，面板启动时也会补裁一次 |
| `PORT_RELEASE_WAIT` | `5` | 指定端口释放等待秒数：启动/重启时等刚停下的残余进程放开端口的最长时间 |
| `PHP_CLI_SERVER_WORKERS` | 4 | `php -S` 并发工作进程数 |

## 与 pyrun 同时跑

```bash
# 两个目录各自 docker compose up -d --build 即可：
#   pyrun  → 面板 :8000，任务 8400-8499
#   phprun → 面板 :8100，任务 8600-8699
```

只想跑一个的话，把 `docker-compose.yml` 的 `ports`、`PANEL_PORT`、`PORT_RANGE_START/END`（以及 Dockerfile 里的默认 ENV 和 `HEALTHCHECK`/`EXPOSE`/`CMD`）改回自己想要的值。

## 构建常见问题

**pip hash 不匹配**（下载被截断/代理改写，与 pyrun 相同）：

```bash
docker compose build --no-cache --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
docker builder prune -f && docker compose build --no-cache
```

**composer 安装很慢 / 卡住**：确认 `COMPOSER_MIRROR` 是国内镜像（compose 已默认阿里源），或临时 `docker compose run --rm phprun composer diagnose`。

**`.php` 任务报「环境里没有 PHP」**：镜像没装 PHP，`docker compose up -d --build` 重建。

## 目录结构

```
phprun/
├── Dockerfile              # python:3.12-slim + php/composer/node + 面板依赖
├── docker-compose.yml      # 8100 面板 / 8600-8699 任务
├── requirements.txt        # 面板自身（fastapi/uvicorn/httpx…）
├── app/
│   ├── main.py             # 入口：鉴权中间件 + 路由 + 静态页
│   ├── api.py              # REST API（上传/任务/日志/composer 包管理/环境信息）
│   ├── manager.py          # 端口池 / 子进程生命周期 / PHP 模式与 composer / 日志
│   ├── proxy.py            # /proxy 反代
│   └── static/index.html   # 前端单文件
├── scripts/                # 挂载点（宿主机）
│   └── demo/               # web/index.php · cli/cli.php · composer-demo/ · app.py
└── data/                   # 上传、任务工作区、日志、registry、composer 全局配置
```

## 进程生命周期

- 面板**退出**时自动 SIGTERM 停掉所有任务（按**进程组**发信号，脚本 fork 的子进程一并清理）
- 面板**启动**时扫描 `data/pids/*.pid`，回收上次遗留的任务进程（校验 cmdline/cwd，避免误杀）
- 启动指定端口任务前，回收**本任务**上次遗留、仍占着该端口的监听（孤儿进程 / 泄漏的反代 fd），见「端口约定 · 端口占用」
- 磁盘日志是**单文件** `data/logs/<任务ID>.log`（一直追加，不轮转）：每次启动的头部行、以及跨天继续输出时都会写一行 `[YYYY-MM-DD HH:MM:SS]` 日期标记；每天 04:00 把标记日期早于 `LOG_KEEP_DAYS`（默认 5 天）的段落从文件里裁掉，面板启动时也补裁一次（运行中的任务只裁剪不整删）。「清日志」=清空该文件，删除任务时日志一并删除；内存里只留最近 3000 行给面板/SSE
- 任务卡片勾选**「随容器自启动」**→ 状态写进 `data/registry.json`，容器重启后按创建顺序拉起；某任务失败不影响其他任务
- 每个任务独立会话运行（`start_new_session`），互相之间**不隔离**（共享文件系统与网络，仅端口分离）

## 已知限制

- 单容器 + 子进程模型：任务之间无隔离，共享文件系统与网络
- `php -S` 是开发级内置服务器（`PHP_CLI_SERVER_WORKERS=4` 并发），生产级请自行加 nginx/apache 反代
- composer 包按任务安装：A 任务 `vendor/` 里的包 B 任务用不了，这是 Composer 的常态，不是缺陷
- 缺类自动 require 只覆盖常见包名表，其余给提示不自动装（避免装错包）
- WebSocket 反代只做基础帧转发；只在**面板进程存活**时 cron 定时才生效
