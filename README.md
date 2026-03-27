# Research Papers

Python pipeline for turning Google Drive research notebooks into structured notebook summaries, an indicator library, generated strategy definitions, local-market-data backtests, and per-strategy tradebook CSVs.

## What Runs

`python main.py` does this:

1. Loads configuration from environment variables and an optional project-root `.env`.
2. Authenticates one or more Google Drive accounts.
3. Scans My Drive plus any configured Shared Drives.
4. Keeps notebook-like files only:
   - `.ipynb`
   - Google Colab notebooks (`application/vnd.google.colaboratory`)
5. Downloads or exports each notebook to a temporary file.
6. Parses notebook cells with `nbformat`.
7. Extracts indicators notebook-by-notebook with an OpenAI-compatible LLM when configured, rotating across configured API keys and falling back to deterministic parsing if needed.
8. Builds a notebook summary, using the extracted indicators plus an OpenAI-compatible LLM summary when configured or a deterministic fallback otherwise.
9. Merges notebook findings into `storage/indicator_library.json`.
10. Generates strategy definitions from indicators supported by the local backtest engine.
11. Scans local CSV and Parquet market-data files.
12. Loads usable OHLCV data for each configured backtest job.
13. Discovers every backtest engine module in `engine/`.
14. Runs every generated strategy across every enabled backtest job and every matching engine.
15. Writes one CSV tradebook per strategy, per job, per engine plus a JSON manifest.

If you only want a filename-based market-data inventory:

```bash
python scan_market_data.py
```

That script only scans configured CSV and Parquet paths and writes `storage/market_data_catalog.json`.

If you only want notebook summaries, the indicator library, and strategies without backtests:

```bash
python refresh_notebook_artifacts.py
```

## Repository Layout

```text
Research_Papers/
|-- main.py
|-- scan_market_data.py
|-- config.py
|-- schemas.py
|-- requirements.txt
|-- setup.ps1
|-- backtest_jobs.example.json
|-- ai/
|   |-- __init__.py
|   |-- indicator_extractor.py
|   |-- llm_client.py
|   |-- strategy_generator.py
|   `-- summarizer.py
|-- drive/
|   |-- __init__.py
|   |-- auth.py
|   `-- fetcher.py
|-- engine/
|   |-- base.py
|   |-- __init__.py
|   |-- backtester.py
|   |-- jobs.py
|   |-- registry.py
|   `-- tradebook.py
|-- utils/
|   |-- __init__.py
|   |-- chunking.py
|   |-- file_utils.py
|   `-- indicator_registry.py
|-- credentials/
|   `-- google_drive/
|-- data/
|-- storage/
`-- output/
    `-- tradebooks/
```

## Installation

Fastest Windows setup:

```powershell
.\setup.ps1
```

That script:

- creates `.venv/` if needed
- installs `requirements.txt`
- creates `.env` from `.env.example` if `.env` does not exist

Manual setup:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Dependencies

The cleaned project only keeps the packages used by the active pipeline:

- `google-api-python-client`
- `google-auth`
- `google-auth-oauthlib`
- `openai`
- `nbformat`
- `pandas`
- `numpy`
- `pyarrow`

## Configuration

`AppConfig.from_env()` loads values from the shell environment first and then from a project-root `.env` file if present.

If `LLM_API_KEYS` contains multiple keys, notebook-level LLM requests rotate across them in round-robin order.

It also creates these working directories automatically:

- `storage/`
- `storage/tmp/`
- `storage/tokens/`
- `storage/market_data/`
- `output/tradebooks/`

### Required Google Credential Setup

This pipeline only needs Google Drive read access and uses the readonly scope:

- `https://www.googleapis.com/auth/drive.readonly`

Recommended local setup:

1. Run `.\setup.ps1` once.
2. Create at least one Google credential JSON file by following one of the step-by-step flows below.
3. Save the downloaded JSON file in `credentials/google_drive/`.
4. Edit `.env` if needed.
5. Optionally set `OPENAI_API_KEY`, `LLM_API_KEYS`, or market-data paths.

Credentials are not created by `setup.ps1`. You must create and download them from Google Cloud first.

### What To Enable In Google Cloud

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project or select an existing project for this pipeline.
3. Go to `APIs & Services` > `Library`.
4. Search for `Google Drive API`.
5. Open it and click `Enable`.

That is the only API this code currently requires for Drive scanning and notebook export.

### Option A: OAuth Client JSON For Your Own Google Drive

Use this when you want the script to read notebooks from your own Google account's My Drive.

