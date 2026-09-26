#!/usr/bin/env bash
# WebCoder - agent installer for macOS and Linux.
# Creates a Python virtual environment in ~/.webcoder-agent, installs the agent from the hub, pairs this
# computer, sets the folders the hub may access and (unless --no-autostart) starts the agent at login
# (launchd on macOS, a systemd user service on Linux). No root rights are needed.
#
#   curl -fsSL https://hub.example.com/downloads/install-agent.sh | bash -s -- \
#        --hub https://hub.example.com --code ABCD-EFGH --root /home/me/projects
set -euo pipefail
# A PYTHONHOME/PYTHONPATH left by another tool makes Python load a foreign standard library.
unset PYTHONHOME PYTHONPATH

HUB="" CODE="" READ_ONLY=0 AUTOSTART=1 MOBILE=0
ROOTS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --hub) HUB="${2%/}"; shift 2 ;;
    --code) CODE="$2"; shift 2 ;;
    --root) ROOTS+=("$2"); shift 2 ;;
    --read-only) READ_ONLY=1; shift ;;
    --no-autostart) AUTOSTART=0; shift ;;
    --mobile) MOBILE=1; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done
if [ -z "$HUB" ] || [ -z "$CODE" ]; then
  echo "用法：install-agent.sh --hub <地址> --code <配对码> [--root <文件夹>]... [--read-only] [--no-autostart] [--mobile]" >&2
  exit 2
fi

AGENT_HOME="$HOME/.webcoder-agent"
VENV="$AGENT_HOME/venv"
PY="$VENV/bin/python"
CONFIG="$AGENT_HOME/config.json"  # passed explicitly: the login service must not depend on the environment
LOG="$AGENT_HOME/agent.log"
agent() { "$PY" -m webcoder.agent --config "$CONFIG" "$@" </dev/null; }  # stdin is this script (curl | bash)
step() { printf '\033[36m==> %s\033[0m\n' "$*"; }

step "查找 Python 3.10+"
BASE=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    BASE="$candidate"; break
  fi
done
if [ -z "$BASE" ]; then
  echo "需要 Python 3.10+（macOS：brew install python；Debian/Ubuntu：sudo apt install python3 python3-venv）" >&2
  exit 1
fi

mkdir -p "$AGENT_HOME"
chmod 700 "$AGENT_HOME"  # holds the device secret, edit backups and logs
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }  # for the launchd plist
# Stop the running agent before upgrading it; if this installation fails, it is started again on exit.
RESTART_PREVIOUS=""
if [ "$(uname -s)" = "Darwin" ]; then
  if launchctl print "gui/$(id -u)/com.webcoder.agent" >/dev/null 2>&1; then RESTART_PREVIOUS=launchd; fi
  launchctl bootout "gui/$(id -u)/com.webcoder.agent" >/dev/null 2>&1 || true
elif command -v systemctl >/dev/null 2>&1; then
  if systemctl --user is-active --quiet webcoder-agent 2>/dev/null; then RESTART_PREVIOUS=systemd; fi
  systemctl --user stop webcoder-agent >/dev/null 2>&1 || true
fi
# An agent started by hand (or by the no-systemd fallback) would otherwise keep running next to the new one.
# Only this installation's: run with its config, or from its venv with no --config (the default); an agent run on
# purpose with another --config is another device and keeps running.
ours() {
  for pid in $(pgrep -f -- "-m webcoder[.]agent" 2>/dev/null); do
    case "$(ps -o args= -p "$pid" 2>/dev/null)" in
      *"--config $CONFIG"*) echo "$pid" ;;
      *--config*) ;;
      *"$VENV/bin/python"*) echo "$pid" ;;
    esac
  done
}
if [ -z "$RESTART_PREVIOUS" ] && [ -n "$(ours)" ]; then
  RESTART_PREVIOUS=manual
fi
for pid in $(ours); do kill "$pid" 2>/dev/null || true; done
restore_previous() {
  case "$RESTART_PREVIOUS" in
    launchd) launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.webcoder.agent.plist" >/dev/null 2>&1 || true ;;
    systemd) systemctl --user start webcoder-agent >/dev/null 2>&1 || true ;;
    manual) nohup "$PY" -m webcoder.agent --config "$CONFIG" run --log-file "$LOG" </dev/null >/dev/null 2>&1 & ;;
  esac
}
trap restore_previous EXIT

if [ ! -x "$PY" ]; then
  step "创建独立的 Python 虚拟环境：$VENV"
  "$BASE" -m venv "$VENV" </dev/null || { echo "创建虚拟环境失败（Debian/Ubuntu 需要：sudo apt install python3-venv）" >&2; exit 1; }
