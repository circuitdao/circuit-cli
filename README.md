# circuit-cli

A comprehensive CLI tool to manage your Circuit collateral vault, savings, and perform protocol/DAO upkeep for the
Circuit protocol on Chia blockchain.

## About Circuit

Circuit is a decentralized autonomous organization (DAO) on the Chia blockchain that provides a stablecoin protocol. The
circuit-cli tool enables users to:

- **Manage Collateral Vaults**: Deposit XCH as collateral, borrow BYC (the stablecoin), and manage your positions
- **Earn with Savings Vaults**: Deposit BYC to earn interest from protocol fees
- **Participate in Governance**: Propose and vote on protocol changes using CRT (governance tokens)
- **Run Protocol Operations**: Act as a keeper for liquidations, auctions, and protocol maintenance
- **Operate Price Oracles**: Announce price feeds and earn CRT rewards

## Installation

### From PyPI (Recommended)

```bash
pip install circuit-cli
```

### From Source

```bash
# Clone the repository
git clone https://github.com/circuitdao/circuit-cli
cd circuit-cli

# Install dependencies with Poetry
poetry install

# Run the CLI
poetry run circuit-cli --help
```

## Requirements

- **Python 3.11+**
- **chia-blockchain 2.5.5** (installed automatically as dependency)

## Quick Start

### 1. Environment Setup

Use the provided `env.sh` script to configure your environment:

```bash
# Set environment for testnet
. ./env.sh set test

# Or for mainnet
. ./env.sh set main

# Or for local simulator
. ./env.sh set sim

# View current settings
. ./env.sh show

# Clear environment
. ./env.sh clear
```

### 2. Private Key Configuration

```bash
# Set your private key (not stored by the tool)
export PRIVATE_KEY=your_private_key_hex

# Or pass it with each command
circuit-cli -p your_private_key_hex wallet balances
```

### 3. Basic Operations

```bash
# Check wallet balances
circuit-cli wallet balances

# View available commands
circuit-cli --help

# Get help for specific command group
circuit-cli vault --help
```

## Environment Variables

The CLI uses these environment variables (managed via `env.sh`):

- **`PRIVATE_KEY`**: Hex-encoded private key for signing transactions (not stored)
- **`BASE_URL`**: Circuit API endpoint URL (set by env.sh per network)
- **`ADD_SIG_DATA`**: Additional signature data/genesis challenge (set by env.sh per network)
- **`FEE_PER_COST`**: Optional transaction fee override
- **`NO_WAIT_TX`**: Skip waiting for transaction confirmation
- **`CIRCUIT_CLI_PROGRESS`**: Progress display mode (off/text/json)

## Command Reference

### Global Options

```bash
circuit-cli [GLOBAL_OPTIONS] COMMAND [COMMAND_OPTIONS]

Global Options:
  --verbose              Enable verbose logging
  --base-url URL         Override Circuit RPC API server URL
  --add-sig-data DATA    Override additional signature data
  --private-key KEY      Private key for signing (or use -p)
  --fee-per-cost FPC     Set fee: 'fast', 'medium', or integer mojos per cost
  --no-wait              Don't wait for transaction confirmation
  --json                 Output JSON instead of human-readable format
  --progress MODE        Progress display: 'off', 'text', or 'json'
  -dd PATH               Set persistence directory
```

### User Commands

#### Wallet Management

```bash
# View wallet addresses and puzzle hashes  
circuit-cli wallet addresses [-i INDEX] [-p]

# Check balances for XCH, BYC, and CRT
circuit-cli wallet balances

# List individual coins in wallet
circuit-cli wallet coins [-t TYPE]  # TYPE: xch|byc|crt|all|gov|empty|bill

# Toggle CRT coin between governance and regular mode
circuit-cli wallet toggle COIN_NAME [-i]
```

#### Collateral Vault Operations

```bash
# Show your vault status
circuit-cli vault show

# Deposit XCH as collateral
circuit-cli vault deposit AMOUNT

# Withdraw XCH collateral
circuit-cli vault withdraw AMOUNT

# Borrow BYC against collateral
circuit-cli vault borrow AMOUNT

# Repay BYC debt
circuit-cli vault repay AMOUNT
```

#### Savings Vault Operations

```bash
# Show savings vault status
circuit-cli savings show

# Deposit BYC to earn interest
circuit-cli savings deposit AMOUNT [INTEREST_AMOUNT]

# Withdraw BYC plus earned interest
circuit-cli savings withdraw AMOUNT [INTEREST_AMOUNT]
```

