# WebCoder

让 **ChatGPT 网页版** 和 **Codex** 通过 MCP 读写你电脑上（或另一台设备上）的文件：家里的台式机、公司电脑、云服务器、NAS……
一个中心 Hub 负责 MCP 接入、OAuth 授权和 Web 管理面板；每台设备上运行一个轻量 Agent，主动连接 Hub，只开放你指定的文件夹。

- 支持的客户端：**ChatGPT 网页版**（开发者模式应用，Plus / Pro / Business / Enterprise / Edu）与 **Codex**（CLI / IDE 插件 / App）
- 能力：浏览、读取、搜索、创建、编辑、移动、删除文件，**跨设备复制**；每次修改都有 SHA 校验和自动备份，可一键恢复

## 架构

```text
        ChatGPT 网页版（开发者模式应用）                 Codex
           │                      │                        │
   ① 公网 HTTPS + OAuth     ② Secure MCP Tunnel        令牌或 OAuth
           │              （OpenAI 官方 tunnel-client     │
           │               主动连出，stdio 桥接程序）      │
           ▼                      ▼                        ▼
        ┌──────────────────────────────────────────────────────┐
        │  Hub（FastAPI + SQLite）                               │
        │  /mcp 接口 · /oauth/* 授权服务 · / Web 管理面板          │
        └───────────────────────────▲──────────────────────────┘
                                    │ Agent 主动发起的 WebSocket（设备无需开放端口）
                                    │ HMAC 双向认证 + AES-256-GCM 逐帧加密
                     ┌──────────────┴──────────────┐
                     ▼                             ▼
              Agent · 你的电脑               Agent · 服务器 …
              允许的文件夹                   允许的文件夹
```

- **项目** = 某台设备上的一个文件夹，是授权的基本单位；每个项目可设为只读或读写。
- **Agent 是最后一道防线**：Hub 发来的每个路径都要落在 Agent 本机配置的 `allowed_roots` 之内（含符号链接解析），并拒绝密钥类文件；`.git/` 不能用文件工具读写（其中的配置可能含令牌），git 操作走专门的 git 工具。

## 快速开始

需要 Python 3.10+（已在 3.11 / 3.13 上测试）。

### 1. 启动 Hub

**方式 A：直接运行（本机 / 局域网 / 配合 Secure MCP Tunnel）**

```bash
python -m pip install -e ".[hub]"
python -m webcoder.hub --data-dir data init-admin          # 创建面板管理员
python -m webcoder.hub --data-dir data run                  # 默认只监听 127.0.0.1:8787
```

**方式 B：Docker Compose（服务器，配合 ChatGPT 直连）**

```bash
cp .env.example .env                                      # 设置 WEBCODER_DOMAIN 等
docker compose run --rm hub python -m webcoder.hub init-admin
docker compose -f compose.yml -f deploy/compose.https.yml up -d   # Caddy 自动申请 HTTPS 证书
```

`WEBCODER_PUBLIC_URL`（或 `--public-url`）必须是客户端实际访问的地址，OAuth 元数据由它生成。打开面板 `WEBCODER_PUBLIC_URL/`，用管理员登录。

### 2. 接入设备（Agent）

面板 →「设备」→「添加设备」：填名称、选系统（Windows / macOS / Linux）、**每行填一个允许 AI 访问的文件夹**（只用手机控制或 SSH 时可以先不填），按需勾选「整台设备只读」「开机自动运行」，然后把生成的**一条命令**复制到那台电脑上运行（Windows 用 PowerShell，macOS / Linux 用终端，需要 Python 3.10+，不需要管理员权限）：

```powershell
& ([scriptblock]::Create((irm 'https://你的hub/downloads/install-agent.ps1'))) -Hub 'https://你的hub' -Code 'ABCD-EFGH' -Root 'D:\code','E:\notes'
```

```bash
bash -c "$(curl -fsSL 'https://你的hub/downloads/install-agent.sh' || echo 'exit 1')" install-agent.sh --hub 'https://你的hub' --code 'ABCD-EFGH' --root '/home/me/code'
```

它会在 `~/.webcoder-agent` 建独立的 Python 虚拟环境 → 从 Hub 安装 Agent → 用一次性配对码（15 分钟有效）配对 → 设置允许的文件夹（不存在就创建）→ 注册开机自动运行（Windows 计划任务「WebCoder Agent」、macOS launchd、Linux systemd 用户服务），并确认已连上 Hub。重新运行同一类命令就是升级 / 重新配对。文件夹、只读和手机控制每次都按命令里的设置重新应用：命令里没有的文件夹会被收回。SSH 服务器列表在重新配对同一台设备时保留；配对成另一台设备（例如删掉后重新添加）时，不会继承旧设备的文件夹、手机和服务器权限。

- **文件夹只能在设备上改**（Hub 无权扩大 Agent 的访问范围）：面板里点「修改设置」会生成一条命令，在那台电脑上运行即可，正在运行的 Agent 几秒内自动生效。也可以直接用 `python -m webcoder.agent roots list|add|remove|set`。
- `python -m webcoder.agent read-only on` 可把整台设备切成只读，AI 只能看、不能改：文件和 git 只能读，不运行本机命令行工具；手机只能列出、读屏幕结构、看日志和设备信息（不能操作，截图也不能存进项目）；SSH 只能列出服务器，不执行命令也不传文件。
- 配置文件在 `~/.webcoder-agent/config.json`（含设备密钥，权限 0600），日志 `agent.log`（自动轮转），备份在 `backups/`。

<details>
<summary>手动安装 / 卸载</summary>

手动安装（弹窗里「分步安装」有带配对码的完整命令）：

