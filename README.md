# ibkr-paper-gateway

**IBKR Paper (IB Gateway)**: a small local MCP server that lets your AI assistant **paper-trade** on Interactive Brokers through **IB Gateway** (or TWS) running on your own computer. It reads your paper account, positions, quotes and price history, and places, modifies and cancels orders on your IBKR **paper** account. It refuses live accounts and live API ports.

> **Unofficial.** This project is not affiliated with, endorsed by, or supported by Interactive Brokers. It talks to IB Gateway over IBKR's TWS API using [`ib_async`](https://github.com/ib-api-reloaded/ib_async), the maintained successor of `ib_insync`. **Paper trading only. Not financial advice.**

- Runs locally over stdio (Python 3.11+). It is launched with [`uv`](https://docs.astral.sh/uv/), which fetches Python and `ib_async` automatically, so there is nothing else to install.
- Installs by URL as a plugin in **EnConvo**, **Claude Code** and **Codex**.
- You log in to IB Gateway yourself. This server never sees or stores your IBKR username or password.

Looking for read-only access to a live account through IBKR's official MCP server instead? See [ibkr-mcp-guard](https://github.com/tivojn/ibkr-mcp-guard).

## Safety model

| | |
|---|---|
| **Loopback only** | Connects only to `127.0.0.1` (or another loopback address). A non-loopback `IBKR_PAPER_HOST` is refused before anything is dialled. |
| **Live ports refused** | Default port **4002** (IB Gateway paper). **7497** (TWS paper) is allowed. The live ports **4001** (IB Gateway) and **7496** (TWS) are refused outright, whatever you set. Any other port must be set explicitly with `IBKR_PAPER_PORT`, and the paper gate below still applies. |
| **Paper gate on every order action** | Before **every** preview, place, modify and cancel, the server asks the Gateway for its managed accounts again (`reqManagedAccts`). It refuses unless **every** account starts with `DU` or `DF` (IBKR's paper prefixes) and the target account is one of them. If you log in with a live login on a paper port, everything order-related is refused. The verdict is never cached. |
| **Two-step orders** | `preview_order` checks the limits and the gate, runs IBKR's what-if check (`whatIfOrder`) and returns a plain read-back, the estimated notional and commission, and a `preview_id`. Nothing is sent. `place_order` needs that `preview_id` (valid **2 minutes**, single use) **and** `confirm: true`, and it runs the paper gate again. Modifications work the same way. Cancels need `confirm: true`. |
| **Approve/Cancel prompt** | If your MCP client supports [elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation), `place_order` and `modify_order` show the read-back and ask you to **Approve** or **Cancel**. The gate is checked once more after you approve. Otherwise your host's own tool-approval prompt applies (the order tools are marked `destructiveHint: true`). `IBKR_PAPER_AUTO=1` skips the prompt for automated strategy runs. A preview is still required. |
| **Limits** | Per order: at most **100 shares** or **10 option contracts**, at most **$50,000** estimated notional. At most **10 orders per minute**. Stocks and ETFs only by default (options are opt-in). US (USD) instruments only by default. **Market orders are refused outside regular trading hours** (and with `outsideRth`). All of these can be changed with [environment variables](#environment-variables). |
| **Audit log** | Every preview, place, modify and cancel attempt (allowed or refused, with the reason, the accounts seen, the order and the order id) and every fill seen is appended as one JSON line to `paper-orders.log` (mode `0600`) in the data directory. If the log cannot be written, the order is not sent. `paper_log` shows recent entries. |
| **No credentials** | You log in to IB Gateway yourself. The server only uses the API socket. There is no telemetry. |

Notional is estimated as quantity × multiplier × the higher of the order price and the current market price. A market order with no price available is refused. A SELL limit far below the market is therefore counted at the market price.

## Set up IB Gateway (paper)

1. **Get a paper account.** In IBKR's Client Portal, open **Settings → Paper Trading Account** and create it. Note the paper username. Paper accounts start with simulated funds and have ids starting with `DU`.
2. **Install IB Gateway (Stable)** from IBKR's website (search for "IB Gateway stable download").
3. **Log in.** Start IB Gateway, choose **IB API** (not FIX) and **Paper Trading**, then sign in with your **paper** username.
4. **API settings** (**Configure → Settings → API → Settings**):
   - **Socket port: 4002**
   - **Read-Only API: off** (otherwise orders are rejected)
   - **Allow connections from localhost only: on**
   - **Trusted IPs: 127.0.0.1**
   - Optional: under **Lock and Exit**, set auto-restart so the session survives the nightly restart.
5. **Re-login.** IBKR ends Gateway sessions daily (and needs a full login at least weekly). If the assistant says the Gateway is not reachable, log in again.

TWS works too: use its paper login and set the API port to **7497** with `IBKR_PAPER_PORT=7497`.

Paper accounts get live market data only if your live account shares its subscriptions with the paper account. Without that, quotes are **delayed**, and the `quote` tool says so.

## Install

### EnConvo

Open **Plugins → Install from URL** and paste:

```
https://github.com/tivojn/ibkr-paper-gateway
```

EnConvo reads `.mcp.json` and expands `${CLAUDE_PLUGIN_ROOT}`. You need `uv` on your PATH (`brew install uv`, or see [uv's install guide](https://docs.astral.sh/uv/getting-started/installation/)).

### Claude Code

```bash
claude plugin marketplace add https://github.com/tivojn/ibkr-paper-gateway
claude plugin install ibkr-paper-gateway@ibkr-paper-gateway
```

Inside Claude Code: `/plugin marketplace add tivojn/ibkr-paper-gateway`, then `/plugin install ibkr-paper-gateway@ibkr-paper-gateway`.

Or from a clone, as a plain MCP server:

```bash
git clone https://github.com/tivojn/ibkr-paper-gateway ~/ibkr-paper-gateway
claude mcp add ibkr-paper -- uv run --no-project --quiet --python ">=3.11" --with "ib_async>=2.0" python ~/ibkr-paper-gateway/server/main.py
```

### Codex

Clone the repo, then add to `~/.codex/config.toml`:

```toml
[mcp_servers.ibkr-paper]
command = "uv"
args = ["run", "--no-project", "--quiet", "--python", ">=3.11", "--with", "ib_async>=2.0",
        "python", "/Users/you/ibkr-paper-gateway/server/main.py"]
startup_timeout_sec = 60   # the first run downloads ib_async
```

The repo also ships `.codex-plugin/plugin.json` for hosts that install Codex-format plugins by URL.

`--no-project` stops `uv` from picking up an unrelated `pyproject.toml` in whatever directory the host starts the server from.

## Tools

| Tool | What it does |
|---|---|
| `gateway_status` | Connected?, host/port and whether it is a paper or live port, managed accounts labelled paper/LIVE, server time, limits, and whether paper order submission is allowed right now and why. |
| `account_summary {account?}` | Net liquidation, cash, buying power, margin, P&L. |
| `positions {account?}` | Positions and average cost. |
| `portfolio {account?}` | Positions with market price, market value and P&L. |
| `quote {symbol, secType?, exchange?, currency?}` | Snapshot bid/ask/last/close, labelled live or delayed. |
| `history {symbol, duration, barSize, whatToShow}` | Historical bars (default 5 D of 1-hour TRADES bars). |
| `open_orders` | Open orders with status and fill progress. |
| `executions {since?}` | Fills with commissions. |
| `preview_order {account?, symbol, secType?, action, quantity, orderType, limitPrice?, stopPrice?, tif?, outsideRth?}` | Step 1: read-back + estimates + `preview_id`. Sends nothing. |
| `place_order {preview_id, confirm: true}` | Step 2: sends the previewed order to the paper account. |
| `modify_order {orderId, quantity?/limitPrice?/stopPrice?/tif?/outsideRth?}` then `{orderId, preview_id, confirm: true}` | Previews, then sends, a change to an open order placed by this server. |
| `cancel_order {orderId, confirm: true}` | Cancels an open order placed by this server. |
| `paper_log {limit}` | Recent audit-log entries. |

`orderType` is `MKT`, `LMT`, `STP` or `STP_LMT`. `tif` is `DAY` or `GTC`. Options (`secType: "OPT"`, with `expiry`, `strike`, `right`) need `IBKR_PAPER_ALLOW_OPT=1`. Every result is a one-line summary followed by compact JSON.

Only orders placed by this server's API client id (`IBKR_CLIENT_ID`, default 17) can be modified or cancelled, which is how the TWS API works. Orders you placed by hand in TWS are shown by `open_orders` but cannot be changed here.

## Example

> **You:** buy 10 AAPL at market on paper
>
> **Assistant** calls `preview_order {symbol: "AAPL", action: "BUY", quantity: 10, orderType: "MKT"}`:
> *PREVIEW (not sent): BUY 10 share(s) of AAPL — market order, DAY, PAPER account DU1234567. Est. notional $3,372.00, est. commission $1.00. preview_id pv_… (valid 120 s).*
>
> **Assistant:** Here is the order. Shall I place it?
>
> **You:** yes
>
> **Assistant** calls `place_order {preview_id: "pv_…", confirm: true}` (your app may also show an Approve/Cancel prompt):
> *PAPER order 1012 sent: BUY 10 share(s) of AAPL … Status: Submitted.*

Other prompts:

- "Check my IB Gateway paper connection."
- "What's my paper account balance, positions and P&L?"
- "Quote SPY and show the last 5 days of hourly bars."
- "Place a limit order to sell 5 MSFT at 520, good till cancelled, on paper."
- "Move my AAPL limit order to 335." / "Cancel order 1012."
- "Show the paper order log."

## Strategy runs

For automated strategies (a scheduled task or loop that trades the paper account without a human in the chat):

- Set `IBKR_PAPER_AUTO=1` so `place_order` and `modify_order` do not wait for an Approve/Cancel prompt. Every order still needs `preview_order` first, then `place_order` with `confirm: true` within 2 minutes. The paper gate, the limits and the audit log still apply.
- Keep the limits tight for the strategy (`IBKR_PAPER_MAX_QTY`, `IBKR_PAPER_MAX_NOTIONAL`, `IBKR_PAPER_MAX_ORDERS_PER_MIN`).
- Prefer limit orders. Market orders are refused outside regular hours. The hours check uses the contract's liquid hours from IBKR, or 09:30–16:00 New York time on weekdays if those are unavailable (no holiday calendar).
- Use `executions {since}` and `paper_log` to reconcile what happened.
- Paper fills are simulated and can differ from what would happen in a live account. Paper results do not predict live results.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `IBKR_PAPER_HOST` | `127.0.0.1` | Must be loopback, otherwise refused. `localhost` is pinned to `127.0.0.1`. |
| `IBKR_PAPER_PORT` | `4002` | API port. `7497` for TWS paper. `4001`/`7496` are always refused. Other ports are allowed only when set here. |
| `IBKR_CLIENT_ID` | `17` | Fixed TWS API client id. Change it if another app uses 17. |
| `IBKR_PAPER_MAX_QTY` | `100` | Max shares per order. |
| `IBKR_PAPER_MAX_OPT_QTY` | `10` | Max option contracts per order. |
| `IBKR_PAPER_MAX_NOTIONAL` | `50000` | Max estimated notional (USD) per order. |
| `IBKR_PAPER_MAX_ORDERS_PER_MIN` | `10` | Max orders (placements and modifications) per rolling minute. |
| `IBKR_PAPER_SEC_TYPES` | `STK,ETF` | Allowed security types for orders. |
| `IBKR_PAPER_ALLOW_OPT` | off | `1` allows option orders (`OPT`). |
| `IBKR_PAPER_US_ONLY` | on | `0` allows non-USD / non-US-listed instruments. |
| `IBKR_PAPER_ALLOW_MKT_OUTSIDE_RTH` | off | `1` allows market orders outside regular hours. |
| `IBKR_PAPER_AUTO` | off | `1` skips the Approve/Cancel prompt (paper only; a preview is still required). |
| `CLAUDE_PLUGIN_DATA` / `PLUGIN_DATA` | | Data directory (set by plugin hosts). Otherwise `$XDG_CONFIG_HOME/ibkr-paper-gateway` or `~/.config/ibkr-paper-gateway`. |

An invalid or non-positive limit value falls back to the default (a warning goes to stderr), so a typo never loosens a limit.

Set these in your host's MCP server settings (EnConvo: the plugin's server environment; Claude Code: `claude mcp add -e IBKR_PAPER_AUTO=1 …`; Codex: `env = { IBKR_PAPER_AUTO = "1" }`).

## Troubleshooting

- **"IB Gateway is not reachable on 127.0.0.1:4002".** Start IB Gateway, log in with your **paper** login, and check **API → Settings**: port 4002, Read-Only API off. Gateway logs out daily; log in again.
- **"Could not connect … clientId".** Another program is connected with client id 17. Set `IBKR_CLIENT_ID` to another number.
- **Everything order-related is refused with "non-paper (LIVE) account(s)".** You are logged in with a live login. Log out of IB Gateway and log in with the paper login (choose **Paper Trading** on the login screen).
- **Orders are rejected with a read-only error.** Turn off **Read-Only API** in the Gateway's API settings.
- **Quotes are delayed.** Your paper account has no live market-data subscription. In Client Portal you can share your live account's subscriptions with the paper account.
- **"preview … expired".** Previews are valid 2 minutes. Preview again.
- **"order … is not an open order placed by this connection".** Only orders placed through this server (same client id) can be modified or cancelled.
- **The server doesn't start.** Check that `uv` is installed and on the PATH your host uses (GUI apps may not see your shell PATH; use the full path, e.g. `/opt/homebrew/bin/uv`, as the command).
- **Logs.** The server logs to stderr only. stdout carries MCP JSON-RPC.

## How it works

```
your AI host ──stdio (MCP)──▶ ibkr-paper-gateway ──TWS API socket (ib_async)──▶ IB Gateway on 127.0.0.1:4002 ──▶ IBKR paper account
                                   │ port policy · fresh paper gate · limits · preview/confirm
                                   └─ audit log: paper-orders.log (0600)
```

The MCP stdio protocol (newline-delimited JSON-RPC 2.0: `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`, plus outgoing `elicitation/create`) is implemented directly in about 200 lines instead of using the `mcp` SDK. That keeps the only dependency `ib_async`, makes the first launch through `uv` quick, and keeps the whole request path small enough to audit. The server connects lazily on the first tool call with a fixed client id, asks for delayed data where there is no live subscription (`reqMarketDataType(3)`), and reconnects on the next call after a drop.

### Development

```bash
uv run --no-project --with "ib_async>=2.0" python -m unittest -v      # offline, fake IB object
uv run --no-project --quiet --with "ib_async>=2.0" python scripts/live_smoke.py   # read-only: is a Gateway listening on 4002?
```

The offline tests cover the port policy (4001/7496 refused, 4002/7497 allowed, non-loopback refused), the paper gate (DU allowed; U, mixed, empty, and a target not in the list refused; fresh check on every call), the limits, the preview → place flow (unknown, expired and reused previews, missing confirm), gated modify and cancel, the audit log and its `0600` mode, Approve/Cancel elicitation and auto mode, the hooks on a real `ib_async.IB` object, and JSON-RPC round trips both in-process and through the real entry point over pipes. The live smoke check places no orders.

## Disclaimer

This software is provided as is, under the MIT License, without warranty of any kind. It is meant for IBKR **paper** (simulated) accounts only. It is **not financial advice**, and nothing it or your assistant outputs is a recommendation to buy or sell anything. AI assistants make mistakes: check every order yourself. **You use it at your own risk.** Interactive Brokers, IBKR, IB Gateway and TWS are marks of their owners; this project is not affiliated with them.

## License

[MIT](LICENSE) © 2026 Adam Cohen