1. In Google Cloud Console, open the same project where you enabled `Google Drive API`.
2. Go to `APIs & Services` > `OAuth consent screen`.
3. Configure the consent screen if Google asks for it.
4. If the app is in testing mode, add your Google account under `Test users`.
5. Go to `APIs & Services` > `Credentials`.
6. Click `Create Credentials` > `OAuth client ID`.
7. Choose `Desktop app` as the application type.
8. Enter any name you want, then click `Create`.
9. Click `Download JSON`.
10. Save the file in `credentials/google_drive/`.

What happens next:

- On the first `python main.py` run, the app opens a browser window for Google sign-in.
- After you approve access, the app writes a token cache into `storage/tokens/`.
- The downloaded OAuth client JSON stays in `credentials/google_drive/`.

### Option B: Service Account JSON For Shared Folders Or Shared Drives

Use this when the notebooks are in a folder or Shared Drive that you can explicitly share with a service account.

1. In Google Cloud Console, open the same project where you enabled `Google Drive API`.
2. Go to `APIs & Services` > `Credentials`.
3. Click `Create Credentials` > `Service account`.
4. Create the service account.
5. Open the service account and go to `Keys`.
6. Click `Add Key` > `Create new key` > `JSON`.
7. Download the JSON file.
8. Save the file in `credentials/google_drive/`.
9. Open the JSON file and copy the `client_email` value.
10. Share the target Google Drive folder with that email, or add it as a member of the target Shared Drive.

Important:

- A service account cannot read your private My Drive automatically.
- It only sees folders or Shared Drives that are explicitly shared with its `client_email`.
- If you want the pipeline to scan Shared Drives, also set `GOOGLE_SHARED_DRIVE_IDS` in `.env`.

### Which JSON File Should You Use?

- Use an OAuth client JSON (`installed`) for your own My Drive.
- Use a service account JSON (`service_account`) for shared folders or Shared Drives.
- Do not manually create an `authorized_user` JSON for normal setup. This repo creates token JSON files automatically in `storage/tokens/` after the OAuth login flow.

Example `.env`:

```dotenv
GOOGLE_CREDENTIAL_DIR=credentials/google_drive
OPENAI_API_KEY=sk-...
MARKET_DATA_DIRS=C:\path\to\ohlc_data
BACKTEST_JOBS_FILE=backtest_jobs.example.json
LLM_MODEL=gpt-4.1-mini
LOG_LEVEL=INFO
```

Supported credential inputs:

- `GOOGLE_CREDENTIAL_DIR`: preferred directory of credential JSON files
- `GOOGLE_CREDENTIAL_FILES`: comma- or semicolon-separated credential file paths
- `GOOGLE_CREDENTIAL_LIST_FILE`: optional text file containing one credential path per line
- `GOOGLE_CREDENTIAL_JSON` and `GOOGLE_CREDENTIAL_JSON_1..N`: inline JSON fallbacks

