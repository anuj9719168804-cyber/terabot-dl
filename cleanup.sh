#!/bin/bash
# Cleanup: remove downloaded videos older than 1 hour, truncate log if >10MB, warn if disk >80%
cd /home/agentuser/terabox-bot
find storage -type f -mmin +60 -delete 2>/dev/null
LOGSIZE=$(stat -c%s bot.log 2>/dev/null || echo 0)
if [ "$LOGSIZE" -gt 10485760 ]; then tail -c 1048576 bot.log > bot.tmp && mv bot.tmp bot.log; fi
USE=$(df / --output=pcent | tail -1 | tr -dc '0-9')
if [ "$USE" -gt 80 ]; then echo "WARNING: disk usage ${USE}%" >&2; fi
