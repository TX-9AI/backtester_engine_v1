#!/bin/bash
# research/run_strategy_tester.sh — backtester_engine_v1
# v1.0 — 2026-06-29 — Launch eth_strategy_tester.py in background tmux session

SESSION="eth-research"
LOG="$HOME/btc-backtester/research/strategy_tester.log"
SCRIPT="$HOME/btc-backtester/research/eth_strategy_tester.py"
VENV="$HOME/btc-backtester/venv/bin/activate"

# Kill existing session if present
tmux kill-session -t "$SESSION" 2>/dev/null || true

# Launch in detached tmux session
tmux new-session -d -s "$SESSION" -x 220 -y 50 \
    "source $VENV && python $SCRIPT 2>&1 | tee $LOG; echo DONE >> $LOG"

echo ""
echo "  ✓  eth_strategy_tester.py running in tmux session: $SESSION"
echo ""
echo "  To check progress:"
echo "    tail -20 $LOG"
echo ""
echo "  To check if still running:"
echo "    tmux has-session -t $SESSION 2>/dev/null && echo RUNNING || echo DONE"
echo ""
echo "  To watch live output:"
echo "    tail -f $LOG"
echo ""
echo "  To attach and see full terminal:"
echo "    tmux attach -t $SESSION"
echo ""