### Governance & Bills

```bash
# List your governance coins
circuit-cli bills list [-x] [-e] [-n] [-v] [-c] [-d] [-i] [-l]

# Toggle CRT coin governance mode
circuit-cli bills toggle COIN_NAME [-i]

# Propose new bill/statute change
circuit-cli bills propose INDEX VALUE [OPTIONS]

# Implement enacted bill
circuit-cli bills implement [COIN_NAME]

# Reset bill to nil
circuit-cli bills reset COIN_NAME
```

### Price Oracle Operations

#### Announcer Management

```bash
# Launch new price announcer
circuit-cli announcer launch PRICE

# Show announcer information
circuit-cli announcer show [-a] [-v] [-p]

# Update announcer price
circuit-cli announcer update PRICE [COIN_NAME]

# Configure announcer settings
circuit-cli announcer configure [COIN_NAME] [OPTIONS]

# Register with announcer registry for rewards
circuit-cli announcer register [COIN_NAME]

# Exit announcer and get deposit back
circuit-cli announcer exit [COIN_NAME]
```

#### Oracle Price Management

```bash
# View current oracle prices
circuit-cli oracle show

# Update oracle price queue (requires approved announcer)
circuit-cli oracle update [-i]
```

### Protocol Upkeep & Keeper Operations

#### System Information

```bash
# Show protocol invariants and constants
circuit-cli upkeep invariants

# Show protocol state (vaults, auctions, treasury)
circuit-cli upkeep state [-v] [-r] [-s] [-t] [-b]

# Check RPC server status
circuit-cli upkeep rpc status
circuit-cli upkeep rpc sync
circuit-cli upkeep rpc version
```

#### Liquidation Bot

```bash
# Run liquidation bot (continuously monitor)
circuit-cli upkeep liquidator [OPTIONS]

Options:
  --max-bid-amount AMOUNT     Maximum bid amount
  --min-discount DISCOUNT     Minimum price discount to bid
  --run-once                  Run once and exit
  --max-offer-amount AMOUNT   Max XCH per offer (default: 1.0)
  --offer-expiry-seconds SEC  Offer expiry time (default: 300)
```

#### Vault Management (Keeper Operations)

```bash
# List all vaults in protocol
circuit-cli upkeep vaults list [COIN_NAME] [-s] [-n]

# Transfer stability fees from vault to treasury
circuit-cli upkeep vaults transfer [COIN_NAME]

# Liquidate undercollateralized vault
circuit-cli upkeep vaults liquidate COIN_NAME [-t PUZZLE_HASH]

# Bid in liquidation auction
circuit-cli upkeep vaults bid COIN_NAME [AMOUNT] [--max-bid-price PRICE] [-i]

# Recover bad debt from vault
circuit-cli upkeep vaults recover COIN_NAME
```

#### Auction Participation

**Recharge Auctions** (sell BYC for CRT):

```bash
circuit-cli upkeep recharge list
circuit-cli upkeep recharge start COIN_NAME
circuit-cli upkeep recharge bid COIN_NAME [AMOUNT] [-crt AMOUNT] [-t PUZZLE_HASH] [-i]
circuit-cli upkeep recharge settle COIN_NAME
```

**Surplus Auctions** (sell CRT for BYC):

```bash
circuit-cli upkeep surplus list
circuit-cli upkeep surplus start
circuit-cli upkeep surplus bid COIN_NAME [AMOUNT] [-t PUZZLE_HASH] [-i]
circuit-cli upkeep surplus settle COIN_NAME
```

#### Treasury Management

```bash
# Show treasury status
circuit-cli upkeep treasury show

# Rebalance treasury coins
circuit-cli upkeep treasury rebalance [-i]

# Launch new treasury coin
circuit-cli upkeep treasury launch [SUCCESSOR_LAUNCHER_ID] [-c] [-b BILL_COIN_NAME]
```

#### Announcer Registry & Rewards

```bash
# Show announcer registry
circuit-cli upkeep registry show

# Distribute CRT rewards to announcers
circuit-cli upkeep registry reward [-t PUZZLE_HASH] [-i]

# Manage announcer approvals
circuit-cli upkeep announcers list [COIN_NAME] [-p] [-v]
circuit-cli upkeep announcers approve COIN_NAME [-c] [-b BILL_COIN_NAME]
circuit-cli upkeep announcers disapprove COIN_NAME [-c] [-b BILL_COIN_NAME]
circuit-cli upkeep announcers penalize [COIN_NAME]
```

### Administrative Commands

#### Statutes Management