```bash
python -m pip install "https://你的hub/downloads/webcoder-agent.zip"   # Hub 直接提供 Agent 安装包（含桥接程序）
python -m webcoder.agent pair --hub https://你的hub --code ABCD-EFGH --root "D:\code"
python -m webcoder.agent run
```

卸载：在面板里删除设备即可。那台电脑上的 Agent 会自动停止，清掉设备密钥，并删除它的开机自启项：在线的立即执行，离线的等下次连上 Hub 时执行。这个「已删除」通知带有只有 Hub 和设备知道的密钥证明，旁人伪造不了。程序和修改备份会留在 `~/.webcoder-agent`。删除对话框里有一条彻底卸载的命令（Windows 版如下，macOS / Linux 的在面板里），在那台电脑上运行，会把自启项、进程和整个文件夹（包括修改备份）一起删掉（只停这次安装的 Agent：用 `--config` 另外运行的 Agent 属于别的设备，不受影响）：

```powershell
Stop-ScheduledTask -TaskName 'WebCoder Agent' -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName 'WebCoder Agent' -Confirm:$false -ErrorAction SilentlyContinue; $cfg = Join-Path $env:USERPROFILE '.webcoder-agent\config.json'; Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like '*-m webcoder.agent*' -and ($_.CommandLine.Contains($cfg) -or $_.CommandLine -notlike '*--config*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 1; Remove-Item -Recurse -Force "$env:USERPROFILE\.webcoder-agent" -ErrorAction SilentlyContinue
```

反复添加 / 删除设备、重复运行安装命令，都不会积累服务：自启项名称固定（Windows 计划任务「WebCoder Agent」、macOS `com.webcoder.agent`、Linux `webcoder-agent`），重新安装会先停掉旧 Agent（包括手动启动的）再覆盖；Agent 自带单实例锁，同一个文件夹只会跑一个；安装中途失败会把原来的 Agent 重新启动。
</details>

### 3. 建项目、发令牌

- 面板 →「项目」：选择设备，点「浏览…」挑选文件夹，设置只读/读写。
- 面板 →「访问令牌」：Codex 和 Secure MCP Tunnel 需要令牌；ChatGPT 直连走 OAuth，不需要令牌。

## 多用户

一个 Hub 可以给多人用。每个用户各自添加设备、项目和令牌，**彼此完全隔离**：看不到、也访问不到别人的设备、项目、令牌、OAuth 授权和审计日志；项目名只需在自己名下唯一。

- **邀请**：管理员在面板「用户」页生成邀请链接（1–30 天有效，只能用一次），发给对方；对方打开链接自己设置用户名和密码，注册后即登录。
- **管理**：管理员可以设/取消管理员、停用 / 启用（停用立即退出登录、断开其设备，其令牌和 OAuth 授权暂停，启用后恢复）、生成密码重置链接、删除用户（连同其设备、项目、令牌和日志；设备上的文件不受影响）。系统始终保留至少一名可用的管理员。
- **自己的账号**：点右上角用户名修改密码（其他设备上的登录随之失效）。管理员忘记密码时，在服务器上运行 `python -m webcoder.hub reset-password --username 名字`。
- ChatGPT / Codex 的 OAuth 授权页会显示当前登录的账号，授权只涉及这个账号下的项目；可以点「换个账号」。
- 从单用户版本升级时，数据库在首次启动时自动迁移：已有的设备、项目、令牌和日志都归属第一个管理员。

隔离防的是用户之间，不防 Hub 的运营者：文件内容会经过 Hub 中转，能控制 Hub 服务器的人理论上可以改代码读取经过的数据。只邀请信任这台 Hub 的人。

## 控制手机和模拟器（安卓 adb / 鸿蒙 hdc）

接在电脑上的安卓手机、鸿蒙手机或模拟器，也可以交给 ChatGPT / Codex 操作：读屏幕、点击、滑动、输入、按键、启动或安装应用、看日志、截图、在手机和项目文件夹之间传文件。Agent 在那台电脑上调用 adb（Android SDK platform-tools）或 hdc（DevEco Studio / OpenHarmony SDK），会自动在 PATH 和常见安装位置里查找。

**两道开关，缺一不可：**

1. **设备端**：面板「设备」→ 添加设备或「修改设置」时勾选「允许 AI 控制……手机 / 模拟器」，在那台电脑上运行生成的命令（或直接 `python -m webcoder.agent mobile on`）。默认关闭，Hub 无法远程打开。
2. **授权**：访问令牌勾选「还允许控制手机」；ChatGPT 授权页上「控制手机 / 模拟器」要单独勾选（默认不勾）。ChatGPT 调用手机工具却没有这个权限时，会提示你重新授权。开启手机控制后，在 ChatGPT 的应用设置里点一次「Refresh」（找不到就删掉应用重新添加），它才会看到新工具：ChatGPT 会缓存工具列表，新建会话、重新授权都不会刷新它。

**工具**：`mobile_devices`（列出手机）、`mobile_ui`（把屏幕读成文字：每个元素的文字、id 和可点击的坐标）、`mobile_tap` / `mobile_swipe` / `mobile_type` / `mobile_key`、`mobile_app`（启动、停止、卸载，或从项目文件夹安装 .apk / .hap）、`mobile_logs`（logcat / hilog）、`mobile_info`（已装应用、前台应用、机型与系统版本）、`mobile_screenshot`（截图存进项目文件夹，并直接显示在对话里）、`mobile_push` / `mobile_pull`（手机 ↔ 项目文件夹；拉取绝不覆盖，推送默认不覆盖手机上的已有文件，确需替换时显式传 overwrite）、`mobile_shell`（在手机的 shell 里执行任意命令，即 adb shell / hdc shell，例如 dumpsys、pm、am、getprop）。只有开启了手机控制、或授权里带手机权限时，这些工具才出现在工具列表里。

