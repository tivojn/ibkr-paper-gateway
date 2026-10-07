"""Connection to IB Gateway / TWS through ib_async.

Connects lazily to loopback only, with a fixed clientId, and reconnects on the
next call after a drop. `fresh_accounts()` asks the Gateway for managedAccounts
again on every call (reqManagedAccts) and never reuses an earlier answer.
"""

from __future__ import annotations

import asyncio
import sys

from .policy import GATEWAY_HELP, PolicyError, check_endpoint, connect_host


class GatewayUnavailable(Exception):
    pass


def _default_factory():
    from ib_async import IB

    return IB()


class Broker:
    def __init__(self, cfg, ib_factory=None):
        self.cfg = cfg
        self.factory = ib_factory or _default_factory
        self.ib = None
        self._lock = asyncio.Lock()
        self._acct_waiters: list = []
        self.recent_errors: list = []
        self.disconnects = 0

    # -- connection -------------------------------------------------------
    def endpoint(self) -> str:
        """Port label; raises PolicyError for non-loopback hosts or live ports."""
        return check_endpoint(self.cfg.host, self.cfg.port)

    def connected(self) -> bool:
        return bool(self.ib is not None and self.ib.isConnected())

    def _install_hooks(self, ib) -> None:
        wrapper = ib.wrapper
        original = wrapper.managedAccounts

        def managed_accounts_hook(accounts_list: str):
            original(accounts_list)
            accounts = [a for a in accounts_list.split(",") if a]
            waiters, self._acct_waiters = self._acct_waiters, []
            for fut in waiters:
                if not fut.done():
                    fut.set_result(accounts)

        wrapper.managedAccounts = managed_accounts_hook

        def on_error(req_id, code, msg, *_):
            self.recent_errors.append({"reqId": req_id, "code": code, "message": msg})
            del self.recent_errors[:-20]

        def on_disconnect():
            self.disconnects += 1
            print("[ibkr-paper-gateway] disconnected from IB Gateway", file=sys.stderr, flush=True)

        ib.errorEvent += on_error
        ib.disconnectedEvent += on_disconnect

    async def ensure(self):
        """Return a connected IB object, connecting/reconnecting as needed."""
        self.endpoint()  # policy first; never dial a refused endpoint
        if self.connected():
            return self.ib
        async with self._lock:
            if self.connected():
                return self.ib
            if self.ib is None:
                self.ib = self.factory()
                self._install_hooks(self.ib)
            host = connect_host(self.cfg.host)
            try:
                await asyncio.wait_for(
                    self.ib.connectAsync(
                        host,
                        self.cfg.port,
                        clientId=self.cfg.client_id,
                        timeout=self.cfg.connect_timeout,
                        readonly=False,
                    ),
                    timeout=self.cfg.connect_timeout + 5,
                )
            except PolicyError:
                raise
            except (ConnectionRefusedError, asyncio.TimeoutError, TimeoutError, OSError) as e:
                self._safe_disconnect()
                raise GatewayUnavailable(
                    f"IB Gateway is not reachable on {host}:{self.cfg.port} ({type(e).__name__}). "
                    + GATEWAY_HELP
                ) from e
            except Exception as e:  # API handshake errors, clientId in use, ...
                self._safe_disconnect()
                raise GatewayUnavailable(
                    f"Could not connect to IB Gateway on {host}:{self.cfg.port}: {e}. If another "
                    f"app uses clientId {self.cfg.client_id}, set IBKR_CLIENT_ID. " + GATEWAY_HELP
                ) from e
            try:
                # 3 = delayed data when there is no live subscription; live data is still
                # returned where the account has a subscription.
                self.ib.reqMarketDataType(3)
            except Exception:
                pass
            print(
                f"[ibkr-paper-gateway] connected to {host}:{self.cfg.port} clientId={self.cfg.client_id}",
                file=sys.stderr,
                flush=True,
            )
            return self.ib

    def _safe_disconnect(self) -> None:
        try:
            if self.ib is not None:
                self.ib.disconnect()
        except Exception:
            pass

    def close(self) -> None:
        self._safe_disconnect()

    # -- fresh account list ----------------------------------------------
    async def fresh_accounts(self) -> list:
        ib = await self.ensure()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._acct_waiters.append(fut)
        try:
            ib.client.reqManagedAccts()
            return list(await asyncio.wait_for(fut, timeout=self.cfg.request_timeout))
        except (asyncio.TimeoutError, TimeoutError):
            raise PolicyError(
                "Refused: IB Gateway did not answer the managed-accounts request, so the paper "
                "check could not run."
            )
        finally:
            if fut in self._acct_waiters:
                self._acct_waiters.remove(fut)

    async def call(self, awaitable, what: str):
        try:
            return await asyncio.wait_for(awaitable, timeout=self.cfg.request_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            raise GatewayUnavailable(f"IB Gateway did not answer the {what} request in time.")
