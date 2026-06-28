#!/bin/bash
# install.sh — backtester_engine_v1
# =============================================================================
# install.sh — backtester_engine_v1
# v1.0  — 2026-06-28 — EC2 web installer, mirrors crypto_trader install.sh pattern
# v1.1  — 2026-06-28 — Correct repo URL (backtester_engine_v1), .gitignore compliance
# v1.2  — 2026-06-28 — Step 3: optional crypto_trader strategy file pull from GitHub
#                       Auto-detects latest crypto_trader_v* repo under TX-9AI
# v1.3  — 2026-06-28 — Fix: strip full URL from GITHUB_REPO input (accept slug or full URL)
#                       Fix: API auth token passed correctly to org repo search
# v1.4  — 2026-06-28 — Fix: GitHub API endpoint /orgs/ → /users/ (TX-9AI is a user not an org)
# v1.5  — 2026-06-28 — Replace manual quarter display with status.py dashboard
# v1.6  — 2026-06-28 — Fix: embed GitHub token in remote URL so push never prompts for credentials
# v1.7  — 2026-06-28 — Fix: patch bt_config.py instead of config.py for balance sed
# v1.8  — 2026-06-28 — Fix: copy full analysis/, strategy/, utils/ packages from crypto_trader repo
#                       instead of flat individual files — strategy imports require full package structure
# v1.9  — 2026-06-28 — Fix: copy ALL packages (analysis, data, database, execution,
#                       notifications, risk, strategy, utils) — not just subset
# v1.10 — 2026-06-28 — Fix: add tzdata to pip install (required by utils/time_utils.py ZoneInfo)
# v1.11 — 2026-06-28 — Fix: add yfinance to pip install (required by macro_data.py)
# v1.12 — 2026-06-28 — Add: gdown install + Kraken OHLCVT full history download
#                       Fix: file validation check config.py → bt_config.py
#                       Add: gdown to pip install
# v1.13 — 2026-06-28 — Remove: disk expansion step (set EBS volume size at EC2 launch instead)
# =============================================================================

export DEBIAN_FRONTEND=noninteractive
export TERM=xterm-256color

REPO="https://github.com/TX-9AI/backtester_engine_v1.git"
INSTALL_DIR="$HOME/btc-backtester"
DEPLOY_DIR="$HOME/btc-backtester-deploy"
VENV="$INSTALL_DIR/venv"
GH_ORG="TX-9AI"
KRAKEN_ZIP_ID="1ptNqWYidLkhb2VAKuLCxmp2OXEfGO-AP"
KRAKEN_ZIP="$HOME/Kraken_OHLCVT.zip"

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

# ─── STEP 1: STARTING BALANCE ─────────────────────────────────────────────────
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

# ─── STEP 2: GITHUB REPO & TOKEN ──────────────────────────────────────────────
print_step "2/8" "GitHub Repository"
echo ""
GITHUB_REPO=""
GITHUB_TOKEN=""
printf "    GitHub repo [e.g. TX-9AI/backtester_engine_v1, ENTER to skip]: "; read -r GITHUB_REPO

if [[ -n "$GITHUB_REPO" ]]; then
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

# ─── STEP 3: CRYPTO_TRADER STRATEGY FILES ─────────────────────────────────────
print_step "3/8" "crypto_trader Strategy Files (optional)"
echo ""
echo -e "  The backtester replays trades through the live bot's strategy stack."
echo -e "  Pull the strategy files now from the latest crypto_trader repo?"
echo ""
printf "    Pull crypto_trader strategy files? [Y/n, default=Y]: "; read -r PULL_STRATEGY
PULL_STRATEGY="${PULL_STRATEGY:-Y}"