**限制与安全**：
- 截图通过 MCP Apps 组件显示在 ChatGPT 对话里给你看；图片数据只交给组件，不进模型上下文，所以模型靠 `mobile_ui` 的文字来读屏幕。
- adb 只能输入 ASCII 文字（中文需要装 ADB Keyboard 之类的输入法）；鸿蒙的 `uitest` 支持 UTF-8。
- `mobile_shell` 能在手机上执行任意命令，每条都需要你在 ChatGPT 里确认，并连同命令原文记入审计；停用设备会立即终止它。命令只交给手机的 shell 执行：电脑这边 adb / hdc 按参数列表调用，不经过电脑上的 shell。其余工具是参数固定的动作，传到手机 shell 的参数都经过校验或引号转义；传文件只能在手机和项目文件夹之间，拒绝列表照样生效。
- 手机上的操作在 ChatGPT 里都标为有副作用的操作，执行前会让你确认；全部记入审计日志。App 界面上的文字被当作数据而不是指令，但请仍然留意提示注入。
- 只限定了部分项目的令牌，只能控制托管这些项目的电脑上的手机。
- 推送不覆盖的做法：先传到同目录的临时文件，再在手机上用一条命令检查并改名到位，所以传输期间新出现的同名文件不会被覆盖。adb / hdc 没有原子的“不覆盖”写入，检查和改名之间仍有微秒级的窗口。
- iOS 暂不支持：真机界面自动化需要 WebDriverAgent / idb，且依赖 macOS + Xcode。

## 管理服务器（SSH）

项目里放着服务器的说明文档、部署脚本、配置和运维记录时，ChatGPT / Codex 可以一边读这些文件，一边登录服务器执行命令，不用把项目和服务器拆开。SSH 由托管项目的那台电脑发起：Agent 调用系统自带的 OpenSSH 客户端，用你自己的 `~/.ssh/config`、密钥和 known_hosts。服务器的密码和私钥从不经过 Hub。

**准备**：在那台电脑上，`ssh 主机名 hostname` 要能直接跑通，不弹密码或确认提示。要做到这一点，用密钥或 ssh-agent，并先手动连一次以接受主机指纹。主机名建议用 `~/.ssh/config` 里的别名，例如 `web-1`。Windows 10/11 自带 OpenSSH 客户端。

**两道开关，缺一不可：**

1. **设备端**：面板「设备」→「修改设置」→「允许 AI 通过 SSH 管理的服务器」，每行填一个主机名，然后在那台电脑上运行生成的命令（也可以直接运行 `python -m webcoder.agent ssh-hosts set web-1 db-1`）。这份列表只保存在那台电脑上，Hub 无法远程添加；列表外的主机一律拒绝连接。
2. **授权**：访问令牌要勾选「还允许在服务器上执行命令」。ChatGPT 第一次调用 SSH 工具时会提示你重新授权，授权页上「在服务器上执行命令」需要单独勾选（默认不勾）。

**工具**：
- `ssh_hosts`：列出能连的服务器，以及由哪台电脑去连。
- `ssh_exec`：在服务器上执行一条命令，返回退出码、stdout 和 stderr；超时 1–600 秒，默认 60 秒。
- `ssh_upload`：把项目里的文件传到服务器。服务器上的同名文件没有备份，所以默认不覆盖，确需替换时显式传 overwrite。新文件先以私有权限写好，再设成本地文件的权限（macOS / Linux，受服务器 umask 限制，与 scp 相同；Windows 上用服务器默认值）。机密文件请传 `mode`，例如 `600`。
- `ssh_download`：把服务器上的文件存进项目，需要写权限，并且绝不覆盖项目里的已有文件。

同一个主机名只由一台电脑列出时，会自动用那台电脑；有多台时，用 `computer` 参数指定。

**限制与安全**：
- `ssh_exec` 能执行**任意命令**，权限就是你 ssh 登录的那个账号。所以：
  - ChatGPT 把它标为有副作用的操作，每条命令执行前都会请你确认；请看清命令再点确认。
  - 所有命令，包括失败和超时的，都会连同命令原文记入审计日志。
  - 服务器输出会被当作数据而不是指令，但请留意提示注入：日志、网页等内容可能诱导模型执行别的命令。
- 连接是非交互式的：不分配 TTY，没有端口转发，也不执行 ssh 配置里的 `LocalCommand` / `RemoteCommand`；连接主机用的 `ProxyCommand` / `ProxyJump`（跳板机）照你的 ssh 配置运行。因此 `sudo` 需要免密码配置，`top`、`vim` 这类交互程序用不了；运行时间长的任务用 `nohup` 或 systemd。
- 每条命令的输出只保留最后 20 万个字符（边收边丢，大量输出不会占满内存）；下载的单个文件最大 2 GB，超过就中止，不会先写满磁盘。
- 上传时先传到服务器上同目录的私有临时文件，确认大小完整后才写入目标：连接中途断开不会留下半个文件，也不会损坏原文件。覆盖时原地写入，保留原文件的属主、属组和权限；写入前先在服务器上私有备份原文件，写入失败会自动恢复，恢复也失败时备份留在服务器上，并在报错里给出它的路径。
- 在面板里停用、删除或重新配对设备，会立刻终止这台电脑上正在运行的 ssh / adb / hdc 命令（紧急停止）。已经在服务器上启动、又不再输出的命令，可能在服务器上继续运行到结束（非交互连接断开时，服务器不会给它发挂断信号）。
- 主机名不能以 `-` 开头，调用时放在 `--` 之后，无法借主机名注入 ssh 选项。上传和下载只能在服务器和项目文件夹之间进行，拒绝列表照样生效。
- 只限定了部分项目的令牌，只能用托管这些项目的电脑去连服务器。

## Git

