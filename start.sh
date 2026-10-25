#!/data/data/com.termux/files/usr/bin/bash
cd "$(dirname "$0")"

if [ -f bridge.pid ] && kill -0 "$(cat bridge.pid)" 2>/dev/null; then
    echo "Bridge 已经在运行"
    exit 0
fi

mkdir -p logs
nohup python bridge.py >> logs/bridge.log 2>&1 &
echo $! > bridge.pid

echo "Bridge 已启动，PID: $(cat bridge.pid)"