STRATEGY_PULLED=false
if [[ "$PULL_STRATEGY" =~ ^[Yy] ]]; then
    CT_TOKEN="$GITHUB_TOKEN"
    if [[ -z "$CT_TOKEN" ]]; then
        echo ""
        read -rsp "    GitHub token needed to query API (paste, ENTER): " CT_TOKEN; echo ""
    fi

    print_info "Searching for latest crypto_trader_v* repo under ${GH_ORG}..."

    CT_REPO=$(curl -fsSL \
        -H "Accept: application/vnd.github+json" \
        ${CT_TOKEN:+-H "Authorization: token ${CT_TOKEN}"} \
        "https://api.github.com/users/${GH_ORG}/repos?per_page=100" 2>/dev/null \
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
            COPIED=0
            MISSING=0

            for pkg in analysis data database execution notifications risk strategy utils; do
                src="$CT_DEPLOY/$pkg"
                dest="$INSTALL_DIR/crypto_trader/$pkg"
                if [ -d "$src" ]; then
                    cp -r "$src" "$dest"
                    count=$(find "$dest" -name "*.py" | wc -l)
                    COPIED=$((COPIED + count))
                    print_ok "Copied $pkg/ → crypto_trader/$pkg/ ($count files)"
                else
                    print_warn "Package not found in repo: $pkg/"
                    MISSING=$((MISSING + 1))
                fi
            done

            if [ -f "$CT_DEPLOY/config.py" ]; then
                cp "$CT_DEPLOY/config.py" "$INSTALL_DIR/crypto_trader/config.py"
                COPIED=$((COPIED + 1))
                print_ok "Copied config.py → crypto_trader/config.py"
            else
                print_warn "config.py not found in repo root"
                MISSING=$((MISSING + 1))
            fi

            rm -rf "$CT_DEPLOY"
            print_ok "Total: ${COPIED} files copied to crypto_trader/"
            [ "$MISSING" -gt 0 ] && print_warn "${MISSING} packages/files not found"
            STRATEGY_PULLED=true
        else
            print_warn "Clone failed. Skipping strategy pull."
        fi
    fi
else
    print_ok "Skipping strategy pull — copy files manually to crypto_trader/ when ready."
fi

# ─── STEP 4: SYSTEM PACKAGES ──────────────────────────────────────────────────
print_step "4/8" "System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-pip python3-venv python-is-python3 \
    git rsync bc sqlite3 curl wget
print_ok "System packages ready."

# ─── STEP 5: CLONE & INSTALL BACKTESTER FILES ─────────────────────────────────
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

for f in main.py bt_config.py backtest/data_fetcher.py backtest/replay.py; do
    [ -f "$INSTALL_DIR/$f" ] || { echo "ERROR: $f missing. Aborting."; exit 1; }
done
print_ok "Files installed to ${INSTALL_DIR}"

# ─── STEP 7: PYTHON ENVIRONMENT ───────────────────────────────────────────────
print_step "6/8" "Python environment"
python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install --upgrade pip -q
pip install ccxt pandas numpy matplotlib plotly reportlab scipy \
    requests tzdata yfinance gdown -q
print_ok "Dependencies installed."

grep -q "btc-backtester/venv" ~/.bashrc || echo "source $VENV/bin/activate" >> ~/.bashrc
grep -q "cd ~/btc-backtester"  ~/.bashrc || echo "cd $INSTALL_DIR"           >> ~/.bashrc

# ─── STEP 8: WRITE SESSION CONFIG ─────────────────────────────────────────────
print_step "7/8" "Writing session config"
sed -i "s/^DEFAULT_STARTING_BALANCE.*/DEFAULT_STARTING_BALANCE  = ${BALANCE_INPUT}/" \
    "$INSTALL_DIR/bt_config.py"
print_ok "bt_config.py updated — default balance \$${BALANCE_INPUT}"

# ─── STEP 9: GIT INIT + KRAKEN DATA DOWNLOAD ──────────────────────────────────
print_step "8/8" "Git setup + Kraken historical data download"

cd "$INSTALL_DIR"
if [ ! -d ".git" ]; then
    git init -q
    git branch -M main 2>/dev/null || true
    if [[ -n "$GITHUB_REPO" ]]; then
        git remote add origin "https://${GITHUB_TOKEN}@github.com/${GITHUB_REPO}.git"
        print_ok "Git repo initialized — push.sh ready to use"
    else
        print_ok "Git initialized — add remote manually when ready"
    fi
fi

# Download Kraken OHLCVT full history
echo ""
echo -e "  Kraken provides official historical OHLCVT data (all pairs, all timeframes)."
echo -e "  This is the data source for backtesting — ~8GB download, stored in ~/data/."
echo -e "  Download now? Requires ~20GB free disk space during extraction."
echo ""
printf "    Download Kraken OHLCVT history? [Y/n, default=Y]: "; read -r DOWNLOAD_DATA
DOWNLOAD_DATA="${DOWNLOAD_DATA:-Y}"

if [[ "$DOWNLOAD_DATA" =~ ^[Yy] ]]; then
    print_info "Downloading Kraken_OHLCVT.zip from Google Drive..."
    print_info "This will take 10-20 minutes depending on connection speed."
    echo ""
    gdown "${KRAKEN_ZIP_ID}" -O "${KRAKEN_ZIP}"

    if [ -f "${KRAKEN_ZIP}" ]; then
        ZIP_SIZE=$(du -sh "${KRAKEN_ZIP}" | cut -f1)
        print_ok "Download complete: ${KRAKEN_ZIP} (${ZIP_SIZE})"
        echo ""
        print_info "Run the following to process the data into quarterly cache files:"
        echo ""
        echo -e "    ${CYAN}python load_kraken_csv.py${RESET}"
        echo ""
        print_info "This will extract XBTUSD 1m data, split by quarter, and delete the ZIP."
    else
        print_warn "Download failed. Run manually: gdown ${KRAKEN_ZIP_ID}"
    fi
else
    print_ok "Skipping data download — run 'gdown ${KRAKEN_ZIP_ID}' when ready."
fi

# ── Final status ───────────────────────────────────────────────────────────────
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
echo -e "  Next steps:"
echo -e "    python load_kraken_csv.py         — process downloaded data into cache"
echo -e "    python fetch_data.py --status     — check cache status"
echo -e "    python main.py --quarter 2025-Q1  — run a backtest"
echo ""

source "${VENV}/bin/activate"
cd "$INSTALL_DIR"
python status.py

exec bash