fi

step "安装 WebCoder Agent"
"$PY" -m pip install --disable-pip-version-check --quiet --upgrade "$HUB/downloads/webcoder-agent.zip" </dev/null

# `webcoder` (tasks for ChatGPT on the web, from a project folder) on PATH, without the venv's python with it.
if [ -x "$VENV/bin/webcoder" ]; then
  mkdir -p "$HOME/.local/bin" && ln -sf "$VENV/bin/webcoder" "$HOME/.local/bin/webcoder"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "提示：把 ~/.local/bin 加入 PATH 后就能在任意文件夹运行 webcoder" ;;
  esac
fi

step "配对这台电脑"
paired=$(agent pair --hub "$HUB" --code "$CODE")
printf '%s\n' "${paired%%$'\n'*}"  # the rest are hints for manual setups; this script does those steps

# Always applied, even when empty: reinstalling with fewer folders must take the others away.
if [ ${#ROOTS[@]} -gt 0 ]; then
  step "设置允许访问的文件夹：${ROOTS[*]}"
else
  step "不开放任何文件夹（之后可在面板「修改设置」里添加）"
fi
agent roots set --create ${ROOTS[@]+"${ROOTS[@]}"} >/dev/null  # this form survives set -u on bash 3.2 (macOS)
if [ "$READ_ONLY" = 1 ]; then
  agent read-only on >/dev/null
else
  agent read-only off >/dev/null
fi
if [ "$MOBILE" = 1 ]; then
  step "允许控制连接到这台电脑的手机/模拟器（adb / hdc）"
  agent mobile on >/dev/null
else
  agent mobile off >/dev/null
fi

if [ "$AUTOSTART" = 0 ]; then
  RESTART_PREVIOUS=""
  # Remove autostart left over from an earlier install.
  if [ "$(uname -s)" = "Darwin" ]; then
    rm -f "$HOME/Library/LaunchAgents/com.webcoder.agent.plist"
  elif command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable webcoder-agent >/dev/null 2>&1 || true
  fi
  printf '\n安装完成。需要时用这条命令启动 Agent：%s -m webcoder.agent --config %s run\n' "$PY" "$CONFIG"
  exit 0
fi
LOG_START=0
[ -f "$LOG" ] && LOG_START=$(wc -c < "$LOG" | tr -d ' ')  # only look at what the new run writes

case "$(uname -s)" in
  Darwin)
    step "设置登录后自动运行（launchd）"
    plist="$HOME/Library/LaunchAgents/com.webcoder.agent.plist"
    mkdir -p "$(dirname "$plist")"
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.webcoder.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml "$PY")</string><string>-m</string><string>webcoder.agent</string>
    <string>--config</string><string>$(xml "$CONFIG")</string><string>run</string>
    <string>--log-file</string><string>$(xml "$LOG")</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$(xml "$AGENT_HOME")</string>
</dict>
</plist>
PLIST
    launchctl bootstrap "gui/$(id -u)" "$plist"
    ;;
  *)
    if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
      step "设置登录后自动运行（systemd 用户服务）"
      unit="$HOME/.config/systemd/user/webcoder-agent.service"
      mkdir -p "$(dirname "$unit")"
      cat > "$unit" <<UNIT
[Unit]
Description=WebCoder agent
After=network-online.target

[Service]
ExecStart="$PY" -m webcoder.agent --config "$CONFIG" run --log-file "$LOG"
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
      systemctl --user daemon-reload
      systemctl --user enable --now webcoder-agent
      echo "提示：希望在未登录时也运行，请执行：sudo loginctl enable-linger $USER"
    else
      step "没有可用的 systemd 用户服务，改为在后台启动（重启后需重新运行本命令）"
      nohup "$PY" -m webcoder.agent --config "$CONFIG" run --log-file "$LOG" </dev/null >/dev/null 2>&1 &
    fi
    ;;
esac
RESTART_PREVIOUS=""  # the new agent is running

for _ in $(seq 1 20); do
  # plain grep (not -q) reads to the end, so tail never dies of SIGPIPE under pipefail
  if [ -f "$LOG" ] && tail -c "+$((LOG_START + 1))" "$LOG" | grep "connected to" >/dev/null; then
    printf '\n\033[32m完成：Agent 已在后台运行并连上 Hub。日志：%s\033[0m\n' "$LOG"
    exit 0
  fi
  sleep 1
done
printf '\n\033[33mAgent 已在后台启动，但暂未确认连接成功，请查看日志：%s\033[0m\n' "$LOG"
