#!/bin/bash
# =============================================================================
# install.sh — backtester_engine_v1
# v1.0 — 2026-06-28 — EC2 web installer, mirrors crypto_trader install.sh pattern
# v1.1 — 2026-06-28 — Correct repo URL (backtester_engine_v1), .gitignore compliance
# v1.2 — 2026-06-28 — Step 3: optional crypto_trader strategy file pull from GitHub
#                      Auto-detects latest crypto_trader_v* repo under TX-9AI
# v1.3 — 2026-06-28 — Fix: strip full URL from GITHUB_REPO input (accept slug or full URL)
#                      Fix: API auth token passed correctly to org repo search
# =============================================================================

export DEBIAN_FRONTEND=noninteractive
export TERM=xterm-256color

REPO="https://github.com/TX-9AI/backtester_engine_v1.git"
INSTALL_DIR="$HOME/btc-backtester"
DEPLOY_DIR="$HOME/btc-backtester-deploy"
VENV="$INSTALL_DIR/venv"
VERSION="1.2"
GH_ORG="TX-9AI"

exec < /dev/tty

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
BOLD='\033[1m'; RESET='\033[0m'

print_step() { echo -e "\n${BOLD}${GREEN}[ $1 ]${RESET} $2"; }
print_ok()   { echo -e "  ${GREEN}✓${RESET}  $1"; }
print_info() { echo -e "  ${CYAN}→${RESET}  $1"; }
print_warn() { echo -e "  ${YELLOW}⚠${RESET}  $1"; }

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}║     backtester_engine_v1  |  Vertigo Capital        ║${RESET}"
echo -e "${BOLD}${CYAN}║     BTC/USD  |  Kraken Historical  |  Full Suite    ║${RESET}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Have ready:"
echo "    - GitHub Personal Access Token"
echo "    - Optional: pull crypto_trader strategy files from GitHub"
echo ""
read -rp "  Press ENTER to continue or Ctrl+C to cancel..."

# ─── STEP 1: STARTING BALANCE ────────────────────────────────────────────────
print_step "1/8" "Session Starting Balance"
echo ""
echo -e "  Default starting balance for backtest sessions."
echo -e "  You can change it interactively at the start of any run."
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
print_step "2/8" "GitHub Repository"
echo ""
GITHUB_REPO=""
GITHUB_TOKEN=""
printf "    GitHub repo [e.g. TX-9AI/backtester_engine_v1, ENTER to skip]: "; read -r GITHUB_REPO

if [[ -n "$GITHUB_REPO" ]]; then
    # Strip full URL if user pasted it — keep only the slug (owner/repo)
    GITHUB_REPO="${GITHUB_REPO#https://github.com/}"
    GITHUB_REPO="${GITHUB_REPO#http://github.com/}"
    GITHUB_REPO="${GITHUB_REPO%/}"
    echo ""
    read -rsp "    GitHub Personal Access Token (paste, ENTER): " GITHUB_TOKEN; echo ""
    print_ok "GitHub repo: https://github.com/${GITHUB_REPO}"
    print_ok "GitHub token accepted."
else
    print_ok "Skipping GitHub — add remote manually when ready."
fi

# ─── STEP 3: CRYPTO_TRADER STRATEGY FILES ────────────────────────────────────
print_step "3/8" "crypto_trader Strategy Files (optional)"
echo ""
echo -e "  The backtester replays trades through the live bot's strategy stack."
echo -e "  Pull the strategy files now from the latest crypto_trader repo?"
echo ""
printf "    Pull crypto_trader strategy files? [Y/n, default=Y]: "; read -r PULL_STRATEGY
PULL_STRATEGY="${PULL_STRATEGY:-Y}"

