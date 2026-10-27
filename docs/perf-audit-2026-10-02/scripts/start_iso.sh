#!/bin/zsh
# usage: start_iso.sh MODE TRACEFILE
cd /private/tmp/claude-501/-Users-adijain-Desktop-MOE-Project/5e4ba776-e4b5-406d-82b3-56ab928e76bf/scratchpad
export MODE=$1 TRACE=$2 WISP_HOME=$PWD/iso_home WISP_BACKEND_URL=http://127.0.0.1:8775
rm -f $TRACE
nohup /Users/adijain/Desktop/MOE_Project/.venv/bin/python run_iso.py > iso_backend_$1.log 2>&1 &
echo $! > iso.pid
for i in {1..60}; do curl -sf http://127.0.0.1:8775/mode >/dev/null 2>&1 && { echo ready after $i; break; }; sleep 0.5; done
