# circuit-cli

CLI tool to manage your Circuit collateral vault, savings, and perform protocol/DAO upkeep for the Circuit protocol on Chia.

## Features
- Interact with Circuit RPC to manage vaults and savings
- Create, sign, and submit transactions against the Circuit protocol
- Helper commands for keepers and price announcers
- Works against mainnet, testnet, and local simulator environments

## Installation
- Using pip (from PyPI once released):
  - `pip install circuit-cli`
- From source (using Poetry):
  - `poetry install`
  - `poetry run circuit-cli --help`

## Quick start
1. Source env.sh to set environment variables for a target environment (main | test | sim):
   - `. ./env.sh set test`
   - Or clear/show: `. ./env.sh clear` / `. ./env.sh show`
2. Ensure you have your PRIVATE_KEY set in the environment or pass it via `-p`.
3. View available commands:
   - `circuit-cli --help`

## Environment variables
The CLI relies on a few environment variables that you can manage via the provided env.sh helper:
- PRIVATE_KEY: hex-encoded private key used for signing. Not stored by the tool. You can pass it with `-p`.
- BASE_URL: base URL of the Circuit API (set by env.sh for main/test/sim).
- ADD_SIG_DATA: additional signature data (genesis challenge). Set by env.sh per network.
- FEE_PER_COST: optional fee-per-cost override for transactions.

Example:
```
. ./env.sh set test
export PRIVATE_KEY=your_private_key_hex
circuit-cli wallet balances
```

## CLI entry point
When installed, the following command-line entry point is available:
- circuit-cli: main Circuit RPC CLI (includes keeper and price announcer related subcommands)

Run with --help to see options, e.g.:
```
circuit-cli --help
```

## Requirements
- Python 3.11+
- chia-blockchain 2.5.5 (installed as a dependency)

## Development
- Linting/formatting: Ruff and Black are configured (line length 120).
- Build: `poetry build`
- Publish (maintainers): `poetry publish --build`