STRATEGY_PULLED=false
if [[ "$PULL_STRATEGY" =~ ^[Yy] ]]; then

    # Resolve token — use GITHUB_TOKEN from step 2 if available, else prompt
    CT_TOKEN="$GITHUB_TOKEN"
    if [[ -z "$CT_TOKEN" ]]; then
        echo ""
        read -rsp "    GitHub token needed to query API (paste, ENTER): " CT_TOKEN; echo ""
    fi

    # Auto-detect latest crypto_trader_v* repo via GitHub API
    print_info "Searching for latest crypto_trader_v* repo under ${GH_ORG}..."

    API_AUTH=""
    [[ -n "$CT_TOKEN" ]] && API_AUTH="-H \"Authorization: token ${CT_TOKEN}\""

    CT_REPO=$(curl -fsSL \
        -H "Accept: application/vnd.github+json" \
        ${CT_TOKEN:+-H "Authorization: token ${CT_TOKEN}"} \
        "https://api.github.com/orgs/${GH_ORG}/repos?per_page=100" 2>/dev/null \
        | python3 -c "
import sys, json
try:
    repos = json.load(sys.stdin)
    matches = sorted(
        [r['name'] for r in repos if r['name'].startswith('crypto_trader_v')],
        reverse=True
    )
    print(matches[0] if matches else '')
except Exception:
    print('')
" 2>/dev/null)

    if [[ -z "$CT_REPO" ]]; then
        print_warn "Could not find a crypto_trader_v* repo. Skipping strategy pull."
        print_warn "You can copy files manually to: ${INSTALL_DIR}/crypto_trader/"
    else
        print_ok "Found: ${GH_ORG}/${CT_REPO}"
        CT_CLONE_URL="https://${CT_TOKEN}@github.com/${GH_ORG}/${CT_REPO}.git"
        CT_DEPLOY="$HOME/ct-deploy-tmp"

        print_info "Cloning ${CT_REPO}..."
        git clone -q "$CT_CLONE_URL" "$CT_DEPLOY" 2>/dev/null

        if [ -d "$CT_DEPLOY" ]; then
            mkdir -p "$INSTALL_DIR/crypto_trader"

            # Copy strategy and analysis files — the ones the backtester imports
            STRATEGY_FILES=(
                "analysis/regime_classifier.py"
                "analysis/volatility_engine.py"
                "analysis/liquidity_mapper.py"
                "analysis/structure_analyzer.py"
                "strategy/momentum_strategy.py"
                "strategy/compression_scalp_strategy.py"
                "strategy/sweep_reversal_strategy.py"
                "strategy/mean_reversion_strategy.py"
                "risk/risk_manager.py"
                "config.py"
            )

            COPIED=0
            MISSING=0
            for f in "${STRATEGY_FILES[@]}"; do
                src="$CT_DEPLOY/$f"
                dest="$INSTALL_DIR/crypto_trader/$(basename $f)"
                if [ -f "$src" ]; then
                    cp "$src" "$dest"
                    COPIED=$((COPIED + 1))
                else
                    print_warn "Not found in repo: $f"
                    MISSING=$((MISSING + 1))
                fi
            done

            rm -rf "$CT_DEPLOY"
            print_ok "Copied ${COPIED} strategy files → ${INSTALL_DIR}/crypto_trader/"
            [ "$MISSING" -gt 0 ] && print_warn "${MISSING} files not found — check repo structure"
            STRATEGY_PULLED=true
        else
            print_warn "Clone failed. Skipping strategy pull."
        fi
    fi
else
    print_ok "Skipping strategy pull — copy files manually to crypto_trader/ when ready."
fi

# ─── STEP 4: SYSTEM PACKAGES ─────────────────────────────────────────────────
print_step "4/8" "System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv python-is-python3 \
    git rsync bc sqlite3 curl wget
print_ok "System packages ready."

# ─── STEP 5: CLONE & INSTALL BACKTESTER FILES ────────────────────────────────
print_step "5/8" "Installing backtester files"

if [ -d "$DEPLOY_DIR/.git" ]; then
    echo "  Updating existing repo..."
    cd "$DEPLOY_DIR" && git pull -q
else
    echo "  Cloning repository..."
    if [[ -n "$GITHUB_TOKEN" && -n "$GITHUB_REPO" ]]; then
        git clone -q "https://${GITHUB_TOKEN}@github.com/${GITHUB_REPO}.git" "$DEPLOY_DIR"
    else
        git clone -q "$REPO" "$DEPLOY_DIR"
    fi
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

mkdir -p "$INSTALL_DIR/data/cache"
mkdir -p "$INSTALL_DIR/reports/output"

chmod +x "$INSTALL_DIR/push.sh" 2>/dev/null || true

for f in main.py config.py backtest/data_fetcher.py backtest/replay.py; do
    [ -f "$INSTALL_DIR/$f" ] || { echo "ERROR: $f missing. Aborting."; exit 1; }
done
print_ok "Files installed to ${INSTALL_DIR}"

# ─── STEP 6: PYTHON ENVIRONMENT ──────────────────────────────────────────────
print_step "6/8" "Python environment"
python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install --upgrade pip -q
pip install ccxt pandas numpy matplotlib plotly reportlab scipy requests -q
print_ok "Dependencies installed."

grep -q "btc-backtester/venv" ~/.bashrc || echo "source $VENV/bin/activate" >> ~/.bashrc
grep -q "cd ~/btc-backtester"  ~/.bashrc || echo "cd $INSTALL_DIR"           >> ~/.bashrc

# ─── STEP 7: WRITE SESSION CONFIG ────────────────────────────────────────────
print_step "7/8" "Writing session config"

sed -i "s/^DEFAULT_STARTING_BALANCE.*/DEFAULT_STARTING_BALANCE  = ${BALANCE_INPUT}/" \
    "$INSTALL_DIR/config.py"

print_ok "config.py updated — default balance \$${BALANCE_INPUT}"

# ─── STEP 8: GIT INIT ────────────────────────────────────────────────────────
print_step "8/8" "Git setup"

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
if [ "$STRATEGY_PULLED" = true ]; then
echo -e "  Strategy files:   ${INSTALL_DIR}/crypto_trader/  ✓ pulled from ${CT_REPO}"
else
echo -e "  Strategy files:   ${INSTALL_DIR}/crypto_trader/  ○ not pulled — copy manually"
fi
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
