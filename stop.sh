#!/data/data/com.termux/files/usr/bin/bash
cd "$(dirname "$0")"

if [ -f bridge.pid ]; then
    kill "$(cat bridge.pid)" 2>/dev/null
    rm -f bridge.pid
    echo "Bridge 已停止"
else
    echo "Bridge 未运行"
fi
