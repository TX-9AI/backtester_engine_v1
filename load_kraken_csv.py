# load_kraken_csv.py — backtester_engine_v1
# v1.0 — 2026-06-28 — Process Kraken OHLCVT ZIP into quarterly cache CSVs
# v1.1 — 2026-06-28 — Fix: chunked reading to avoid OOM kill on T2 micro (1GB RAM)
#                      Extracts CSV from ZIP first, then streams in 100k-row chunks
#                      Writing per-quarter files incrementally — never loads full dataset

"""
Extracts XBTUSD 1-minute OHLCVT data from the official Kraken historical
data ZIP, splits it into quarterly cache files matching data_fetcher format,
then deletes the ZIP and raw extracted files to reclaim disk space.

Kraken ZIP structure:
    master_q4/XBTUSD_1.csv  ← 1-minute BTC/USD (what we want)

Kraken CSV columns (no header):
    timestamp, open, high, low, close, volume, trades

Output: data/cache/BTC_USD_1m_YYYY-QN.csv per quarter
        columns: timestamp, open, high, low, close, volume

Usage:
    python load_kraken_csv.py                    # process ~/Kraken_OHLCVT.zip
    python load_kraken_csv.py --zip ~/my.zip     # specify ZIP path
    python load_kraken_csv.py --no-delete        # keep ZIP after processing
"""

import argparse
import csv
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bt_config as cfg

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN  = '\033[0;32m'
YELLOW = '\033[1;33m'
CYAN   = '\033[0;36m'
BOLD   = '\033[1m'
DIM    = '\033[2m'
RESET  = '\033[0m'

DEFAULT_ZIP = Path.home() / "Kraken_OHLCVT.zip"
TARGET_FILE = "XBTUSD_1.csv"
CHUNK_SIZE  = 100_000   # rows per chunk — safe for 1GB RAM


def quarter_label(ts_sec: int) -> str:
    dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
    q  = (dt.month - 1) // 3 + 1
    return f"{dt.year}-Q{q}"


def find_target_in_zip(zf: zipfile.ZipFile) -> str | None:
    for name in zf.namelist():
        if Path(name).name == TARGET_FILE:
            return name
    for name in zf.namelist():
        n = Path(name).name.upper()
        if "XBTUSD" in n and n.endswith("_1.CSV"):
            return name
    return None


def fmt_size(path: Path) -> str:
    b = path.stat().st_size
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}GB"


