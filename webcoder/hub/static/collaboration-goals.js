"use strict";
// 协作中心 · 协作目标：规划、房主批准执行范围、工作项、实际操作与目标内消息。
// 移植自 CodePier 的 web/collaboration-goals.js（https://github.com/cyeinfpro/codepier，MIT 许可，
// Copyright (c) 2026 Remote Dev MCP Agent contributors），按 WebCoder 的工具与权限改写。
// 目标提议不会创建凭据、订阅或执行权限；执行需要房主另外批准。
window.WebCoderCollaborationGoals = {
  create(ctx) {
    const {
      state, E, field, button, empty, getRecord, mutation, showDrawer, closeDrawer, projectLabel, roomProjects,
      when, onPage, rerender,
    } = ctx;
    let items = [];
    let options = null;
    let detail = null;
    let revision = 0;
    let loading = 0;
    let projection = "";
    const names = {
      proposed: "讨论与规划 · 尚未启用执行",
      active: "执行范围已批准",
      paused: "已暂停",
      cancelled: "已取消",
      cancelling: "已请求取消 · 未证明已停止",
      expired: "已到期",
      blocked: "等待处理",
      queued: "等待领取",
      leased: "已领取",
      running: "正在处理",
      succeeded: "已提交结果",
      completed: "已完成",
      failed: "失败",
      pending: "等待依赖",
      unknown: "结果未知 · 可能已生效",
      awaiting_review: "等待复核",
    };
    // WebCoder 的能力：read = 读取文件 / 搜索 / git 查看；write = 改文件与 git 提交；execute = cli_run 运行本机程序
    const capNames = { read: "读取项目", write: "修改项目文件", execute: "运行本机程序（cli_run）" };
    // A step's own state (the tool call), apart from the work item's.
    const operationStates = {
      queued: "已接受 · 尚未执行", running: "正在执行", cancelling: "已请求取消 · 未证明已停止", succeeded: "执行成功",
      failed: "执行失败", cancelled: "已取消 · 未执行或已停止", unknown: "结果未知 · 可能已生效",
    };
    const workReasons = {
      GOAL_PAUSE: "目标已暂停", GOAL_CANCEL: "目标已取消", GOAL_REVISED: "目标已修订，需重新批准", GOAL_REAPPROVED: "目标已重新批准",
      LEASE_EXPIRED: "领取已过期", ROOM_PAUSED: "协作室已暂停",
    };
    const cancelNotes = {
      cancelled_before_delivery: "目标停止时操作尚未投递，已取消",
      cancel_requested_in_flight_not_proven_stopped: "已请求取消正在运行的程序，但无法证明它已停止",
      in_flight_file_operation_tracked_not_cancellable: "文件操作已在执行，无法中途取消，仅继续记录真实状态",
    };
    const local = (message = "") => ({ local: true, message });
    const status = (value) => names[value] || value || "暂无记录";
    const badge = (value) => `<span class="cc-status">${E(status(value))}</span>`;
    const canManage = () => Boolean(state.snapshot?.can_manage);
    const canApprove = () => canManage() && options?.can_approve === true;
    const enabled = () => Boolean(state.snapshot?.capabilities?.coordination_goals);
    const connection = (id) => options?.participants?.find((p) => p.grant_id === id);
    const connectionName = (id) => `${connection(id)?.label || "助手连接"} · ${String(id).slice(-8)}`;
    const goalById = (id) => (detail?.goal?.id === id ? detail.goal : items.find((g) => g.id === id));
    const goalScope = (id) => {
      const g = goalById(id);
      if (!g) throw new Error("目标记录已改变，请重新读取。");
      return { project: g.anchor_project_id || state.project, environment_id: g.environment_id || state.environment };
    };
    const mutateGoal = (operation, args) => mutation(operation, { ...(args.goal_id ? goalScope(args.goal_id) : {}), ...args });
    const token = () => [state.generation, revision, state.project, state.conversation, state.identity];
    const current = (t) => t.every((value, index) => value === token()[index]) && onPage();

    function clear() {
      revision += 1;
      loading += 1;
      items = [];
      options = null;
      detail = null;
      projection = "";
    }

    function privateClear() {
      clear();
      closeDrawer();
      const drawer = document.querySelector("#cc-drawer");
      if (drawer) drawer.innerHTML = "";
      for (const node of document.querySelectorAll(".cc-coordination-list, .cc-coordination-context")) {
        node.innerHTML = empty("连接或项目范围已改变，请重新读取。");
      }
    }

    async function load(snapshot = state.snapshot, sourceCurrent = () => true) {
      if (!snapshot?.room || !snapshot.capabilities?.coordination_goals) {
        clear();
        return;
      }
      const t = token();
      const request = ++loading;
      const fresh = () => current(t) && request === loading && sourceCurrent();
      try {
        // Read the authority projection first. Never reuse an earlier goals response
        // after observing revocation or a changed project projection.
        const nextOptions = await getRecord("coordination_options", "");
        if (!fresh()) return;
        const nextProjection = JSON.stringify({ participants: nextOptions.participants || [],
          can_approve: nextOptions.can_approve });
        if (projection && projection !== nextProjection) {
          privateClear();
          return load(snapshot, sourceCurrent);
        }
        options = nextOptions;
        projection = nextProjection;
        const collected = [];
        const cursors = new Set();
        let cursor = "";
        do {
          const page = await getRecord("coordination_goals", "", cursor, { limit: "30" });
          if (!fresh()) return;
          collected.push(...(page.items || []));
          cursor = page.next_cursor || "";
          if (cursor && cursors.has(cursor)) throw new Error("目标分页游标重复，请刷新后重试。");
          if (cursor) cursors.add(cursor);
        } while (cursor);
        if (!fresh()) return;
        items = [...new Map(collected.map((g) => [g.id, g])).values()];
        if (detail) {
          const latest = items.find((g) => g.id === detail.goal?.id);
          if (!latest || latest.version !== detail.goal.version) {
            detail = null;
            const drawer = document.querySelector("#cc-drawer");
            if (drawer?.querySelector(".cc-goal-detail, #cc-goal-approve, #cc-goal-cancel, #cc-goal-draft[data-id]:not([data-id=''])")) {
              closeDrawer();
              drawer.innerHTML = "";
            }
          }
        }
      } catch (error) {
        if (error.stale || !fresh()) return;
        if ([401, 403, 409].includes(error.status)
            || ["FORBIDDEN", "PERMISSION_DENIED", "AUTHORIZATION_CHANGED", "ACCESS_CHANGED"].includes(error.code)) {
          privateClear();
        }
        throw error;
      }
    }

    function action(label = "＋ 目标") {
      return enabled() && canManage() ? button("goal-new", label) : "";
    }

    function usage(g) {
      const budget = g.budget || {};
      const used = g.usage || {};
      return `工作项 ${E(used.work_items ?? 0)}/${E(budget.max_work_items ?? "—")} · 步骤 ${E(used.steps ?? 0)}/${E(budget.max_steps ?? "—")}
        · 协调消息 ${E(used.messages ?? 0)}/${E(budget.max_messages ?? "—")}`;
    }

    function summary(g) {
      return `<article class="cc-goal-summary" data-goal-id="${E(g.id)}"><div class="cc-row"><strong>${E(g.objective)}</strong>${badge(g.state)}</div>
        <p>${E(g.acceptance)}</p><small>${E((g.project_ids || []).map(projectLabel).join(" · "))} · ${(g.participant_grant_ids || []).length} 个助手连接 · v${E(g.version)}</small>
        <div class="cc-actions">${button("goal-detail", "查看步骤与结果", g)}${canApprove() && ["proposed", "paused"].includes(g.state)
          ? button("goal-review", "审阅执行范围", g) : ""}</div></article>`;
    }

    function context() {
      if (!enabled()) return "";
      return `<section class="cc-coordination-context"><div class="cc-row"><h3>协作目标</h3>${action("新目标")}</div>
        ${items.slice(0, 3).map(summary).join("") || empty("给出想完成的结果。先规划，再由房主确认执行范围。")}</section>`;
    }

    function list() {
      if (!enabled()) return "";
      return `<section class="cc-coordination-list"><div class="cc-row"><h2>目标与工作项</h2>${action("创建协作目标")}</div>
        <p class="cc-hint">助手可围绕目标拆解、交接与复核。保存目标仅建立规划；执行需房主另外确认已有连接与能力。</p>
        ${items.map(summary).join("") || empty("还没有协作目标。")}</section>`;
    }

    function choices(name, rows, selected) {
      return rows.map(([id, label]) => `<label><input type="checkbox" name="${name}" value="${E(id)}" ${selected.includes(id) ? "checked" : ""}>${E(label)}</label>`).join("");
    }

    function draftForm(goal = null) {
      const projects = roomProjects().filter((p) => p.environment_id === state.environment);
      const participants = options?.participants || [];
      const caps = options?.capabilities || [];
      const budget = goal?.budget || options?.budget_defaults || {};
      const number = (name, label, value, min, max) =>
        field(label, `<input name="${name}" type="number" min="${min}" max="${max}" step="1" value="${E(value)}" required>`);
      return `<p>写清希望达到的结果与验收条件。保存后仍需房主单独批准，才会接受执行。</p>
        <form id="cc-goal-draft" class="cc-form" data-id="${E(goal?.id || "")}" data-version="${E(goal?.version || "")}">
        ${field("想完成什么", `<textarea name="objective" rows="3" maxlength="4000" required placeholder="例如：找出失败原因，修复并提供测试证据">${E(goal?.objective)}</textarea>`)}
        ${field("怎样算完成", `<textarea name="acceptance" rows="2" maxlength="2000" required placeholder="写出可核对的结果或证据">${E(goal?.acceptance)}</textarea>`)}
        <fieldset class="cc-project-choices"><legend>本目标涉及的项目</legend>${choices("project_ids",
          projects.map((p) => [p.project_id, p.project_label || projectLabel(p.project_id)]), goal?.project_ids || [state.project])}</fieldset>
        <p class="cc-hint">之后加入房间的项目不会自动加入这个目标。</p>
        <fieldset class="cc-project-choices"><legend>参与的助手连接</legend>${choices("participant_grant_ids",
          participants.map((p) => [p.grant_id, connectionName(p.grant_id)]), goal?.participant_grant_ids || [])}
          ${participants.length ? "" : empty("没有可用的助手连接。请先在「访问令牌」创建令牌或在 ChatGPT 里完成 OAuth 授权，并确认它能读取目标项目。")}</fieldset>
        ${options?.truncated ? `<p class="cc-hint">当前仅显示前 ${E(options.scan_limit || 128)} 个可扫描连接，候选可能不完整。</p>` : ""}
        ${field("首位协调助手", `<select name="coordinator_grant_id" required><option value="">先选择参与连接</option>${participants
          .filter((p) => (goal?.participant_grant_ids || []).includes(p.grant_id))
          .map((p) => `<option value="${E(p.grant_id)}" ${p.grant_id === goal?.coordinator_grant_id ? "selected" : ""}>${E(connectionName(p.grant_id))}</option>`)
          .join("")}</select>`)}
        <p class="cc-hint">批准后，先交给这个连接一个只读协调工作项，由它拆解、交接和复核。职责名称不授予权限。</p>
        <fieldset class="cc-project-choices"><legend>拟使用的已有能力</legend>${choices("capabilities",
          caps.map((c) => [c, capNames[c] || c]), goal?.capabilities || ["read"])}</fieldset>
        <p class="cc-hint">所有参与连接须已有全部目标项目的读取权限。能力是目标上限；每项工作按所需能力选择连接，只读助手可参与复核。
          修改文件需要读写项目和带写入权限的连接；运行程序还需要 cli 权限，且项目所在电脑在「设备 → 修改设置」里允许了程序。</p>
        <div class="cc-two">${number("duration_minutes", "执行有效期（分钟）", (goal?.duration_seconds || 3600) / 60, 1, 1440)}
          ${number("max_work_items", "工作项上限", budget.max_work_items || 20, 1, 100)}</div>
        <details><summary>步骤、交接与重试预算</summary><div class="cc-two">
          ${number("max_steps", "操作步骤上限", budget.max_steps || 50, 1, 500)}
          ${number("max_messages", "协调消息上限", budget.max_messages ?? 40, 0, 200)}
          ${number("max_attempts", "每项尝试上限", budget.max_attempts || 3, 1, 3)}
          ${number("lease_seconds", "领取有效期（秒）", budget.lease_seconds || 300, 30, 900)}</div></details>
        <p class="cc-goal-gate" role="status"></p>
        <div class="cc-actions"><button class="primary" type="submit">保存规划，不启用执行</button>${button("close-drawer", "取消")}</div></form>`;
    }

    function draftPayload(form) {
      const f = new FormData(form);
      return {
        conversation_id: state.conversation,
        objective: String(f.get("objective")).trim(),
        acceptance: String(f.get("acceptance")).trim(),
        project_ids: f.getAll("project_ids"),
        participant_grant_ids: f.getAll("participant_grant_ids"),
        coordinator_grant_id: f.get("coordinator_grant_id"),
        capabilities: f.getAll("capabilities"),
        duration_seconds: Math.round(Number(f.get("duration_minutes")) * 60),
        budget: Object.fromEntries(["max_work_items", "max_steps", "max_messages", "max_attempts", "lease_seconds"]
          .map((k) => [k, Number(f.get(k))])),
      };
    }

    function validateDraft(form, payload) {
      const issues = [];
      if (!payload.project_ids.length) issues.push("请至少选择一个项目。");
      if (!payload.participant_grant_ids.length) issues.push("请至少选择一个助手连接。");
      if (!payload.capabilities.includes("read")) issues.push("目标须保留读取能力。");
      if (!payload.participant_grant_ids.includes(payload.coordinator_grant_id)) issues.push("请选择本目标中的首位协调助手。");
      for (const id of payload.participant_grant_ids) {
        const p = connection(id);
        if (!p) {
          issues.push("所选连接已不可用。");
          continue;
        }
        const visible = p.project_ids || [];
        if (!visible.includes("*") && payload.project_ids.some((project) => !visible.includes(project))) {
          issues.push(`${connectionName(id)} 缺少所选项目权限。`);
        }
        if (!(p.scopes || []).includes("read")) issues.push(`${connectionName(id)} 缺少项目读取能力。`);
      }
      for (const project of payload.project_ids) {
        for (const cap of payload.capabilities) {
          const covered = payload.participant_grant_ids.some((id) => {
            const p = connection(id);
            return (p?.project_capabilities?.[project] || p?.scopes || []).includes(cap);
          });
          if (!covered) issues.push(`${projectLabel(project)} 的所选助手连接缺少能力：${capNames[cap] || cap}。`);
        }
      }
      const node = form.querySelector(".cc-goal-gate");
      if (node) node.textContent = issues.join(" ");
      return issues;
    }

    function review(g) {
      const budget = g.budget || {};
      const projects = g.projects || (g.project_ids || []).map((project_id) => ({ project_id }));
      return `<div class="cc-goal-review-scroll"><p class="cc-prose">${E(g.objective)}</p><p><strong>验收：</strong>${E(g.acceptance)}</p>
        <dl class="cc-facts"><div><dt>确认版本</dt><dd>v${E(g.version)}</dd></div><div><dt>有效期</dt><dd>${E(g.duration_seconds / 60)} 分钟</dd></div>
          <div><dt>能力</dt><dd>${E((g.capabilities || []).map((c) => capNames[c] || c).join("、"))}</dd></div>
          <div><dt>首位协调助手</dt><dd>${E(connectionName(g.coordinator_grant_id))}</dd></div></dl>
        <p>批准后将为上述协调连接创建一个只读协调工作项，计入工作项预算。后续工作分别核对所需能力。</p>
        <h3>固定项目范围</h3><ul>${projects.map((p) => `<li><strong>${E(projectLabel(p.project_id))}</strong>
          ${p.root ? `<br><span class="cc-goal-path">${E(p.root)}</span>` : ""}
          ${p.mode ? `<br><small>映射模式：${p.mode === "write" ? "读写" : "只读"}${p.allow_tasks === undefined ? "" : p.allow_tasks
            ? " · 所在电脑允许运行程序" : " · 所在电脑未允许运行程序"}</small>` : ""}</li>`).join("")}</ul>
        <h3>现有助手连接</h3><ul>${(g.participant_grant_ids || []).map((id) => `<li>${E(connectionName(id))}</li>`).join("")}</ul>
        <p>最多 ${E(budget.max_work_items)} 个工作项、${E(budget.max_steps)} 个操作步骤、${E(budget.max_messages)} 条协调消息；
          每项最多尝试 ${E(budget.max_attempts)} 次，领取有效期 ${E(budget.lease_seconds)} 秒。</p>
        <section class="cc-goal-boundary"><h3>执行边界</h3>
          <p>此确认允许上述现有连接在这个目标内使用已有能力。不会扩大连接权限；关键操作仍按具体任务与平台要求审批。</p>
          ${(g.capabilities || []).includes("execute") ? `<p><strong>程序（cli_run）以项目所在电脑的账户权限运行，不是操作系统级的项目沙箱。</strong>
            项目路径限制不能代表程序的实际文件访问边界。</p>` : ""}
          <p>自动交接需要独立的目标工作事件原生订阅。普通消息提醒、旧只读任务订阅和收件回执都不会启用目标执行，也不证明助手在线或独立聊天身份。</p></section></div>
        ${canApprove() && g.can_approve !== false
          ? `<form id="cc-goal-approve" class="cc-form" data-id="${E(g.id)}" data-version="${E(g.version)}" data-digest="${E(g.digest)}">
            <label><input type="checkbox" name="confirm" required> 我以房主身份确认此版本、项目、连接、能力、有效期与预算</label>
            <div class="cc-actions"><button type="submit" class="primary">确认启用此目标执行</button>${button("close-drawer", "关闭审阅")}
              <p class="cc-hint">提交确认后，关闭窗口不会撤销已提交的请求。</p></div></form>`
          : empty("仅房主可确认启用执行。")}`;
    }

    function safeLink(url, label) {
      if (typeof url !== "string" || !url) return "";
      try {
        const parsed = new URL(url, location.origin);
        if (!["https:", "http:"].includes(parsed.protocol) || parsed.username || parsed.password) return "";
        return `<a class="cc-result-link" href="${E(parsed.href)}" target="_blank" rel="noopener noreferrer">${E(label || "查看记录")}</a>`;
      } catch {
        return "";
      }
    }

    function operationLine(op) {
      return `${E(op.tool || "操作")} · ${E(operationStates[op.state] || status(op.state))} · ${E(String(op.operation_id || op.id).slice(-8))}`;
    }

    // Whether a connection currently holds every capability on the project (unknown until options load: allow).
    const capable = (id, project, needed) => {
      const p = connection(id);
      if (!options || !p) return !options;
      return needed.every((cap) => (p.project_capabilities?.[project] || []).includes(cap));
    };

    function assignForm(g, w) {
      const others = (g.participant_grant_ids || []).filter((id) => id !== w.assignee_grant_id
        && capable(id, w.project_id, w.required_capabilities || ["read"]));
      if (!others.length) return "";
      return `<form class="cc-goal-assign cc-form cc-inline-form" data-goal="${E(g.id)}" data-work="${E(w.id)}" data-version="${E(w.version)}">
        ${field("改派给另一参与连接", `<select name="assignee_grant_id" required><option value="">选择连接</option>${others
          .map((id) => `<option value="${E(id)}">${E(connectionName(id))}</option>`).join("")}</select>`)}
        <button type="submit" class="small">改派这个工作项</button></form>`;
    }

    function workCard(w, all, operations, g) {
      const related = operations.filter((o) => o.work_item_id === w.id || o.work_id === w.id
        || (w.operation_ids || w.result?.operation_ids || []).includes(o.id || o.operation_id));
      const results = w.result || {};
      const refs = results.artifacts || results.artifact_refs || [];
      return `<article class="cc-work-card" data-work-id="${E(w.id)}"><div class="cc-row"><h3>${E(w.objective)}</h3>${badge(w.state)}</div>
        <p>${E(w.acceptance)}</p>
        <small>${E(projectLabel(w.project_id))} · ${E(connectionName(w.assignee_grant_id))}${w.responsibility ? ` · 职责：${E(w.responsibility === "coordinator" ? "首位协调" : w.responsibility)}` : ""}
          · 所需能力：${E((w.required_capabilities || ["read"]).map((c) => capNames[c] || c).join("、"))} · 第 ${E(w.attempt || 0)} 次领取</small>
        <p>依赖：${(w.dependencies || []).map((id) => E(all.find((v) => v.id === id)?.objective || id)).join("、") || "无"}</p>
        ${w.lease_until ? `<p>当前领取有效至 ${E(when(w.lease_until))}</p>` : ""}
        ${w.reason_code ? `<p class="cc-hint">原因：${E(workReasons[w.reason_code] || w.reason_code)}</p>` : ""}
        ${g && w.approval_id && g.approval_id && w.approval_id !== g.approval_id ? '<p class="cc-hint">属于此前的批准版本，不再接受执行。</p>' : ""}
        <section class="cc-work-operations"><h4>实际操作</h4>${related.map((op) => `<details><summary>${operationLine(op)}</summary>
          <p>操作 ID：${E(op.operation_id || op.id)}</p>${op.summary ? `<p class="cc-prose">${E(String(op.summary).slice(0, 300))}${String(op.summary).length > 300 ? "…" : ""}</p>` : ""}
          ${op.error_code ? `<p class="cc-hint">错误代码：${E(op.error_code)}</p>` : ""}
          ${button("goal-operation", "查看实际操作记录", { id: op.operation_id || op.id }, "", "small")}${safeLink(op.url || op.operation_url, "打开记录链接")}</details>`).join("")
          || empty("暂无实际操作记录。规划与领取状态不等于执行完成。")}</section>
        ${results.summary ? `<section class="cc-work-result"><h4>提交结果 · ${E(status(results.outcome))}</h4><p class="cc-prose">${E(results.summary)}</p>
          <p class="cc-hint">${results.execution_verified ? "结果关联了本次尝试的真实操作" : "结果没有关联任何操作：只是讨论或规划，不代表已经执行"}
            · 尚未经房主验收</p>
          ${refs.map((r) => (typeof r === "string" ? safeLink(r, "查看结果附件") : safeLink(r.url, r.label || r.name || "查看结果附件"))).join("")}</section>` : ""}
        ${(results.limitations || []).map((v) => `<p class="cc-hint">${E(v)}</p>`).join("")}
        ${g && canApprove() && g.state === "active" && w.state === "queued" && w.approval_id === g.approval_id ? assignForm(g, w) : ""}</article>`;
    }

    function subscription(g) {
      if (g.state !== "active" || !options?.work_event) return "";
      const filters = {
        project_id: g.anchor_project_id || state.project,
        environment_id: g.environment_id || state.environment,
        conversation_id: g.conversation_id,
        goal_id: g.id,
        approval_id: g.approval_id,
      };
      const instruction = `请使用当前聊天已有的 WebCoder 助手连接，只为此已批准目标创建原生事件订阅。事件：${options.work_event}；参数：${JSON.stringify(filters)}。`
        + "须使用当前宿主实际提供的订阅能力、回调与签名材料并完成其原生确认，不新建凭据或扩大权限。"
        + "普通消息与旧只读任务订阅不能替代此订阅。若宿主不支持，请说明具体缺失能力，不声称已经接通。"
        + "订阅后先读取目标并核对现有待领取工作，批准前发出的通知可能早于订阅。收到事件后重新读取目标与工作项，按当前版本、所需能力和领取租约执行；关键操作仍遵守任务和平台审批。"
        + "此订阅只绑定上述 approval_id，修订重新批准后需要对应新批准的原生订阅。";
      return `<details class="cc-goal-subscription"><summary>连接原生目标工作通知</summary>
        <p class="cc-hint">分别发送到所选助手连接对应的原聊天。页面不会自动订阅，订阅有效也不代表助手在线。</p>
        ${field("目标工作订阅指令", `<textarea rows="5" readonly class="cc-goal-subscription-text">${E(instruction)}</textarea>`)}
        ${button("goal-copy-subscription", "复制订阅指令")}</details>`;
    }

    function workForm(g, work) {
      const current = work.filter((w) => w.approval_id === g.approval_id);
      return `<details class="cc-goal-work-new"><summary>房主添加工作项</summary>
        <form id="cc-goal-work" class="cc-form" data-id="${E(g.id)}">
          ${field("工作目标", '<textarea name="objective" rows="2" maxlength="4000" required></textarea>')}
          ${field("完成标准", '<textarea name="acceptance" rows="2" maxlength="2000" required></textarea>')}
          <div class="cc-two">${field("所属项目", `<select name="target_project_id" required>${(g.project_ids || [])
            .map((id) => `<option value="${E(id)}" ${id === (g.anchor_project_id || state.project) ? "selected" : ""}>${E(projectLabel(id))}</option>`).join("")}</select>`)}
            ${field("交给", `<select name="assignee_grant_id" required><option value="">选择参与连接</option>${(g.participant_grant_ids || [])
              .map((id) => `<option value="${E(id)}">${E(connectionName(id))}</option>`).join("")}</select>`)}</div>
          <fieldset class="cc-project-choices"><legend>所需能力（不超过目标批准的上限）</legend>${choices("required_capabilities",
            (g.capabilities || ["read"]).map((c) => [c, capNames[c] || c]), ["read"])}</fieldset>
          ${current.length ? `<fieldset class="cc-project-choices"><legend>依赖的工作项（可选）</legend>${choices("dependencies",
            current.map((w) => [w.id, w.objective.slice(0, 60)]), [])}</fieldset>` : ""}
          ${field("职责说明（可选，不是权限）", '<input name="responsibility" maxlength="120">')}
          <p class="cc-hint">工作项只在本次批准范围内创建；对应连接仍需自行领取，并按所需能力重新核验。不会扩大连接授权，也不会自动开始执行。</p>
          <div class="cc-actions"><button type="submit">添加工作项</button></div></form></details>`;
    }

    function detailMarkup(record) {
      const g = record.goal;
      const work = record.work_items || [];
      const operations = record.operations || [];
      return `<div class="cc-goal-detail" data-goal-id="${E(g.id)}"><div class="cc-row">${badge(g.state)}<small>v${E(g.version)}</small></div>
        <h3>${E(g.objective)}</h3><p>${E(g.acceptance)}</p>
        <p class="cc-hint">${E((g.project_ids || []).map(projectLabel).join(" · "))}。新加入房间的项目不属于此目标。</p>
        <p class="cc-hint">已用：${usage(g)}${g.state === "active" && g.expires_at ? ` · 执行有效至 ${E(when(g.expires_at))}` : ""}</p>
        <div class="cc-actions">${canApprove() && ["proposed", "paused"].includes(g.state) ? button("goal-review", "审阅执行范围", g) : ""}
          ${canManage() && !["cancelled", "expired"].includes(g.state) ? button("goal-edit", "修订规划", g) : ""}
          ${canApprove() && g.state === "active" ? button("goal-pause", "暂停目标", g) : ""}
          ${canApprove() && !["cancelled", "expired"].includes(g.state) ? button("goal-cancel-review", "取消目标…", g, "", "danger") : ""}</div>
        <p class="cc-hint">${g.state === "active" ? "已批准范围不保证助手在线。实际领取、操作与结果分别记录。"
          : "尚不接受新的目标执行；普通讨论和规划可以继续。"}</p>
        ${subscription(g)}
        <div class="cc-row"><h3>步骤与结果</h3>${button("goal-detail", "刷新步骤与结果", g, "", "small")}</div>
        ${work.map((w) => workCard(w, work, operations, g)).join("") || empty("还没有工作项。助手提出的拆解、依赖与交接会显示在这里。")}
        ${canApprove() && g.state === "active" ? workForm(g, work) : ""}
        <h3>目标内协调记录</h3>
        ${(record.messages || []).map((m) => `<article class="cc-goal-message"><small>${E(m.author_kind === "owner" || m.author_grant_id === "owner"
          ? "房主" : connectionName(m.author_grant_id || m.grant_id))} · ${E(when(m.created || m.created_at))}${(m.mention_grant_ids || []).length
          ? ` · 提及 ${E(m.mention_grant_ids.map(connectionName).join("、"))}` : ""}</small>
          <p class="cc-prose">${E(m.body_text || m.text || m.body?.text || "")}</p></article>`).join("")
          || empty("暂无目标内交接或复核消息。普通房间消息不会递归触发执行。")}
        ${canApprove() && g.state === "active" ? `<form id="cc-goal-message" class="cc-form" data-id="${E(g.id)}">
          ${field("目标内补充说明", '<textarea name="body_text" rows="2" maxlength="4000" required></textarea>')}
          <fieldset class="cc-project-choices"><legend>@一个助手连接（可选）</legend>${choices("mention_grant_ids",
            (g.participant_grant_ids || []).map((id) => [id, connectionName(id)]), [])}</fieldset>
          <p class="cc-hint">仅在此已批准目标内交接；需要单独工作事件订阅，不复用普通提醒。</p>
          <button type="submit">发送目标内消息</button></form>` : ""}</div>`;
    }

    async function readDetail(id) {
      const dismissed = state.drawerEpoch;
      const t = token();
      const record = await getRecord("coordination_goal", id, "", goalScope(id));
      if (!current(t) || dismissed !== state.drawerEpoch) return null;
      detail = record;
      return record;
    }

    function operationMarkup(record, op) {
      return `<p><span class="cc-status">${E(operationStates[op.state] || status(op.state))}</span></p><dl class="cc-facts"><div><dt>操作 ID</dt><dd>${E(op.operation_id || op.id)}</dd></div>
          <div><dt>工具</dt><dd>${E(op.tool)}</dd></div><div><dt>所属项目</dt><dd>${E(projectLabel(op.project_id))}</dd></div>
          <div><dt>尝试</dt><dd>第 ${E(op.attempt ?? "—")} 次</dd></div></dl>
        ${op.error_code ? `<p class="cc-goal-gate">错误代码：${E(op.error_code)}</p>` : ""}
        ${op.cancel_requested ? `<p class="cc-hint">已请求取消。${E(cancelNotes[op.cancel_note] || op.cancel_note || "")}</p>`
          : op.cancel_note ? `<p class="cc-hint">${E(cancelNotes[op.cancel_note] || op.cancel_note)}</p>` : ""}
        ${["queued", "running", "cancelling", "unknown"].includes(op.state) ? '<p class="cc-hint">操作结果尚未确定：不要当作已完成，也不要重复执行同一步骤。</p>' : ""}
        <h3>Hub 记录的输出摘要</h3><pre class="cc-json">${E(op.summary || "暂无输出")}</pre>
        <p class="cc-hint">Hub 只保存每个步骤输出的前 2000 个字符；完整输出已返回给执行这一步的助手连接。</p>
        ${button("goal-detail", "返回目标步骤", record.goal)}`;
    }

    async function act(actionName, element) {
      if (!actionName.startsWith("goal-")) return null;
      if (!enabled()) throw new Error("当前服务未启用目标协作能力。");
      if (actionName === "goal-copy-subscription") {
        const area = element.closest(".cc-goal-subscription").querySelector("textarea");
        area.focus();
        area.select();
        area.setSelectionRange(0, area.value.length);
        try {
          if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(area.value);
            return local("已复制目标工作订阅指令，请在对应原聊天完成原生确认。");
          }
        } catch {
          // The selection stays: the system copy command still works.
        }
        return local("已选中目标工作订阅指令，请使用系统复制。");
      }
      if (actionName === "goal-operation") {
        // WebCoder keeps each step's state and the first 2000 characters of its output with the goal record.
        const record = detail;
        const id = element.dataset.id;
        const op = record?.operations?.find((o) => (o.operation_id || o.id) === id);
        if (!op) throw new Error("此操作不属于当前目标记录，请重新读取。");
        showDrawer("实际操作记录", operationMarkup(record, op), element);
        return local();
      }
      if (actionName === "goal-new") {
        showDrawer("创建协作目标", empty("正在核对已有助手连接…"), element);
        const t = token();
        const dismissed = state.drawerEpoch;
        await load();
        if (!current(t) || dismissed !== state.drawerEpoch) return local();
        if (!canManage()) throw new Error("当前没有目标管理权限。");
        showDrawer("创建协作目标", draftForm(), element);
        return local();
      }
      if (["goal-detail", "goal-review", "goal-edit", "goal-cancel-review"].includes(actionName)) {
        showDrawer("读取目标…", empty("正在核对当前目标…"), element);
        const record = await readDetail(element.dataset.id);
        if (!record) return local();
        const g = record.goal;
        if (actionName === "goal-review") {
          showDrawer("房主确认执行范围", review(g), element);
        } else if (actionName === "goal-edit") {
          showDrawer("修订目标规划", `<p class="cc-goal-boundary">保存修订会停止旧版本接受新执行，必须重新批准。</p>${draftForm(g)}`, element);
        } else if (actionName === "goal-cancel-review") {
          showDrawer("取消这个协作目标", `<p>停止本目标接受新工作。已发出的实际操作可能仍在运行，需以其状态为准。</p>
            <form id="cc-goal-cancel" class="cc-form" data-id="${E(g.id)}" data-version="${E(g.version)}">
            <label><input type="checkbox" name="confirm" required> 确认取消“${E(g.objective)}”</label>
            <div class="cc-actions"><button type="submit" class="danger-solid">确认取消目标</button>${button("close-drawer", "返回")}</div></form>`, element);
        } else {
          showDrawer("目标、步骤与结果", detailMarkup(record), element);
        }
        return local();
      }
      if (actionName === "goal-pause") {
        const t = token();
        await mutateGoal("goal-control", { goal_id: element.dataset.id, expected_version: Number(element.dataset.version), action: "pause" });
        if (!current(t)) return local();
        detail = null;
        state.notes = "目标已暂停。现有实际操作是否停止，请核对操作记录。";
        state.error = false;
        await rerender();
        return local(state.notes);
      }
      return local();
    }

    async function showDetailAgain(goalId, message, trigger) {
      const record = await readDetail(goalId);
      if (record) showDrawer("目标、步骤与结果", detailMarkup(record), trigger);
      return local(message);
    }

    async function submit(form) {
      if (!form.id.startsWith("cc-goal-") && !form.classList.contains("cc-goal-assign")) return null;
      const t = token();
      try {
        if (form.id === "cc-goal-work") {
          const f = new FormData(form);
          const required = f.getAll("required_capabilities");
          if (!required.length) throw new Error("请至少选择一项所需能力。");
          if (!capable(f.get("assignee_grant_id"), f.get("target_project_id"), required)) {
            throw new Error(`${connectionName(f.get("assignee_grant_id"))} 当前没有这个项目的所需能力：${required.map((c) => capNames[c] || c).join("、")}。`);
          }
          await mutateGoal("work-create", {
            goal_id: form.dataset.id,
            objective: String(f.get("objective")).trim(),
            acceptance: String(f.get("acceptance")).trim(),
            target_project_id: f.get("target_project_id"),
            assignee_grant_id: f.get("assignee_grant_id"),
            required_capabilities: required,
            dependencies: f.getAll("dependencies"),
            responsibility: String(f.get("responsibility") || "").trim(),
          });
          if (!current(t)) return local();
          return showDetailAgain(form.dataset.id, "工作项已排队，等待对应连接领取；排队不代表执行。", form);
        }
        if (form.classList.contains("cc-goal-assign")) {
          await mutateGoal("work-assign", {
            goal_id: form.dataset.goal,
            work_item_id: form.dataset.work,
            assignee_grant_id: new FormData(form).get("assignee_grant_id"),
            expected_version: Number(form.dataset.version),
          });
          if (!current(t)) return local();
          return showDetailAgain(form.dataset.goal, "工作项已改派；新连接仍需自行领取。", form);
        }
        if (form.id === "cc-goal-draft") {
          const payload = draftPayload(form);
          if (validateDraft(form, payload).length) throw new Error("请核对所选连接的项目与能力。");
          await mutateGoal(form.dataset.id ? "goal-update" : "goal-create", {
            ...payload,
            ...(form.dataset.id ? { goal_id: form.dataset.id, expected_version: Number(form.dataset.version) } : {}),
          });
        } else if (form.id === "cc-goal-message") {
          const f = new FormData(form);
          await mutateGoal("goal-message", {
            goal_id: form.dataset.id,
            body_text: f.get("body_text"),
            mention_grant_ids: f.getAll("mention_grant_ids"),
            input_work_item_ids: [],
          });
          if (!current(t)) return local();
          return showDetailAgain(form.dataset.id, "目标内消息已保存，不代表助手已读取或执行。", form);
        } else {
          if (!form.elements.confirm?.checked) throw new Error("请先明确确认此版本的范围。");
          if (!canApprove()) throw new Error("仅房主可确认执行或取消目标。");
          await mutateGoal(form.id === "cc-goal-approve" ? "goal-approve" : "goal-control", {
            goal_id: form.dataset.id,
            expected_version: Number(form.dataset.version),
            ...(form.id === "cc-goal-approve" ? { digest: form.dataset.digest } : { action: "cancel" }),
          });
        }
        if (!current(t)) return local();
        // Keep the review visible until the single replacement render commits.
        // Closing it before a second fetch exposes actionable stale controls.
        detail = null;
        state.notes = form.id === "cc-goal-draft" ? "目标规划已保存，执行尚未启用。"
          : form.id === "cc-goal-approve" ? "此目标版本已批准，首个只读协调工作项已排队。请连接本批准版本的原生工作通知；排队不代表执行。"
            : "目标已取消。";
        state.error = false;
        await rerender();
        return local(state.notes);
      } catch (error) {
        if (error.stale || !current(t)) return local();
        if (form.elements.confirm) form.elements.confirm.checked = false;
        if ([401, 403].includes(error.status)) privateClear();
        if (error.status === 409 || ["STALE_VERSION", "VERSION_CONFLICT"].includes(error.code)) {
          detail = null;
          state.notes = `${error.message}。目标或连接权限已改变，请重新审阅当前版本；未按旧版本执行。`;
          state.error = true;
          await rerender();
          return local(state.notes);
        }
        throw error;
      }
    }

    function change(event) {
      const form = event.target.form;
      if (form?.id !== "cc-goal-draft") return;
      if (event.target.name === "participant_grant_ids") {
        const selected = new FormData(form).getAll("participant_grant_ids");
        const picker = form.elements.coordinator_grant_id;
        const chosen = selected.includes(picker.value) ? picker.value : selected[0];
        picker.innerHTML = `<option value="">先选择参与连接</option>${selected.map((id) =>
          `<option value="${E(id)}"${id === chosen ? " selected" : ""}>${E(connectionName(id))}</option>`).join("")}`;
      }
      validateDraft(form, draftPayload(form));
    }

    return { clear, load, action, context, list, act, submit, change, enabled };
  },
};
