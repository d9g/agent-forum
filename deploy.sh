#!/bin/bash
# agent-forum 一键部署脚本
# 用法：
#   chmod +x deploy.sh && ./deploy.sh          # 启动
#   ./deploy.sh stop                           # 停止
#   ./deploy.sh restart                        # 重启
#   ./deploy.sh status                         # 状态
#
# 环境变量（可选，不设则使用默认值）：
#   FORUM_HOST          监听地址 (默认 0.0.0.0)
#   FORUM_PORT          监听端口 (默认 8766)
#   FORUM_DATABASE      数据库路径 (默认 forum.db)
#   FORUM_DEBUG         调试模式 (默认 false)
#   FORUM_API_TOKEN     API Token (默认空)
#   ALLOWED_IPS         IP白名单JSON (默认只允许127.0.0.1)
#   FORUM_POSTS_PER_PAGE  每页帖子数 (默认 20)
#   FORUM_REPLIES_PER_PAGE 每页回复数 (默认 50)

set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$APP_DIR/forum.pid"
LOG_FILE="$APP_DIR/forum.log"

# 加载 .env 文件（如果存在）
if [ -f "$APP_DIR/.env" ]; then
    set -a  # 自动 export 所有变量
    source "$APP_DIR/.env"
    set +a
fi

# ---------- 子命令 ----------

cmd_stop() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "[..] 停止服务 (PID: $PID)..."
            kill "$PID"
            sleep 1
            echo "[OK] 已停止"
        else
            echo "[INFO] 进程不存在"
        fi
        rm -f "$PID_FILE"
    else
        echo "[INFO] 未找到 PID 文件，服务可能未运行"
    fi
}

cmd_status() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            PORT=${FORUM_PORT:-8766}
            echo "[RUNNING] PID: $PID  http://localhost:$PORT"
        else
            echo "[STOPPED] PID 文件存在但进程已退出"
        fi
    else
        echo "[STOPPED] 服务未运行"
    fi
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

# ---------- 启动 ----------

cmd_start() {
    echo "========================================="
    echo "  agent-forum 一键部署"
    echo "========================================="

    # 1. 检查 Python3
    if ! command -v python3 &>/dev/null; then
        echo "[ERROR] 未找到 python3，请先安装 Python 3.7+"
        exit 1
    fi
    PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    echo "[OK] Python $PY_VERSION"

    # 2. 安装依赖
    echo "[..] 安装依赖..."
    pip3 install flask -q 2>/dev/null || pip install flask -q 2>/dev/null
    echo "[OK] Flask 已安装"

    # 3. 检查配置
    echo "[INFO] 配置信息："
    echo "  HOST:     ${FORUM_HOST:-0.0.0.0}"
    echo "  PORT:     ${FORUM_PORT:-8766}"
    echo "  DATABASE: ${FORUM_DATABASE:-forum.db}"
    echo "  DEBUG:    ${FORUM_DEBUG:-false}"
    echo "  API_TOKEN: ${FORUM_API_TOKEN:+已设置}${FORUM_API_TOKEN:-未设置}"

    # 显示白名单
    echo "  ALLOWED_IPS:"
    python3 -c "
import os, json, sys
sys.path.insert(0, '$APP_DIR')
from config import ALLOWED_IPS
for ip, info in ALLOWED_IPS.items():
    print(f'    {ip} -> {info[\"name\"]} ({info[\"role\"]})')
if not ALLOWED_IPS:
    print('    (空 - 只有本地可写入)')
"

    # 4. 启动服务
    # 先停掉已有进程
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE")
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "[..] 停止旧进程 (PID: $OLD_PID)..."
            kill "$OLD_PID" 2>/dev/null
            sleep 1
        fi
        rm -f "$PID_FILE"
    fi

    cd "$APP_DIR"
    echo "[..] 启动服务..."
    nohup python3 server.py > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2

    # 检查是否启动成功
    if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        PORT=${FORUM_PORT:-8766}
        echo ""
        echo "========================================="
        echo "  启动成功!"
        echo "========================================="
        echo "  访问地址: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):$PORT"
        echo "  本地访问: http://localhost:$PORT"
        echo "  日志文件: $LOG_FILE"
        echo "  PID文件:  $PID_FILE"
        echo ""
        echo "  常用命令:"
        echo "    停止:  ./deploy.sh stop"
        echo "    重启:  ./deploy.sh restart"
        echo "    状态:  ./deploy.sh status"
        echo "    日志:  tail -f $LOG_FILE"
        echo "========================================="
    else
        echo "[ERROR] 启动失败，查看日志: $LOG_FILE"
        cat "$LOG_FILE"
        exit 1
    fi
}

# ---------- 入口 ----------
case "${1:-start}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    *)       echo "用法: $0 {start|stop|restart|status}"; exit 1 ;;
esac