def main():
    parser = argparse.ArgumentParser(description="Load Kraken OHLCVT ZIP into quarterly cache")
    parser.add_argument("--zip",       type=str, default=str(DEFAULT_ZIP))
    parser.add_argument("--no-delete", action="store_true")
    args = parser.parse_args()

    zip_path = Path(args.zip)

    print(f"\n{BOLD}{CYAN}  Kraken OHLCVT Loader — backtester_engine_v1{RESET}")
    print(f"  {'─'*52}")

    if not zip_path.exists():
        print(f"\n  {YELLOW}⚠  ZIP not found: {zip_path}{RESET}")
        print(f"  Download: gdown 1ptNqWYidLkhb2VAKuLCxmp2OXEfGO-AP\n")
        sys.exit(1)

    zip_size = zip_path.stat().st_size / (1024**3)
    print(f"\n  ZIP: {zip_path}  ({zip_size:.2f}GB)")

    # ── Find target in ZIP ────────────────────────────────────────────────────
    print(f"\n  Scanning ZIP for {TARGET_FILE}...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        target = find_target_in_zip(zf)
        if not target:
            print(f"  {YELLOW}⚠  {TARGET_FILE} not found. XBTUSD files in ZIP:{RESET}")
            for n in zf.namelist():
                if "XBTUSD" in n.upper():
                    print(f"    {n}")
            sys.exit(1)

        uncompressed = zf.getinfo(target).file_size / (1024**3)
        print(f"  Found: {target}  ({uncompressed:.2f}GB uncompressed)")

        # Extract just the one file we need
        print(f"\n  Extracting {TARGET_FILE} to disk...")
        extracted_path = Path.home() / TARGET_FILE
        with zf.open(target) as src, open(extracted_path, "wb") as dst:
            while True:
                block = src.read(8 * 1024 * 1024)  # 8MB blocks
                if not block:
                    break
                dst.write(block)

    print(f"  {GREEN}✓{RESET}  Extracted: {extracted_path} ({fmt_size(extracted_path)})")

    # ── Open per-quarter output files ─────────────────────────────────────────
    cfg.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    quarter_files   = {}   # label → open file handle
    quarter_writers = {}   # label → csv.writer
    quarter_counts  = {}   # label → row count
    header = ["timestamp", "open", "high", "low", "close", "volume"]

    print(f"\n  Streaming CSV in {CHUNK_SIZE:,}-row chunks → quarterly files...")
    print(f"  {'─'*52}")

    total_rows = 0
    prev_label = None

    with open(extracted_path, "r") as f:
        reader = csv.reader(f)
        chunk  = []

        for row in reader:
            if not row or len(row) < 6:
                continue
            chunk.append(row)

            if len(chunk) >= CHUNK_SIZE:
                total_rows += _process_chunk(
                    chunk, quarter_files, quarter_writers,
                    quarter_counts, header
                )
                chunk = []
                # Progress indicator
                q_done = len(quarter_counts)
                print(f"  {DIM}...{total_rows:,} rows processed, "
                      f"{q_done} quarters started{RESET}", end="\r")

        # Final partial chunk
        if chunk:
            total_rows += _process_chunk(
                chunk, quarter_files, quarter_writers,
                quarter_counts, header
            )

    # ── Close all output files ────────────────────────────────────────────────
    for fh in quarter_files.values():
        fh.close()

    print(f"\n  {'─'*52}")
    print(f"  {GREEN}✓{RESET}  {total_rows:,} total rows processed")
    print(f"\n  Quarters written:")

    for label in sorted(quarter_counts.keys()):
        out_path = cfg.CACHE_DIR / f"BTC_USD_1m_{label}.csv"
        size = fmt_size(out_path) if out_path.exists() else "?"
        print(f"    {GREEN}✓{RESET}  {label}  "
              f"{quarter_counts[label]:>8,} candles  ({size})")

    # ── Cleanup ───────────────────────────────────────────────────────────────
    print(f"\n  Cleaning up extracted file...")
    extracted_path.unlink()
    print(f"  {GREEN}✓{RESET}  Deleted {extracted_path}")

    if not args.no_delete:
        print(f"  Deleting ZIP...")
        zip_path.unlink()
        print(f"  {GREEN}✓{RESET}  Deleted {zip_path}")

    print(f"\n  {BOLD}Done.{RESET} {len(quarter_counts)} quarters cached.")
    print(f"\n  Next steps:")
    print(f"    {CYAN}python fetch_data.py --status{RESET}      — verify cache")
    print(f"    {CYAN}python main.py --quarter 2025-Q1{RESET}   — run a backtest\n")


def _process_chunk(chunk, quarter_files, quarter_writers, quarter_counts, header):
    """Write a chunk of rows to per-quarter CSV files. Returns row count written."""
    written = 0
    for row in chunk:
        try:
            ts_sec = int(float(row[0]))
            label  = quarter_label(ts_sec)

            # Open new quarter file if needed
            if label not in quarter_files:
                out_path = cfg.CACHE_DIR / f"BTC_USD_1m_{label}.csv"
                fh = open(out_path, "w", newline="")
                quarter_files[label]   = fh
                quarter_writers[label] = csv.writer(fh)
                quarter_writers[label].writerow(header)
                quarter_counts[label]  = 0

            # Convert timestamp to ISO format matching our cache standard
            dt  = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
            ts_str = dt.strftime("%Y-%m-%d %H:%M:%S+00:00")

            quarter_writers[label].writerow([
                ts_str,
                row[1],   # open
                row[2],   # high
                row[3],   # low
                row[4],   # close
                row[5],   # volume
            ])
            quarter_counts[label] += 1
            written += 1
        except (ValueError, IndexError):
            continue
    return written


if __name__ == "__main__":
    main()