### Optional Variables

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_SHARED_DRIVE_IDS` | empty | Shared Drive IDs to scan in addition to My Drive. |
| `LLM_API_KEYS` | unset | Comma- or semicolon-separated API keys. Requests rotate round-robin across this list. |
| `OPENAI_API_KEYS` | unset | Alias for `LLM_API_KEYS`. |
| `GROQ_API_KEYS` | unset | Alias for `LLM_API_KEYS`. If set and no base URL is provided, the client defaults to Groq's OpenAI-compatible endpoint. |
| `OPENAI_API_KEY` | unset | Single-key fallback for OpenAI-compatible LLM access. |
| `GROQ_API_KEY` | unset | Single-key fallback for Groq access. |
| `LLM_BASE_URL` | unset | Base URL for OpenAI-compatible providers. |
| `OPENAI_BASE_URL` | unset | Alias for `LLM_BASE_URL`. |
| `GROQ_BASE_URL` | unset | Alias for `LLM_BASE_URL`. |
| `LLM_MODEL` | `gpt-4.1-mini` | Model name passed to the OpenAI-compatible Responses API. |
| `OPENAI_MODEL` | `gpt-4.1-mini` | Alias for `LLM_MODEL`. |
| `LLM_RETRIES` | `3` | Retry count for LLM JSON summary calls. |
| `OPENAI_RETRIES` | `3` | Alias for `LLM_RETRIES`. |
| `LLM_TIMEOUT_SECONDS` | `30` | Per-request timeout for OpenAI-compatible LLM calls before heuristic fallback. |
| `LLM_CHUNK_CHARS` | `7000` | Character budget per notebook chunk before summarization. |
| `OPENAI_CHUNK_CHARS` | `7000` | Alias for `LLM_CHUNK_CHARS`. |
| `LOG_LEVEL` | `INFO` | Python logging level. |
| `DRIVE_PAGE_SIZE` | `1000` | Page size for Google Drive listing requests. |
| `MAX_DOWNLOAD_MB` | `256` | Skip remote files larger than this size. |
| `TEXT_EXCERPT_CHARS` | `12000` | Maximum excerpt stored in notebook summaries. |
| `MARKET_DATA_FILES` | empty | Explicit CSV or Parquet files to load. |
| `MARKET_DATA_DIRS` | `./data, ./storage/market_data` | Directories recursively searched for CSV and Parquet files. |
| `BACKTEST_JOBS_FILE` | `./storage/backtest_jobs.json` | JSON file describing one or more backtest jobs, market filters, and per-job execution parameters. |
| `STRATEGY_TARGET_MIN` | `50` | Minimum generated strategy count. |
| `STRATEGY_TARGET_MAX` | `100` | Maximum generated strategy count. |
| `NO_ENTRY_AFTER` | `15:15` | Legacy fallback used only when no backtest jobs file exists. |
| `EOD_EXIT_TIME` | `15:25` | Legacy fallback used only when no backtest jobs file exists. |
| `BROKERAGE_RATE` | `0.001` | Brokerage as a fraction of turnover. |
| `SLIPPAGE_RATE` | `0.0001` | Slippage applied to entry and exit prices. |
| `CAPITAL_PER_TRADE` | `100000` | Fixed capital allocated per trade. |
| `DEFAULT_STOP_LOSS_PCT` | `0.005` | Stop loss percentage. |
| `DEFAULT_TAKE_PROFIT_PCT` | `0.015` | Take profit percentage. |

### Backtest Job File

Use `BACKTEST_JOBS_FILE` when you want multiple runs with different timings, markets, or risk settings.

If the file is missing, the pipeline falls back to one legacy `default` job built from:

- `NO_ENTRY_AFTER`
- `EOD_EXIT_TIME`
- `BROKERAGE_RATE`
- `SLIPPAGE_RATE`
- `CAPITAL_PER_TRADE`
- `DEFAULT_STOP_LOSS_PCT`
- `DEFAULT_TAKE_PROFIT_PCT`

Example job file:

```json
{
  "jobs": [
    {
      "job_id": "crypto_intraday",
      "engine_names": ["intraday_signal"],
      "market_categories": ["crypto"],
      "ticker_patterns": ["BTC.*", "ETH.*"],
      "no_entry_after": "23:15",
      "eod_exit_time": "23:55",
      "stop_loss_pct": 0.008,
      "take_profit_pct": 0.02
    },
    {
      "job_id": "india_cash",
      "market_categories": ["equities"],
      "ticker_patterns": ["^(RELIANCE|TCS|INFY)$"],
      "no_entry_after": "15:10",
      "eod_exit_time": "15:25"
    }
  ]
}
```

Supported job fields:

- `job_id`: unique run name used in output paths
- `enabled`: optional boolean
- `engine_names`: optional engine allowlist; empty means all discovered engines
- `market_categories`: optional dataset categories such as `crypto`, `indices`, `energy`, `equities`
- `ticker_patterns`: optional regex filters for ticker codes
- `market_data_files`: optional per-job file overrides
- `market_data_dirs`: optional per-job directory overrides
- `no_entry_after`, `eod_exit_time`, `stop_loss_pct`, `take_profit_pct`, `brokerage_rate`, `slippage_rate`, `capital_per_trade`

## Google Drive Authentication

`drive/auth.py` supports:

1. Service account JSON
2. Authorized user JSON
3. OAuth client credentials with `installed` or `web` type

Behavior by credential type:

- `service_account`: authenticates directly with readonly Drive scope
- `authorized_user`: loads and refreshes the stored user token if needed
- `installed` or `web`: launches a local OAuth flow when required and caches the resulting token under `storage/tokens/`

## Google Drive Fetching Rules

The fetcher:

- lists non-trashed files only
- excludes folders
- scans each configured account's My Drive
- optionally scans configured Shared Drives
- de-duplicates files globally by Drive file ID
- downloads binary files directly
- exports supported Google-native files before processing
- directly downloads Google Colab notebooks through Drive media content

Supported Google-native exports:

| Source type | Export format |
|---|---|
| Google Docs | `.txt` |
| Google Sheets | `.csv` |

Other Google-native file types are skipped.

## Notebook Summarization

Each notebook is read with `nbformat` and split into markdown and code sections.

The summarizer extracts:

- indicator mentions
- inferred indicator parameters
- imported library hints
- dataset references
- usage contexts
- a notebook-level summary

If any LLM API key is configured, notebook metadata and extracted findings are sent to an OpenAI-compatible Responses API request that must return JSON with exactly one `summary` field. If the request fails or no key is configured, the pipeline falls back to a deterministic heuristic summary.

Notebook summaries are written to:

```text
storage/notebook_summaries.json
```

## Indicator Extraction and Strategy Generation

`ai/indicator_extractor.py` detects indicators from:

- markdown sentences
- code-line pattern matching
- Python AST call expressions

It merges notebook findings into:

```text
storage/indicator_library.json
```

`ai/strategy_generator.py` converts the indicator library into `StrategyDefinition` objects and writes:

```text
storage/strategies.json
```

Generated strategies contain at least 3 indicators.

Current strategy archetypes:

- `trend_following`
- `crossover_confirmation`
- `mean_reversion`
- `pullback_reentry`
- `volatility_expansion`
- `momentum_breakout`

## Market Data Requirements

Backtests use local market data only.

### Discovery

The engine looks at:

- explicit files from `MARKET_DATA_FILES`
- directories from `MARKET_DATA_DIRS`
- default directories `./data` and `./storage/market_data` if `MARKET_DATA_DIRS` is unset

Directory scanning is recursive and includes:

- `*.csv`
- `*.parquet`

### Accepted Columns

The loader normalizes common synonyms:

| Canonical | Accepted examples |
|---|---|
| `datetime` | `datetime`, `timestamp`, `date_time`, `time_stamp` |
| `open` | `open`, `o` |
| `high` | `high`, `h` |
| `low` | `low`, `l` |
| `close` | `close`, `c`, `ltp` |
| `volume` | `volume`, `vol` |
| `ticker` | `ticker`, `symbol`, `instrument`, `tradingsymbol` |

Minimum required schema after normalization:

- `datetime`
- `open`
- `high`
- `low`
- `close`

Optional columns:

- `volume`
- `ticker`

If `ticker` is missing, the file stem becomes the ticker symbol.  
If `volume` is missing, the engine fills it with `0.0`.

## Backtest Engine

`engine/backtester.py` is the built-in `intraday_signal` engine.

The pipeline now:

- auto-discovers engine classes in `engine/`
- loads all enabled backtest jobs from `BACKTEST_JOBS_FILE`
- runs every strategy for every `job x engine` combination that matches the job allowlist

Future engines only need to subclass `BaseBacktestEngine` and live under `engine/` to be discovered automatically.

Execution characteristics:

- grouped by ticker
- then grouped by session date
- at most one open position per ticker per session
- long and short entries supported
- fixed capital per trade

Exit priority for open positions:

1. stop loss
2. take profit
3. signal exit
4. end-of-day exit

Tradebook CSV columns:

- `ticker`
- `entry_time`
- `exit_time`
- `entry_price`
- `exit_price`
- `position`
- `quantity`
- `gross_pnl`
- `costs`
- `pnl`
- `return_pct`
- `exit_reason`
- `cumulative_pnl`

Possible `exit_reason` values:

- `SL`
- `TP`
- `SIGNAL`
- `EOD`

## Output Files

Active pipeline artifacts:

- `storage/notebook_summaries.json`
- `storage/indicator_library.json`
- `storage/strategies.json`
- `storage/market_data_catalog.json`
- `storage/backtest_jobs.json` if you use the default job-file location
- `storage/pipeline_manifest.json`
- `output/tradebooks/<engine_name>/<job_id>/*.csv`

## Quick Start

1. Run `.\setup.ps1`.
2. Enable `Google Drive API` and download either an OAuth client JSON or a service-account JSON as described in `Required Google Credential Setup`.
3. Save that JSON file in `credentials/google_drive/`.
4. Optionally set `OPENAI_API_KEY` or other LLM variables in `.env`.
5. Put CSV or Parquet OHLC data in `data/` or point `MARKET_DATA_DIRS` to your market-data folders.
6. Define one or more jobs in `backtest_jobs.example.json` or `storage/backtest_jobs.json`.
7. Activate the environment with `.venv\Scripts\Activate.ps1`.
8. Run `python main.py`.
9. If you used OAuth client JSON, finish the browser sign-in once so the token file is created under `storage/tokens/`.

## Files To Read First

If you are extending the active pipeline, start here:

1. `main.py`
2. `config.py`
3. `drive/auth.py`
4. `drive/fetcher.py`
5. `ai/summarizer.py`
6. `ai/indicator_extractor.py`
7. `ai/strategy_generator.py`
8. `engine/jobs.py`
9. `engine/registry.py`
10. `engine/backtester.py`
11. `engine/tradebook.py`
#   R e s e a r c h  
 