项目文件夹是一个 git 仓库的根目录时，ChatGPT / Codex 可以直接用 git：看改动、看历史、提交、切分支、合并、推送。git 在项目所在的电脑上运行，用那台电脑已保存的 git 凭据（凭据管理器 / SSH 密钥）。项目文件夹要是仓库的顶层，位于更大仓库里的子文件夹不行，否则 git 能碰到项目以外的文件。

**工具**：
- 只读（有读权限即可）：`git_status`、`git_diff`（工作区 / 暂存区 / 两个提交之间）、`git_log`、`git_show`（某个提交，或某个文件在某个提交时的内容）、`git_blame`、`git_branches`（分支、远程、标签、stash）。
- 修改（需要写权限，项目必须是读写的）：`git_add`、`git_commit`（含 amend）、`git_restore`、`git_reset`、`git_branch`（切换 / 新建 / 删除 / 改名）、`git_stash`、`git_sync`（fetch / pull / push，含 force-with-lease）、`git_integrate`（merge / rebase / cherry-pick / revert，以及冲突后的 continue / abort）、`git_tag`、`git_clean`（默认只列出要删的文件）、`git_init`。

**安全**：
- 没有任意 git 命令行：每个工具都是参数固定、由 Agent 拼出的 git 命令。git 有好几种执行任意程序的方式（`-c alias.x=!命令`、`--upload-pack`、`ext::` 远程、`rebase --exec`、`submodule foreach` 等），开放任意参数就等于在你电脑上开了 shell。
- AI 发起的 git 不执行钩子，也不启用 fsmonitor、外部 diff / textconv、分页器和编辑器。husky、pre-commit 这类钩子的配置在工作区里，AI 能改，执行它们等于让 AI 执行任意命令。你自己的提交不受影响。
- git 配置里会运行程序的设置（过滤器、合并驱动、签名程序、凭据助手、`core.sshCommand` 等）如果指向 AI 能改的文件，或者有设置来自 AI 能改的文件（例如 include 了仓库里的 `.gitconfig`），git 工具对这个项目停用：AI 能改这些文件，等于能让 git 运行任意程序。「AI 能改的文件」指这台电脑所有允许的文件夹里的文件（`.git` 里的除外），不只是这个项目的。`git-lfs` 这类本机程序不受影响。
- 只和仓库里已配置的远程通信（按名字，例如 `origin`），不接受 URL；不弹密码框，需要输密码就直接报错。远程地址里的凭据会被遮住。
- 推送前会检查：要推送的提交里，只要有目标远端还没有的机密文件改动（另一个远端有也不算；远端有什么是推送时现问的，不看上次 fetch 的记录，远端回退过的提交照样算没有）（比如从含有未提交 `.env` 的 stash 建出来的分支或标签），就拒绝推送，由你手动推。推送时总是写明推哪个分支到哪里（没指定就是当前分支推到 git 算出的 `@{push}`），不受 `push.default` 影响。冲突后 `git_integrate` 的 continue 和 `git_commit` 一样，暂存区里有机密文件就不提交。
- Agent 自己的数据文件夹（设备密钥、备份）如果在仓库里（比如家目录本身是仓库），git 工具同样不碰：不暂存、不提交、不显示，`git_clean` 也不删。如果它已经被提交进了仓库，git 工具在它被移出版本库之前不做任何修改仓库的操作。
- 拒绝列表里的机密文件（`.env`、私钥等）不进 diff / log / show 的输出，不会被暂存或提交（`git_commit` 的 all 和 paths 也一样，已被跟踪的机密文件改了也不会提交；你自己暂存了机密文件时，提交整个暂存区会被拒绝，按 paths 提交则不带上它），`git_clean` 和 `git_stash` 也不会动它们（含有机密文件改动的 stash 不会被 apply / pop）；`git_restore` 不会还原它们，hard reset、强制切换分支和 `git_integrate` 的 abort / skip 在它们有未提交改动时拒绝执行（改动是合并或变基自己带来的也一样，这时请在电脑上完成或中止它）。
- 会丢掉未提交改动的操作（`git_reset` hard、`git_restore`、`git_branch` 切换时丢弃改动），会先把改动存为 `refs/webcoder/snapshots/…`，可以用 `git_stash` 的 apply 找回。快照里不含机密文件（快照也是提交，可能被拿去建分支、打标签、合并或推送；其余内容 AI 本来就能提交并推送），`git_sync` 的 push 也不接受快照引用名。存不下来就不执行，例如合并有未解决的冲突（先用 `git_integrate` 完成或放弃）或仓库还没有任何提交。快照只存已跟踪的文件，所以切换分支、merge、rebase、cherry-pick、revert、pull、hard reset、从别的提交 restore 会覆盖未跟踪或已忽略的文件（比如本地的 `.env`；被忽略的文件 git 自己会直接覆盖）时，一律拒绝，并列出这些文件；冲突后 continue / skip 前会对还没应用的提交再查一遍。
- 会丢数据或影响远程的操作都标为破坏性，ChatGPT 执行前会让你确认；全部记入审计日志。设为只读的设备只能用只读的 git 工具。

## 本机命令行工具（lark-cli、meegle 等）

很多 skill 靠本机装的命令行工具干活，比如飞书的 `lark-cli`（云文档、多维表格、日历、消息……）和飞书项目的 `meegle`。把它们列进设备的白名单后，ChatGPT 就能照着 skill 调用它们。程序在那台电脑上，以你本人的账号和已登录的状态运行。

**两道开关，缺一不可：**