```bash
# List current protocol statutes
circuit-cli statutes list [-f]

# Update statutes price
circuit-cli statutes update [-i]
```

#### CLI Self-Management

```bash
# Release store lock if CLI gets stuck
circuit-cli self unlock
```

## Common Use Cases

### For Regular Users

**Managing a Collateral Vault:**

```bash
# 1. Check your balances
circuit-cli wallet balances

# 2. View current vault
circuit-cli vault show

# 3. Deposit XCH as collateral
circuit-cli vault deposit 10.0

# 4. Borrow BYC stablecoin (maintain >150% collateral ratio)
circuit-cli vault borrow 100.0

# 5. Later, repay debt and withdraw collateral
circuit-cli vault repay 105.0  # includes stability fees
circuit-cli vault withdraw 5.0
```

**Earning with Savings:**

```bash
# 1. Deposit BYC to earn interest
circuit-cli savings deposit 50.0

# 2. Check savings status
circuit-cli savings show

# 3. Withdraw with earned interest
circuit-cli savings withdraw 55.0
```

### For Governance Participants

**Participating in Governance:**

```bash
# 1. Convert CRT to governance mode
circuit-cli bills toggle YOUR_CRT_COIN_NAME

# 2. Propose a statute change (e.g., stability fee rate)
circuit-cli bills propose 5 0.02  # Set 2% stability fee

# 3. Vote by implementing bills
circuit-cli bills implement BILL_COIN_NAME

# 4. Exit governance mode when done
circuit-cli bills toggle YOUR_GOVERNANCE_COIN_NAME
```

### For Keepers & Protocol Operators

**Running a Liquidation Bot:**

```bash
# Monitor and liquidate undercollateralized vaults
circuit-cli upkeep liquidator \
  --max-bid-amount 1000 \
  --min-discount 0.05 \
  --max-offer-amount 2.0
```

**Operating Price Oracle:**

```bash
# 1. Launch announcer with initial price
circuit-cli announcer launch 25.50

# 2. Get it approved through governance
circuit-cli upkeep announcers approve YOUR_ANNOUNCER_COIN

# 3. Register for rewards
circuit-cli announcer register

# 4. Update prices regularly
circuit-cli announcer update 26.75
```

**Participating in Auctions:**

```bash
# Bid in recharge auction (buy CRT with BYC)
circuit-cli upkeep recharge bid AUCTION_COIN 100.0 -crt 50.0

# Bid in surplus auction (buy BYC with CRT)  
circuit-cli upkeep surplus bid AUCTION_COIN 25.0
```

## Output Formats

### Human-Readable (Default)

Pretty-printed tables and formatted output for easy reading.

### JSON Output

Use `--json` flag for machine-readable output:

```bash
circuit-cli --json wallet balances
circuit-cli --json vault show
```

### Progress Monitoring

Control progress display during operations:

```bash
# Text progress (default)
circuit-cli --progress text vault deposit 10.0

# JSON progress events
circuit-cli --progress json upkeep liquidator --run-once

# No progress display
circuit-cli --progress off savings withdraw 50.0
```

## Fee Management

Control transaction fees with `--fee-per-cost`:

```bash
# Fast confirmation (1-2 blocks)
circuit-cli --fee-per-cost fast vault deposit 10.0

# Medium confirmation (up to 5 blocks)  
circuit-cli --fee-per-cost medium vault borrow 100.0

# Custom fee (mojos per cost)
circuit-cli --fee-per-cost 1000 savings deposit 50.0
```

## Network Support

- **Mainnet**: Production Circuit protocol
- **Testnet**: Testing and development
- **Simulator**: Local development environment

Switch networks using `env.sh`:

```bash
. ./env.sh set main    # Mainnet
. ./env.sh set test    # Testnet  
. ./env.sh set sim     # Simulator
```

## Development

### Building from Source

```bash
poetry build
```

### Publishing (Maintainers)

```bash
poetry publish --build
```

### Code Quality

- **Linting**: Ruff configured (line length 120)
- **Formatting**: Black configured (line length 120)

## Links

- **Homepage**: https://circuitdao.com
- **Repository**: https://github.com/circuitdao/circuit-cli
- **Bug Reports**: https://github.com/circuitdao/circuit-cli/issues
- **Circuit Protocol Docs**: https://docs.circuitdao.com

## Support

For questions and support:

- Check the documentation above
- Review command help: `circuit-cli COMMAND --help`
- Report issues on GitHub
- Join the Circuit community: https://discord.gg/HkCkSaqdKe


