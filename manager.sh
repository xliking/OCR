#!/bin/bash

# 脚本名称：manage_service.sh
# 功能：管理任意 Python 服务的启动、关闭和进程查看
# 用法：./manage_service.sh <python_script_name>
# 示例：./manage_service.sh check_slots.py

# 检查是否提供了 Python 文件名参数
if [ $# -ne 1 ]; then
    echo "用法: $0 <python_script_name>"
    echo "示例: $0 check_slots.py"
    exit 1
fi

# 获取 Python 文件名
PYTHON_SCRIPT="$1"
# 日志文件名基于 Python 文件名生成
LOG_FILE="${PYTHON_SCRIPT%.py}.log"

# 查找指定 Python 脚本的进程（只匹配 python3 进程）
find_process() {
    # 使用 pgrep 查找 python3 进程，精确匹配脚本名，排除脚本自身和 grep 进程
    pgrep -f "python3.*${PYTHON_SCRIPT}" -u "$(id -u)" | grep -v "$$" | grep -v "grep"
}

# 查看指定 Python 脚本的进程
view_process() {
    local pids
    pids=$(find_process)
    if [ -n "$pids" ]; then
        echo "找到 $(echo "$pids" | wc -l) 个 $PYTHON_SCRIPT 进程，进程 ID:"
        echo "$pids"
        echo "进程详细信息:"
        # 将 PID 列表转换为以空格分隔的字符串，避免多余的逗号
        pid_list=$(echo "$pids" | tr '\n' ' ' | sed 's/ $//')
        # 使用 ps 精确显示指定 PID 的进程信息
        ps -p "$pid_list" -o user,pid,%cpu,%mem,vsz,rss,tty,stat,start,time,command 2>/dev/null | grep -v "grep"
    else
        echo "未找到正在运行的 $PYTHON_SCRIPT 进程"
    fi
}

# 启动指定 Python 脚本的服务
start_service() {
    local pids
    pids=$(find_process)
    if [ -n "$pids" ]; then
        echo "已有 $(echo "$pids" | wc -l) 个 $PYTHON_SCRIPT 进程运行，进程 ID:"
        echo "$pids"
        read -p "是否关闭现有进程并重新启动？(y/n): " choice
        if [ "$choice" != "y" ] && [ "$choice" != "Y" ]; then
            echo "取消启动操作"
            return
        fi
        stop_service
    fi

    echo "正在启动 $PYTHON_SCRIPT 服务..."
    nohup python3 "$PYTHON_SCRIPT" > "$LOG_FILE" 2>&1 &
    sleep 1  # 等待服务启动
    pids=$(find_process)
    if [ -n "$pids" ]; then
        echo "服务启动成功，进程 ID:"
        echo "$pids"
    else
        echo "服务启动失败，请检查 $LOG_FILE 日志"
    fi
}

# 关闭指定 Python 脚本的服务
stop_service() {
    local pids
    pids=$(find_process)
    if [ -z "$pids" ]; then
        echo "未找到正在运行的 $PYTHON_SCRIPT 进程"
        return
    fi

    echo "找到 $(echo "$pids" | wc -l) 个 $PYTHON_SCRIPT 进程，进程 ID:"
    echo "$pids"
    for pid in $pids; do
        # 先尝试优雅终止（SIGTERM）
        kill -TERM "$pid" 2>/dev/null
        if [ $? -eq 0 ]; then
            echo "已发送终止信号给进程 $pid"
        else
            echo "无法发送终止信号给进程 $pid"
            continue
        fi
        # 等待 3 秒，如果进程仍未终止，则强制杀死（SIGKILL）
        sleep 3
        if ps -p "$pid" > /dev/null 2>&1; then
            kill -KILL "$pid" 2>/dev/null
            if [ $? -eq 0 ]; then
                echo "进程 $pid 未正常终止，已强制杀死"
            else
                echo "无法强制杀死进程 $pid"
            fi
        else
            echo "进程 $pid 已终止"
        fi
    done
    sleep 1  # 等待进程完全终止
    pids=$(find_process)
    if [ -n "$pids" ]; then
        echo "以下进程未能正常终止:"
        echo "$pids"
    else
        echo "所有进程已终止"
    fi
}

# 主菜单
main() {
    while true; do
        echo -e "\n=== $PYTHON_SCRIPT 服务管理 ==="
        echo "1. 启动服务"
        echo "2. 关闭服务"
        echo "3. 查看进程"
        echo "4. 退出"
        read -p "请选择操作 (1-4): " choice

        case $choice in
            1)
                start_service
                ;;
            2)
                stop_service
                ;;
            3)
                view_process
                ;;
            4)
                echo "退出管理脚本"
                exit 0
                ;;
            *)
                echo "无效的选择，请输入 1-4"
                ;;
        esac
    done
}

# 运行主函数
main