1. **设备端**：面板「设备」→「修改设置」→「允许 AI 运行的本机命令行工具」，每行一个命令名（如 `lark-cli`、`meegle`），在那台电脑上运行生成的命令（或 `python -m webcoder.agent commands set lark-cli meegle`）。只能在设备上改，Hub 无法远程添加。
2. **授权**：访问令牌勾选「还允许在本机运行命令行工具」；ChatGPT 授权页上「在本机运行命令行工具」要单独勾选（默认不勾）。

**工具**：`cli_programs`（列出每台电脑允许的程序）、`cli_run`（运行其中一个：参数是列表，可附带 stdin，可指定在某个项目文件夹里运行，超时 1–600 秒）。

**安全**：
- 只能运行白名单里的程序。shell、解释器和启动器永远不能加入：cmd、PowerShell、bash、python、node、npx、sudo、docker 等，加入它们等于允许任意命令。常见的能借选项执行别的命令或收发任意文件的工具也不能加入，例如 git、hg、ssh、scp、rsync、tar、sed、awk、curl、wget、包管理器、构建和测试工具（它们会运行项目里的代码，而 AI 能改项目文件）；git、SSH 和手机另有专门的工具。这份清单没法列全所有这类程序：白名单里的程序能做它自己的选项允许的任何事，所以只加你愿意让 AI 以任意参数运行的程序。
- 程序本身不能在 Agent 允许的文件夹里（npm 包装脚本、它用的 node 和入口脚本、`command_paths` 里写的文件都算）：AI 能改那里的文件，改完再运行就等于执行任意代码。把整个用户目录设为允许的文件夹时，全局安装的 npm 命令也在其中，会被拒绝运行（返回 `CLI_WRITABLE`）；请缩小允许的文件夹，或把程序装到别处。
- 参数按列表传给程序，不经过 shell，所以没有管道、重定向和变量展开。Windows 上 npm 装的命令（`xxx.cmd`）不经过 cmd.exe：cmd.exe 会重新解析参数里的 `&`、`|`、`^`、`%`，能被用来夹带别的命令。Agent 直接读出包装脚本里的 JavaScript 入口，用 node 运行。其他 `.bat` / `.cmd` 一律不运行（在 `command_paths` 里写的也一样，也不能直接写 cmd.exe）。
- 这些程序能以你的身份做它们能做的事，比如发飞书消息、改文档，也可能读写项目以外的文件。所以每次运行 ChatGPT 都会先请你确认，完整命令会记入审计日志（写出来超过 19,000 个字符的命令不运行，长内容请走 stdin），停用设备会立即终止正在运行的程序。只把你愿意让 AI 操作的工具加进白名单。
- 设备设为「整台设备只读」时不运行任何程序：程序能改这台电脑上的文件。
- 程序输出按 UTF-8 解码，不是 UTF-8 时按系统代码页（如 GBK）解码。

## Skills

你为 Codex / Claude 写的 skills（带 `SKILL.md` 的文件夹，描述某类任务怎么做）也可以给 ChatGPT 用：`list_skills` 列出每个 skill 的名字和描述，`read_skill` 读 `SKILL.md` 和 skill 文件夹里的其他文件。服务器说明会让 ChatGPT 开始任务前先看有没有合适的 skill，有就照着做。

- **用户目录下的 skills**（`~/.codex/skills`、`~/.claude/skills`、`~/.agents/skills`）需要在设备上打开：面板「设备」→「修改设置」→ 勾选「允许 AI 读取这台电脑上的 skills」，在那台电脑上运行生成的命令（或 `python -m webcoder.agent skills on`）。默认关闭，Hub 无法远程打开。
- **只读，只开放这几个 skills 文件夹**：`~/.codex`、`~/.claude` 里的登录信息（如 `auth.json`）、设置和对话记录都不开放；skill 文件夹里的机密文件照样受拒绝列表保护。换目录可以在配置里设 `skill_dirs`。
- **项目里的 skills**（项目内的 `.codex/skills`、`.claude/skills`、`.agents/skills`）本来就是项目文件，一直列出，用 `read_file` 读。
- 只限定了部分项目的令牌看不到用户目录下的 skills，那些在它的项目之外。
- 局限：skill 里要求在本机运行的程序（比如 `lark-cli`、`meegle`），ChatGPT 通过这些工具跑不了，它会照实说明，不会假装已经执行。

## 用 ChatGPT 网页版修改本地文件

先在 ChatGPT 里打开开发者模式：**设置 → 安全与登录（Security and login）→ 开发者模式（Developer mode）**（部分账号在「设置 → Plugins」页面底部；企业 / 教育工作区需要管理员授权）。然后二选一：

| | ① 公网 HTTPS + OAuth | ② Secure MCP Tunnel |
| --- | --- | --- |
| Hub 放在哪 | 有域名和 HTTPS 的服务器 | 你自己的电脑（只监听 127.0.0.1） |
| 需要准备 | 域名 + 服务器（`deploy/compose.https.yml`） | OpenAI Platform 账号：tunnel_id + Runtime API key |
| ChatGPT 里的认证 | OAuth（在本面板点“允许”） | No Authentication（令牌由桥接程序携带） |
| 适合 | 多台设备、随时随地用 | 只改自己这台电脑上的文件，不想暴露任何服务 |

### ① 公网 HTTPS + OAuth

