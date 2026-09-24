# 协作中心：dot / Work 聊天协作

协作中心让你自己的几个助手聊天共用持久的讨论记录、任务、证据和结论，并按用途分工：

- **dot 聊天**：协调。接收结果汇总任务（`dot-coordination` 队列），以及分析结果、异常变化的通知。
- **Work 聊天**（ChatGPT 网页的 Work，或桌面端选择 Cloud 的 Work）：只读分析。领取异常分析、监控计划草拟等任务（`work-analysis` 队列）。

本功能移植自 [CodePier](https://github.com/cyeinfpro/codepier) 的协作中心（MIT 许可，版权声明见 `webcoder/hub/collaboration/__init__.py`），并接到 WebCoder 的账号、授权和工具上。默认关闭；不开启时 Hub 的行为、工具列表和 MCP 协议都与之前完全相同。

## 1. 功能开关

```dotenv
WEBCODER_COLLABORATION_ENABLED=false
WEBCODER_MCP_EVENTS_ENABLED=false
WEBCODER_MONITOR_COLLECTOR_ENABLED=false
WEBCODER_ANALYSIS_DISPATCH_ENABLED=false
```

| 开关 | 作用 |
| --- | --- |
| `COLLABORATION_ENABLED` | 主开关：面板「协作中心」和 `collaboration_*` / `monitor_plan_*` 工具。另外三个开关都需要它，否则 Hub 拒绝启动 |
| `MCP_EVENTS_ENABLED` | MCP Events：`/mcp` 接受 2026-07-28 新协议，提供事件目录、订阅和 webhook 投递。dot / Work 聊天收通知需要它 |
| `MONITOR_COLLECTOR_ENABLED` | 采集已批准、未过期、未暂停的监控计划里的固定 HTTP 探针 |
| `ANALYSIS_DISPATCH_ENABLED` | 监控异常自动创建分析任务，并把任务可用事件投递给消费者 |

只开主开关即可试用讨论室和手动领取任务；事件或派发关闭时，面板显示「待人工领取」，不会假装模型已被唤醒。四个开关都不会额外开放写文件、运行程序或部署的能力，也不能绕过项目授权。数据库表在升级时就会建好（开关关闭时同样建好），之后打开开关不需要额外的迁移步骤。

## 2. 谁是谁

- **房主**：登录面板的用户本人。每个用户就是自己的「个人空间」，只能看到和管理自己的项目、讨论室和连接；管理员也看不到别人的协作数据。
- **连接**：一个 WebCoder 授权（面板里的访问令牌，或 ChatGPT / Codex 的 OAuth 授权）。连接的权限照常由它的范围决定，协作中心把它们换算成能力：`read` → read，`write` → write，`cli` → execute（在项目所在电脑上运行允许的程序）。`ssh`、`mobile` 不算协作能力。
- **只读连接**：只有 `read` 权限的连接。领取只读任务（异常分析、结果汇总、监控计划草拟）只接受只读连接；带写入、程序、SSH 或手机权限的连接会被拒绝登记为任务消费者，但它们可以接收通知、在讨论室发言、参与目标协作。

同一个授权下的多个聊天，服务端无法区分。聊天名称、加入码、位置编号都不是身份证明；需要强隔离时给每个聊天单独授权。

## 3. 接入 dot / Work 聊天

### 面板建位置 → 加入码 → 宿主订阅 → 聊天收件确认

1. 在「协作中心」为项目建立讨论室，打开「智能体与订阅」。为每个 dot 或 Work 聊天分别创建位置并命名。
2. 复制完整的加入指令，发到目标聊天里（聊天里要已经连接了 WebCoder）。加入码形如 `WAJ-XXXX-XXXX-XXXX-XXXX`，默认 30 分钟有效，未使用时可以刷新（旧码立即失效）；位置默认有效 7 天。
3. 聊天调用 `collaboration_join`：服务端重新核对这个连接的账号、项目和授权，登记位置，并返回 `subscription_requests`。此时只完成登记：不创建任务、不授予权限、不生成回调或签名材料。
4. 宿主按自己的确认流程创建 MCP Events 订阅。Work 位置订阅任务和状态两类事件；dot 另外订阅结果和异常变化，共四类。面板显示已有 N/M 个订阅和缺少的事件。已登记但缺订阅的位置提供「继续订阅指令」，加入码过期后也能在位置有效期内继续。
5. 订阅通过 challenge 验证后，在面板发送测试事件，到目标聊天核对每个测试事件编号。回调返回 2xx 只说明对方接收了 webhook，不说明聊天处理了或你看到了。
6. 实际看到测试后勾选确认收件。必需的订阅齐全、全部确认后，位置才显示「完整接通」。

| 聊天用途 | 事件 | 参数 |
| --- | --- | --- |
| Work 分析 | `webcoder.collaboration.task_available.v1` | `project_id`、`environment_id`、`slot_id`、`queue=work-analysis` |
| Work / dot 状态 | `webcoder.monitor.status_changed.v1` | `project_id`、`environment_id`、`slot_id` |
| dot 协调 | `webcoder.collaboration.task_available.v1` | `project_id`、`environment_id`、`slot_id`、`queue=dot-coordination` |
| dot 结果 | `webcoder.monitor.result_ready.v1` | `project_id`、`environment_id`、`slot_id` |
| dot 异常状态 | `webcoder.monitor.incident_changed.v1` | `project_id`、`environment_id`、`slot_id` |

`project_id` 必须是项目的固定 ID（面板「项目」页可见，形如 `prj_…`），不能用别名。另外还有两类单独订阅的事件：`webcoder.collaboration.message_mentioned.v1`（讨论室里被 @ 提醒）和 `webcoder.collaboration.work_available.v1`（目标里有分配给这个连接的工作）。

不用加入码也可以：在已连接 WebCoder 的聊天里直接请求宿主订阅某个项目、环境的事件。只要连接对该项目有读取权限就能订阅，首次订阅只会建立一个被动的记录容器，不创建任务、智能体或监控计划。

### 插件能看到事件，但仍是 0/N

插件详情页能列出事件，只说明 `events/list` 发现成功，不说明宿主的自动化服务已经接受了这个连接器。让原 dot / Work Cloud 聊天读取当前可订阅的事件源，使用宿主返回的实际连接器标识；如果宿主在创建订阅前就报错、Hub 也没收到 `events/subscribe`，问题出在宿主一侧。不要反复换加入码、伪造回调，或手工改数据库。

## 4. 讨论室

普通消息只保存讨论：不创建任务、不调用工具、不启动监控。回复引用同一房间的原消息，任务结果回到原话题。房间可以关联多个项目，每条消息都标明所属项目；读取时按当前授权过滤，撤权后旧链接、缓存和游标都拿不到内容。

助手要在房间发言，需要你明确允许这个连接在选定项目里发言（有有效期，可随时撤回），它才能调用 `collaboration_message_create`。普通文字里的 `@` 只是内容；提醒对象要在选择器里选，并且对方位置需要单独订阅 `message_mentioned` 事件。助手发出的回复不会再去唤醒其他助手，避免互相循环。

## 5. 只读任务

在消息上选择「转为任务」，确认目标对象、任务类型（`analyze_incident` 异常分析、`propose_monitor_plan` 监控计划草拟、`summarize_result` 结果汇总）、上下文和验收要求后创建。异常分析只能交给 Work，结果汇总只能交给 dot。助手通过 `collaboration_command_create` 提出的指令只保存为「待批准建议」，你在面板批准后才创建任务。

消费者先读取任务，再按当前版本领取：

```json
{
  "project": "<项目 ID 或别名>",
  "environment_id": "production",
  "job_id": "<任务 ID>",
  "expected_version": 1,
  "idempotency_key": "claim:<稳定请求标识>"
}
```

`collaboration_claim` 返回 attempt、fencing_token、lease_until、deadline_at 和上下文。租约最多 15 分钟，任务总期限最多 30 分钟；`collaboration_heartbeat` 可以续租，但不能超过原期限。`collaboration_result` 提交结构化分析，必须带原 job_id、attempt、fencing_token 和稳定的幂等键：重试同一请求返回原回执，不同内容不能覆盖同一轮结果，过期租约的结果只单独存证。重要事实放在 observations 并引用有效的 evidence_refs，outcome 为 healthy / explained 时必须有证据。缺权限、缺资料或平台拒绝时调用 `collaboration_block` 并停止，不要换身份绕过。

证据读取：Work 用有效的任务租约读取任务上下文里的证据；dot 用结果编号读取该结果引用的证据。dot 处理完结果后可以调用 `collaboration_ack`（表示业务已处理，不表示你已读）。

预算按项目计算（所有环境共用）：最多 2 个未结束任务、每个队列 1 个有效租约、每小时 6 个、每天 20 个新任务。

## 6. 目标协作

在讨论室创建目标：写明目标和验收方式，选择项目、参与的连接、初始协调连接、能力上限（read / write / execute）、期限和预算。助手可以提出或修改目标（`collaboration_goal_create` / `update`），但只有你能在面板批准；修改后必须重新批准。批准时会检查你自己和每个参与连接当前确实具备这些能力（写入要求项目是读写的，execute 要求项目所在电脑允许运行程序）。

批准后，初始协调连接会收到一个协调工作项，然后可以在目标范围内拆分（`collaboration_work_create`）、指派（`collaboration_work_assign`）、讨论（`collaboration_goal_message`）和复核。每个工作项要求的能力不能超过目标批准的上限，执行时还会按连接当前的权限再查一次。

执行步骤用 `collaboration_work_execute`：参数 `tool` 是该项目可用的 WebCoder 工具名（如 `read_file`、`search`、`edit_file`、`write_file`、`git_status`、`git_commit`、`cli_run`），`arguments` 是该工具的常规参数（`project` 自动填为工作项的项目）。读类工具需要 read，文件和 git 修改需要 write，`cli_run` 需要 execute。每一步都会：

- 先在事务里登记一条操作（检查租约、能力、预算、项目），再真正执行；同一个幂等键重放只返回原操作，不会再执行一次；
- 执行前再次核对目标和授权，失去授权的步骤会被取消、从不发出；
- 和普通工具调用一样经过 WebCoder 的项目边界和 Agent 策略，并记入审计日志；
- 返回操作编号、状态和工具输出。超过租约期限仍未结束的步骤记为 `unknown`（不知道是否已生效），不会被自动重试。
- 完整输出只在执行时返回给调用的助手；面板和 `collaboration_goal_read` 里每个步骤只保留前 2000 个字符的摘要。

提交工作结果（`collaboration_work_result`）时必须列出本次尝试的全部真实操作；还有未结束的操作不能宣称完成，失败或取消的操作不能当作成功。暂停、取消、到期和撤权会阻止后续步骤；已经发到电脑上的程序可能仍在运行，面板保留真实状态，不宣称已经停止。

## 7. 监控

在「监控」中登记固定的 HTTP GET 目标（确认它不修改业务数据，URL 不含凭据）。内部服务需要填写明确允许的私网网段；明文 HTTP 需要单独勾选。计划（阈值、窗口、样本量、到期时间，最长 30 天）的每个版本不可更改，「只校验」不保存，「保存新草稿」不激活，审阅最新版本后单独批准才开始采集。

这些信号是合成探针的观测，不代表全部用户请求、主机或数据库的健康。采集器未开启、数据过期、样本不足、恢复探针失败都会明确显示；恢复只由新鲜的探针和连续窗口判断，模型的结论不能把异常标为已恢复。采样默认保留 7 天，证据正文 14 天。

## 8. MCP Events 协议

开启 `WEBCODER_MCP_EVENTS_ENABLED` 后，`/mcp` 除了原有的 2025 协议外，还接受 2026-07-28 协议的请求：每个请求的 `params._meta` 带 `io.modelcontextprotocol/protocolVersion` 和 `clientCapabilities`，并用 `MCP-Protocol-Version`、`Mcp-Method`（`tools/call` 等还有 `Mcp-Name`）请求头镜像。支持 `server/discover`（声明 `capabilities.events`）、`events/list`、`events/subscribe`、`events/unsubscribe`，以及 `tools/*`、`resources/*`、`prompts/list`；结果带 `resultType: "complete"`。旧协议的 `initialize` 不声明事件。

- **订阅**：`delivery.mode` 只支持 `webhook`，回调必须是无凭据、无片段的 HTTPS 地址，签名材料为 `whsec_` 加 Base64（24–64 字节）。首次订阅会向回调发送 challenge（Standard Webhooks 签名），回答正确才保存；30 分钟内用同一回调和密钥续订不再重复 challenge。`ttlMs` 被收紧到 60 秒–24 小时；换新密钥时旧密钥在 5 分钟内双签。
- **投递**：每次投递重新解析 DNS，只连接公网单播地址并保留原 Host / TLS 主机名，不跟随重定向，不读代理设置，正文不超过 16 KiB。同一订阅按顺序投递，失败按 30 秒、2 分钟、10 分钟、30 分钟重试，最多 5 次；410、413 等不会无限重试，也不会被当作退订。
- **游标**：订阅返回签名游标。省略或 `null` 表示从现在开始（不重放历史）；带旧游标重新订阅可以补投，最多回溯 7 天（或 `maxAgeMs`），超出时返回 `truncated: true`。
- **错误码**：参数 / 回调地址 / 签名材料 / 游标错误为 -32602，不支持的投递方式 -32014，未授权 -32012，事件或订阅不存在 -32011（`data.kind` 区分），超出上限 -32013，回调验证失败 -32015（`data.reason` 只有固定类别，不含回调地址或响应内容）。

## 9. 与 CodePier 的差异

- 没有 Space / OIDC / 动态角色：每个用户就是自己的空间，房主即登录面板的用户本人。
- 能力来自 WebCoder 的授权范围（见第 2 节），不是 CodePier 的 read / write / execute 授权。
- 加入码前缀是 `WAJ-`，事件名前缀是 `webcoder.`，指令里提及的插件是 `@WebCoder`。
- 目标步骤调用 WebCoder 的工具（按工具名），操作记录在协作中心自己的表里；CodePier 的 `read` / `write` / `edit` / `exec` 四个抽象工具和持久化执行引擎没有移植。
- 环境变量前缀是 `WEBCODER_`；Hub 依赖新增 `httpx`（回调与探针）和 `tzdata`（维护时间段的 IANA 时区，Windows 需要）。

## 10. 运维与限制

- 备份必须同时包含数据库和 `master.key`：订阅的回调地址和签名材料用它加密。恢复旧数据或回滚前，先把四个开关都关掉，核对原任务和投递状态后再打开。
- 删除用户时，其全部协作数据（讨论室、消息、任务、订阅、目标、监控）一起删除。删除项目后，相关讨论室保留但无法再访问。
- 首期没有自动归档消息、结果、审计和投递历史，需要留意数据库增长。
- 宿主（ChatGPT 的 dot / Work）能否订阅、何时处理事件、是否回应，需要在真实环境验证；本仓库的测试使用本地接收器，不创建真实的 ChatGPT 订阅。CodePier 自己也还没有完成真实 dot / Work 路由的验收。
- Hub 需要单进程运行（默认如此）：投递的并发限制和扫描位置保存在内存里。
