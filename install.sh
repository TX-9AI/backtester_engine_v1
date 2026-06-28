#!/bin/bash
# =============================================================================
# install.sh — backtester_engine_v1
# v1.0 — 2026-06-28 — EC2 web installer, mirrors crypto_trader install.sh pattern
# v1.1 — 2026-06-28 — Correct repo URL (backtester_engine_v1), .gitignore compliance
#
# Run on a fresh EC2:
#   curl -fsSL https://raw.githubusercontent.com/TX-9AI/backtester_engine_v1/main/install.sh -o install.sh && bash install.sh
# =============================================================================

export DEBIAN_FRONTEND=noninteractive
export TERM=xterm-256color

REPO="https://github.com/TX-9AI/backtester_engine_v1.git"
INSTALL_DIR="$HOME/btc-backtester"
DEPLOY_DIR="$HOME/btc-backtester-deploy"
VENV="$INSTALL_DIR/venv"
VERSION="1.0"

exec < /dev/tty

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
BOLD='\033[1m'; RESET='\033[0m'

print_step() { echo -e "\n${BOLD}${GREEN}[ $1 ]${RESET} $2"; }
print_ok()   { echo -e "  ${GREEN}✓${RESET}  $1"; }
print_info() { echo -e "  ${CYAN}→${RESET}  $1"; }
print_warn() { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
ask()        { read -rp "    $1: " "$2"; }

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║     backtester_engine_v1  |  Vertigo Capital      ║${RESET}"
echo -e "${BOLD}${CYAN}║     BTC/USD  |  Kraken Historical  |  Full Suite    ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Have ready:"
echo "    - GitHub Personal Access Token (to push results)"
echo "    - Optional: crypto_trader_v6 strategy files (uploaded to repo)"
echo ""
read -rp "  Press ENTER to continue or Ctrl+C to cancel..."

# ─── STEP 1: STARTING BALANCE ────────────────────────────────────────────────
print_step "1/7" "Session Starting Balance"
echo ""
echo -e "  This sets the default starting balance for backtest runs."
echo -e "  You can change it at the start of each session interactively."
echo ""
printf "    Default starting balance USD [1000]: "; read -r BALANCE_INPUT
BALANCE_INPUT="${BALANCE_INPUT:-1000}"
if ! echo "$BALANCE_INPUT" | grep -qE '^[0-9]+(\.[0-9]+)?$'; then
    print_warn "Invalid — using default 1000"
    BALANCE_INPUT="1000"
fi
BUYING_POWER=$(echo "$BALANCE_INPUT * 10" | bc)
print_ok "Default balance: \$${BALANCE_INPUT} → \$${BUYING_POWER} buying power (10x)"

# ─── STEP 2: GITHUB REPO & TOKEN ─────────────────────────────────────────────
print_step "2/7" "GitHub Repository (optional — for pushing results)"
echo ""
GITHUB_REPO=""
GITHUB_TOKEN=""
printf "    GitHub repo [ENTER to skip, e.g. TX-9AI/backtester_engine_v1]: "; read -r GITHUB_REPO

if [[ -n "$GITHUB_REPO" ]]; then
    echo ""
    read -rsp "    GitHub Personal Access Token (paste, ENTER): " GITHUB_TOKEN; echo ""
    print_ok "GitHub repo: https://github.com/${GITHUB_REPO}"
    print_ok "GitHub token accepted."
else
    print_ok "Skipping GitHub — push.sh will prompt for token when needed."
fi

# ─── STEP 3: SYSTEM PACKAGES ─────────────────────────────────────────────────
print_step "3/7" "System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv python-is-python3 \
    git rsync bc sqlite3 curl wget
print_ok "System packages ready."

# ─── STEP 4: CLONE & INSTALL FILES ───────────────────────────────────────────
print_step "4/7" "Installing backtester files"

if [ -d "$DEPLOY_DIR/.git" ]; then
    echo "  Updating existing repo..."
    cd "$DEPLOY_DIR" && git pull -q
else
    echo "  Cloning repository..."
    git clone -q "$REPO" "$DEPLOY_DIR"
fi

mkdir -p "$INSTALL_DIR"
rsync -a \
    --exclude='.git' \
    --exclude='*.pem' \
    --exclude='*.bat' \
    --exclude='venv' \
    --exclude='data/cache' \
    --exclude='data/backtest_results.db' \
    --exclude='reports/output' \
    --exclude='__pycache__' \
    "$DEPLOY_DIR/" "$INSTALL_DIR/"

# Create runtime dirs that are excluded from rsync
mkdir -p "$INSTALL_DIR/data/cache"
mkdir -p "$INSTALL_DIR/reports/output"

chmod +x "$INSTALL_DIR/push.sh" 2>/dev/null || true

for f in main.py config.py backtest/data_fetcher.py backtest/replay.py; do
    [ -f "$INSTALL_DIR/$f" ] || { echo "ERROR: $f missing. Aborting."; exit 1; }
done
print_ok "Files installed to ${INSTALL_DIR}"

# ─── STEP 5: PYTHON ENVIRONMENT ──────────────────────────────────────────────
print_step "5/7" "Python environment"
python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install --upgrade pip -q
pip install ccxt pandas numpy matplotlib plotly reportlab scipy requests -q
print_ok "Dependencies installed."

grep -q "btc-backtester/venv" ~/.bashrc || echo "source $VENV/bin/activate" >> ~/.bashrc
grep -q "cd ~/btc-backtester"  ~/.bashrc || echo "cd $INSTALL_DIR"           >> ~/.bashrc

# ─── STEP 6: WRITE SESSION CONFIG ────────────────────────────────────────────
print_step "6/7" "Writing session config"

# Patch default balance into config.py
sed -i "s/^DEFAULT_STARTING_BALANCE.*/DEFAULT_STARTING_BALANCE  = ${BALANCE_INPUT}/" \
    "$INSTALL_DIR/config.py"

print_ok "config.py updated — default balance \$${BALANCE_INPUT}"

# ─── STEP 7: GIT INIT ────────────────────────────────────────────────────────
print_step "7/7" "Git setup"

cd "$INSTALL_DIR"
if [ ! -d ".git" ]; then
    git init -q
    git branch -M main 2>/dev/null || true
    if [[ -n "$GITHUB_REPO" ]]; then
        git remote add origin "https://github.com/${GITHUB_REPO}.git"
        print_ok "Git repo initialized — push.sh ready to use"
    else
        print_ok "Git initialized — add remote manually when ready"
    fi
fi

# ── Final status ──────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║        ✅  Setup Complete — Ready to Backtest!      ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  Default balance:  \$${BALANCE_INPUT} (10x = \$${BUYING_POWER} buying power)"
echo -e "  Install dir:      ${INSTALL_DIR}"
echo -e "  Cache dir:        ${INSTALL_DIR}/data/cache"
echo -e "  Results DB:       ${INSTALL_DIR}/data/backtest_results.db"
echo -e "  Reports:          ${INSTALL_DIR}/reports/output/"
echo ""
echo -e "  Quick start:"
echo -e "    cd ~/btc-backtester"
echo -e "    python main.py                        — interactive session"
echo -e "    python main.py --quarter 2025-Q3      — run specific quarter"
echo -e "    python main.py --all-quarters         — run full history"
echo -e "    python main.py --balance 10000        — override starting balance"
echo ""
echo -e "  After a run:"
echo -e "    python reports/html_report.py --run-id 1   — HTML report"
echo -e "    ls reports/output/                          — view generated reports"
echo ""

source "${VENV}/bin/activate"
cd "$INSTALL_DIR"

# Show available quarters
echo -e "  ${CYAN}Available quarters:${RESET}"
python -c "
from backtest.data_fetcher import DataFetcher
f = DataFetcher()
qs = f.list_available_quarters()
cached = sum(1 for q in qs if f.is_cached(q))
print(f'    {len(qs)} quarters available ({cached} cached)')
print(f'    Earliest: {qs[0]}  |  Latest: {qs[-1]}')
" 2>/dev/null || echo "    (run python main.py to see quarters)"

echo ""
exec bash