1. 按「快速开始」方式 B 部署 Hub，`WEBCODER_PUBLIC_URL=https://你的域名`；Agent 装在你的电脑上。
2. 打开 [chatgpt.com/plugins](https://chatgpt.com/plugins) → 点 **+** 创建开发者模式应用，填名称和说明。
3. Connection 选公网地址，填 `https://你的域名/mcp`；Authentication 选 **OAuth**（客户端注册用动态注册，Hub 不支持 CIMD）。
4. 创建后弹出本面板的授权页：登录 → 选择读 / 写权限和可访问的项目 → 允许。ChatGPT 随后会扫描出 16 个文件工具、17 个 git 工具和 2 个 skills 工具（开启了手机控制时再加 13 个手机工具，列出了 SSH 服务器时再加 4 个 SSH 工具，开放了本机命令行工具时再加 2 个）。
5. 新建对话，从工具菜单加入这个应用，直接说“把 demo 项目里的 README 第一行改成……”。写操作 ChatGPT 会先请你确认。

ChatGPT 的回调地址（`https://chatgpt.com/connector_platform_oauth_redirect` 与 `https://chatgpt.com/connector/oauth/<id>`）已内置在白名单中；Hub 在授权响应里返回 `iss`（RFC 9207），并接受 ChatGPT 以域名作为 `resource` 参数。

**公网 HTTPS 从哪来**：有 VPS 就用上面的 Docker + Caddy；没有 VPS 但有托管在 Cloudflare 的域名，也可以把 Hub 跑在自己电脑上，用 Cloudflare Tunnel 发布出去：

```bash
cloudflared tunnel login
cloudflared tunnel create webcoder
cloudflared tunnel route dns webcoder webcoder.你的域名
cloudflared tunnel run --url http://127.0.0.1:8787 webcoder
# Hub 启动时使用：--public-url https://webcoder.你的域名
```

#### 插件包（ChatGPT 桌面端 / Codex）

> **网页版请用上面的「+」方式。** ChatGPT 会把包内声明了 MCP 服务器（`mcp.json` / `.mcp.json`）的上传插件标成「仅限桌面端」，即使服务器是公网 HTTPS 地址，这种插件在网页版里也用不了（见 OpenAI 帮助中心 [Plugins in ChatGPT and Codex](https://help.openai.com/en/articles/20001256-plugins-in-chatgpt-and-codex)）。

Hub 可以按自己的公网地址生成一个现成的插件包：面板「接入客户端」→「ChatGPT 网页版 · 公网 HTTPS」→ **下载插件包**，或者命令行：

```bash
python -m webcoder.hub --public-url https://你的域名 chatgpt-plugin          # 生成 webcoder-<版本>.zip
python -m webcoder.hub chatgpt-plugin --skill-only                         # 只生成技能包
```

压缩包里是一个 `webcoder/` 文件夹：同时带标准的 `plugin.json` + `mcp.json`（Agent Plugins 1.0.0）和 Codex 兼容的 `.codex-plugin/plugin.json` + `.mcp.json`，外加一个教模型“先读取、再按 sha256 修改”的技能 `skills/webcoder-files` 和图标。在 ChatGPT 插件页上传后，同样会弹出本面板的授权页。

- 插件包里的 MCP 地址必须是公网 HTTPS：Secure MCP Tunnel 只适用于开发者模式里直接创建的应用，不能写进插件包。
- ChatGPT 的插件通过应用 ID 关联 MCP 应用。如果上传后提示缺少应用，先按上面第 2–4 步用「+」创建一次，把浏览器地址栏里 `plugin_asdk_app_…` 那段填进面板（或 `--app-id`）重新生成，包里就会带上 `.app.json`。
- 网页版也想要这个技能：把技能包作为技能（Skill）单独上传，配合「+」创建的应用使用。

### ② Secure MCP Tunnel（不需要公网）

[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 由 OpenAI 官方的 `tunnel-client` 从你的电脑主动连出，ChatGPT 的请求经 OpenAI 转发进来，本机不开放任何端口。

1. 在 [platform.openai.com → Settings → Organization → Tunnels](https://platform.openai.com/settings/organization/tunnels) 创建隧道，记下 `tunnel_id`；再创建一个 Runtime API key（需要 Tunnels Read + Use 权限）。
2. 从 [openai/tunnel-client releases](https://github.com/openai/tunnel-client/releases/latest) 下载 tunnel-client（Windows 选 windows-amd64）。
3. 在本机按「快速开始」启动 Hub（方式 A）和 Agent，建好项目；在「访问令牌」创建令牌，并按弹窗提示保存到 `%USERPROFILE%\.webcoder\token.txt`。
4. 运行（路径用 `/` 分隔；`python` 需要能 `-m webcoder.bridge`，即装过本项目或 Agent 包）：

   ```powershell
   $env:CONTROL_PLANE_API_KEY = "<Runtime API key>"
   tunnel-client init --sample sample_mcp_stdio_local --profile webcoder --tunnel-id <tunnel_id> --mcp-command "python -m webcoder.bridge --url http://127.0.0.1:8787/mcp --token-file C:/Users/<你>/.webcoder/token.txt"
   tunnel-client doctor --profile webcoder --explain
   tunnel-client run --profile webcoder
   ```

5. 在 [chatgpt.com/plugins](https://chatgpt.com/plugins) 点 **+** → Connection 选 **Tunnel** 并选中刚才的隧道 → Authentication 选 **No Authentication** → 创建。
6. 新建对话，从工具菜单加入这个应用即可。

在中国大陆使用时，运行 tunnel-client 的电脑需要能访问 OpenAI（它支持通过环境变量设置出站代理）。

桥接程序最多同时转发 16 个请求，再多的请求在本机排队，等前面的完成再发；它不跟随任何重定向（否则令牌会被带到别处）。

## 连接 Codex

| 方式 | 命令 |
| --- | --- |
| 令牌 | 把令牌存进环境变量 `WEBCODER_TOKEN`，然后 `codex mcp add webcoder --url https://你的hub/mcp --bearer-token-env-var WEBCODER_TOKEN` |
| OAuth | `codex mcp add webcoder --url https://你的hub/mcp` → `codex mcp login webcoder --scopes read,write` |

也可以直接写 `~/.codex/config.toml`：

```toml
[mcp_servers.webcoder]
url = "https://你的hub/mcp"
bearer_token_env_var = "WEBCODER_TOKEN"
tool_timeout_sec = 120
```

## MCP 工具

| 工具 | 权限 | 作用 |
| --- | --- | --- |
| `list_projects` | 读 | 列出可访问的项目、所在设备、在线状态、只读/读写 |
| `list_dir` | 读 | 列目录（可递归，`.git`、`node_modules` 等默认不展开） |
| `read_file` | 读 | 带行号读取文本，返回整文件 sha256；`encoding=base64` 读二进制 |
| `read_files` | 读 | 一次读取最多 20 个文件 |
| `search` | 读 | 内容搜索（字面量/正则、glob 过滤、上下文行） |
| `find_files` | 读 | 按文件名 / 路径 glob 找文件和文件夹（如 `*.py`、`src/**/*.ts`） |
| `stat` | 读 | 类型、大小、修改时间、sha256 |
| `view_image` | 读 | 把项目里的 PNG / JPEG / GIF / WebP 图片（最大 8 MB）直接显示在 ChatGPT 对话里（MCP Apps 组件） |
| `write_file` | 写 | 新建或覆盖；覆盖必须带 `expected_sha256`；保留原换行符和 BOM |
| `edit_file` | 写 | 精确字符串替换（唯一匹配、原子应用、返回 diff） |
| `delete_path` | 写 | 删除文件（需 sha，自动备份）或空文件夹 |
| `move_path` | 写 | 移动 / 重命名，从不覆盖已有目标 |
| `make_dir` | 写 | 创建文件夹 |
| `copy_file` | 写 | **跨项目、跨设备复制**（经 Hub 中转，默认上限 16 MB，复制后校验哈希） |
| `list_backups` | 读 | 查看自动备份 |
| `restore_backup` | 写 | 从备份恢复（恢复前会再备份当前版本） |

所有写操作都是“乐观锁”：必须带上最近一次读取得到的 sha256，文件在此期间被人改过就返回 `SHA_MISMATCH`，不会覆盖别人的修改。工具带有 MCP 注解（`readOnlyHint` 等）和 ChatGPT 使用的 `securitySchemes`：ChatGPT 据此决定哪些操作需要你确认，Codex 的审批模式据此区分读写。

## 安全模型

| 层 | 措施 |
| --- | --- |
| Agent | 只访问 `allowed_roots` 内的项目根；路径不能含 `..`/绝对路径；解析符号链接/联接后仍须在项目内；默认拒绝 `.env`、私钥、`.npmrc`、`.ssh/*` 以及同机 Hub 的数据库（`hub.db`）和 `master.key` 等；`.git/` 不能读写，裸仓库的 config 也不能读（这两条都按完整路径判断：把允许的文件夹或项目根直接设成 `~/.ssh`、`~/.aws` 或某个 `.git` 也绕不过去）；可整机只读；写入原子替换并备份到项目外；含 junction 的文件夹不跨文件系统移动（复制会把它指向的项目外内容一起搬进来） |
| 通道 | 设备密钥只在配对时传一次，并用 Agent 这次配对的一次性密钥封装（X25519 + HKDF + AES-GCM），旁听配对的人拿不到；之后连接时密钥不再上网：HMAC 挑战–应答**双向**认证，每帧 AES-256-GCM 加密，按连接、按方向派生密钥，序号严格递增防重放 |
| Hub | 令牌只存哈希；设备密钥用 `master.key` 加密存储；面板会话 + CSRF，面板接口的响应一律不许缓存（`no-store`）；登录/配对/注册/授权请求/换令牌/吊销/邀请链接限流，等待确认的授权请求有总数上限；请求体在读入前限长（`/mcp` 24 MiB，其余 1 MiB），同时处理的大请求最多 2 个（请求体大的，或要经 Hub 搬运大文件、图片的调用，都算）、批量请求最多 50 条；Agent 在 WebSocket 升级请求里就用设备密钥证明自己（HMAC，5 分钟内有效、只能用一次），其余连接在认证前最多 2 个同时握手、10 秒超时，所以陌生人占不住 Agent 的入口；单帧不超过实际需要的大小（默认约 25 MiB）；审计日志 |
| 账号 | 每个用户的设备、项目、令牌、OAuth 授权、审计日志互相隔离，所有查询都按用户过滤；邀请 / 重置链接只存哈希、一次性、有有效期；停用或删除的用户，其会话、令牌、刷新令牌和设备连接立即失效 |
| OAuth | 仅公开客户端 + PKCE S256；回调白名单只含 ChatGPT（`chatgpt.com`）与本机回环地址（Codex）；访问令牌绑定本 Hub；刷新令牌轮换，复用即吊销；每次授权需面板主人在审批页确认，可限定项目与读写；权限不足时通过 `_meta["mcp/www_authenticate"]` 让 ChatGPT 重新授权 |
| 范围 | 在你的电脑上**不执行任意命令**：只有文件读写、参数固定的 git 和手机操作，以及你在设备上列入白名单的命令行工具（不能是 shell 或解释器）。只有你在设备上列出的 SSH 服务器能执行任意命令，这项需要单独授权，每条命令都要确认，并记入审计日志 |

注意：直连方式下面板和 `/mcp` 必须走 HTTPS；Agent 的配对和通道即使走 `http://` / `ws://` 也是加密的（这种 Hub 会拒绝还不会加密配对的旧版 Agent，请用面板里的安装命令重装），但面板密码、访问令牌和安装命令下载的程序不是。文件内容会进入 ChatGPT 的上下文，项目里别放不想让模型看到的东西。

## 配置参考

Hub 环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WEBCODER_PUBLIC_URL` | `http://127.0.0.1:8787` | 客户端访问的地址（只能是 origin，不能带路径） |
| `WEBCODER_DATA_DIR` | `./data` | SQLite 数据库与 `master.key`（两者要一起备份） |
| `WEBCODER_HOST` / `WEBCODER_PORT` | `127.0.0.1` / `8787` | 监听地址 |
| `WEBCODER_CLIENTS` | `chatgpt,codex` | 启用哪些客户端的 OAuth 回调规则 |
| `WEBCODER_EXTRA_REDIRECT_URIS` | 空 | 额外允许的精确回调地址（逗号分隔） |
| `WEBCODER_ALLOWED_ORIGINS` | 空 | 允许带 `Origin` 头访问 `/mcp` 的浏览器来源 |
| `WEBCODER_AGENT_TIMEOUT` | `45` | 等待 Agent 应答的秒数 |
| `WEBCODER_MAX_TRANSFER_MB` | `16` | 跨设备复制上限（源和目标设备的 Agent 还各有 `max_write_mb` 上限，默认也是 16：调高时两端一起调，以较小的为准） |
| `WEBCODER_TRUSTED_PROXIES` | `127.0.0.1` | 信任其 `X-Forwarded-*` 的反向代理 |

Agent 配置（`~/.webcoder-agent/config.json`）：`hub_url`、`allowed_roots`、`read_only`、`proxy`（`system` / `none` / 代理地址）、`deny`（自定义拒绝列表，`null` 为内置列表）、`max_write_mb`、`backup_keep`、`mobile`（手机控制开关）、`ssh_hosts`（允许连接的服务器）、`ssh_command`（ssh 客户端路径，留空自动查找）、`git_command`、`skills` 与 `skill_dirs`（共享 skills）、`commands`（允许运行的命令行工具）与 `command_paths`（不在 PATH 上的程序路径）。

## 开发与测试

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[hub,dev]"    # Linux/macOS 用 .venv/bin/python
python -m pytest -q
```

测试（277 个）使用真实的 Hub（uvicorn）和真实的 Agent（WebSocket），覆盖：路径与权限边界、编辑语义、加密通道、MCP 协议细节、OAuth 全流程（按 ChatGPT 的方式：动态注册、空 scope、以域名作 resource、`iss`、`_meta` 重新授权；以及 Codex 的回环回调与刷新令牌轮换）、多用户隔离（设备 / 项目 / 令牌 / OAuth / 审计）、邀请与重置链接、停用与删除用户、单用户数据库自动迁移、一键安装命令（在真实的 PowerShell 和 bash 里验证含空格、引号和中文的文件夹路径原样传给安装脚本）、运行中的 Agent 热加载文件夹与重新配对、删除设备后 Agent 自动退役（在线 / 离线、伪造的删除通知无效、只清理自己的自启项）、单实例锁、手机控制（用假的 adb / hdc 验证参数转义、权限、项目边界和 MCP 工具）、SSH（用假的 ssh 验证主机白名单、选项注入防护、非交互选项、超时、上传不覆盖、下载进项目、按主机选电脑和审计）、git（用真实仓库验证钩子不执行、参数不能变成选项、机密不进 diff / 暂存 / clean、丢弃改动前的快照、推送到本地裸仓库、冲突处理和只读设备）、在对话里显示图片（用真实浏览器验证 MCP Apps 组件的握手和渲染）、skills（只读、只开放 skills 文件夹、开关与令牌范围）、本机命令行工具（禁止清单、参数原样传递、用真实 node 验证 npm 包装脚本不经过 cmd.exe、GBK 输出解码、授权与审计）、跨设备复制、stdio 桥接（含 `--token-file`）、插件包（用官方 Agent Plugins Schema 校验）、官方 MCP Python SDK 客户端、以及用 Playwright 驱动的面板 UI 冒烟测试（需要本机有 Edge 或 Chrome，否则自动跳过）。

Windows 一键安装脚本已在 Windows 11 + Windows PowerShell 5.1 上实跑：全新安装、重复运行（升级 + 重新配对）、计划任务开机自启与停止、删除设备后自动移除计划任务、彻底卸载命令、安装失败时恢复原 Agent。Linux 脚本在 Debian 12 上对正式 Hub 实跑过（不含自启部分）；macOS 脚本只做了语法和参数传递测试。

真实客户端验证：Codex CLI 0.149.1（`codex exec` 调用 `list_projects` → `read_file` → `edit_file`）。ChatGPT 网页版的公网 HTTPS + OAuth 路径已用真实账号验证：动态注册、授权、读写项目文件，以及调用手机工具时按需重新授权（加上手机权限），之后用真机（安卓）完成列出手机、读屏幕和截图。Secure MCP Tunnel 路径按官方文档实现并用模拟流程测试，尚未用真实账号验证。

## 目录结构

```text
webcoder/
├── common/     # 共享：错误类型、加密通道、工具函数
├── agent/      # 设备端：文件操作沙箱（fileops.py）、连接循环、命令行
├── hub/        # 中心服务：MCP、OAuth、面板 API、Agent 中继、插件包生成、静态面板
└── bridge/     # stdio ↔ HTTP 桥接（Secure MCP Tunnel 用，仅标准库）
tests/          # pytest：单元、端到端、SDK 兼容、面板 UI
deploy/         # HTTPS（Caddy）部署文件
```

## 已知限制

- 编辑类操作只处理 UTF-8 文本（GBK 等编码请用 `encoding=base64` 读写原始字节）。
- 多用户之间互相隔离，但都要信任 Hub 的运营者（见「多用户」）。
- OAuth 客户端注册只支持动态注册（DCR），暂不支持 CIMD。
- 未实现 MCP 2026-07-28 无状态协议本身（新版客户端会自动回退到 2025-11-25）。
- Docker / Caddy 部署文件、Secure MCP Tunnel 接入尚未在本次开发环境中实测。
