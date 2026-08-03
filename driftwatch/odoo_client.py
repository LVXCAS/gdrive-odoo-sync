# -*- coding: utf-8 -*-
"""Odoo XML-RPC connection for DriftWatch.

WHY XML-RPC and not JSON-RPC or a REST client: Odoo Online (SaaS) 18.0
Enterprise cannot host custom addons, so there is no controller to add and no
``gdrive.*`` model on this instance -- there never will be. XML-RPC is the one
integration surface Odoo Online exposes out of the box
(``/xmlrpc/2/common`` and ``/xmlrpc/2/object``), and ``xmlrpc.client`` is
standard library, so this module adds no dependency. Every call this client
makes is therefore against a STANDARD model (``res.partner``, ``ir.attachment``,
``product.product``, ``crm.lead``, ...); there is no other kind available here.

WHY the domain-normalization helper exists, and why it gets a full paragraph:
``execute_kw(db, uid, key, model, method, args, kwargs)`` takes ``args`` as a
plain list whose first element IS the domain, e.g. ``args = [domain]``. If the
caller already has a domain wrapped one level too many --
``[[('state', '=', 'installed')]]`` instead of ``[('state', '=', 'installed')]``
-- then building ``args = [domain]`` double-wraps it to
``[[[('state', '=', 'installed')]]]``, and Odoo's expression parser dies deep
inside with ``IndexError: tuple index out of range``. Nothing in that traceback
mentions "domain" or "shape", so it re-derives the fix every time from scratch.
:func:`_normalize_domain` is the single place this is handled: it also turns
``None`` into ``[]`` (never ``[[]]``), because ``search_read`` with a bare
``[[]]`` domain fails the same way.

WHY a ``Fault`` is never retried: ``xmlrpc.client.Fault`` is Odoo's own RPC
layer telling the caller the call itself is wrong -- bad domain, bad field
name, an access-rights refusal, a validation error. None of those change on a
second attempt; retrying one just re-sends the same mistake and burns the
retry budget that a genuinely transient failure (a dropped connection, a 502
from the load balancer) needs.
"""
from __future__ import annotations

import random
import socket
import time
import xmlrpc.client
from typing import Any, Iterable, Optional

__all__ = ['OdooClient', 'OdooError', 'OdooAuthError', 'OdooConnectionError']


class OdooError(Exception):
    """Base class for every failure raised by this module."""


class OdooAuthError(OdooError):
    """``authenticate`` was refused (bad login/key/db). Never retryable: a
    wrong password does not become right on the fourth attempt."""


class OdooConnectionError(OdooError):
    """A transport-level call failed and the retry budget was exhausted."""


#: Transport-level exceptions worth retrying: dropped sockets, DNS hiccups,
#: connection resets, and Odoo's own ``ProtocolError`` (raised by
#: ``xmlrpc.client`` for a non-2xx HTTP response, including 5xx from the
#: reverse proxy in front of Odoo Online). ``socket.error`` is an alias for
#: ``OSError`` in Python 3, so this also covers ``ConnectionError`` and
#: ``TimeoutError``.
_TRANSIENT_EXCEPTIONS = (OSError, socket.error, xmlrpc.client.ProtocolError)

_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_BASE_DELAY = 0.5
_DEFAULT_MAX_DELAY = 8.0


def _normalize_domain(domain: Optional[list]) -> list:
    """Return ``domain`` in the plain shape ``execute_kw`` expects.

    See the module docstring for the double-wrap trap this exists to avoid.
    ``None`` becomes ``[]`` (match everything), never ``[[]]``.
    """
    if domain is None:
        return []
    return list(domain)


class OdooClient:
    """A minimal, read-oriented XML-RPC client against one Odoo database.

    Thread-confined, like :class:`~driftwatch.store.Store`: one instance per
    process is the intended usage. The two XML-RPC endpoints
    (``/xmlrpc/2/common`` for auth/introspection, ``/xmlrpc/2/object`` for
    model calls) are connected lazily and cached on the instance.
    """

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self._uid: Optional[int] = None
        self._common: Optional[xmlrpc.client.ServerProxy] = None
        self._models: Optional[xmlrpc.client.ServerProxy] = None

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #
    def _common_proxy(self) -> xmlrpc.client.ServerProxy:
        if self._common is None:
            self._common = xmlrpc.client.ServerProxy(
                f'{self.cfg.odoo_url}/xmlrpc/2/common', allow_none=True)
        return self._common

    def _models_proxy(self) -> xmlrpc.client.ServerProxy:
        if self._models is None:
            self._models = xmlrpc.client.ServerProxy(
                f'{self.cfg.odoo_url}/xmlrpc/2/object', allow_none=True)
        return self._models

    def _with_retry(self, fn, *, label: str,
                    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
                    base_delay: float = _DEFAULT_BASE_DELAY,
                    max_delay: float = _DEFAULT_MAX_DELAY):
        """Run ``fn`` with bounded exponential backoff plus jitter.

        Only :data:`_TRANSIENT_EXCEPTIONS` are retried. A ``xmlrpc.client.Fault``
        propagates on the first attempt -- see the module docstring for why.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return fn()
            except xmlrpc.client.Fault:
                raise
            except _TRANSIENT_EXCEPTIONS as exc:
                if attempt >= max_attempts:
                    # Never interpolate `exc` verbatim from an unknown transport
                    # exception into a message that might get logged next to
                    # `redacted()` output -- neither the URL nor the DB name is
                    # secret, but the API key must never ride along with it, so
                    # the message stays generic rather than embedding `str(exc)`
                    # from an object this module does not control.
                    raise OdooConnectionError(
                        f'{label} failed after {attempt} attempts against '
                        f'{self.cfg.odoo_url!r} (db={self.cfg.odoo_db!r}): '
                        f'{type(exc).__name__}') from exc
                delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                delay += random.uniform(0, delay * 0.25)
                time.sleep(delay)

    # ------------------------------------------------------------------ #
    # auth / introspection
    # ------------------------------------------------------------------ #
    def authenticate(self) -> int:
        """Return the authenticated uid, caching it after the first call."""
        if self._uid is not None:
            return self._uid
        common = self._common_proxy()
        uid = self._with_retry(
            lambda: common.authenticate(
                self.cfg.odoo_db, self.cfg.odoo_login, self.cfg.odoo_api_key, {}),
            label='authenticate')
        # `authenticate` returns `False` (not an exception) for a bad
        # login/key/db combination -- that is a permanent misconfiguration,
        # not a transient failure, so it is raised here rather than retried.
        if not uid:
            raise OdooAuthError(
                f'Odoo authentication was refused for login '
                f'{self.cfg.odoo_login!r} on db {self.cfg.odoo_db!r}.')
        self._uid = int(uid)
        return self._uid

    def version(self) -> dict:
        """Return Odoo's ``common.version()`` payload (server version, etc.)."""
        common = self._common_proxy()
        return self._with_retry(lambda: common.version(), label='version')

    # ------------------------------------------------------------------ #
    # model calls
    # ------------------------------------------------------------------ #
    def _execute_kw(self, model: str, method: str, args: list,
                    kwargs: Optional[dict] = None) -> Any:
        uid = self.authenticate()
        models = self._models_proxy()
        call_kwargs = kwargs or {}
        return self._with_retry(
            lambda: models.execute_kw(
                self.cfg.odoo_db, uid, self.cfg.odoo_api_key,
                model, method, args, call_kwargs),
            label=f'{model}.{method}')

    def search_read(self, model: str, domain: Optional[list] = None,
                    fields: Optional[list] = None, limit: int = 0,
                    offset: int = 0, order: Optional[str] = None) -> list:
        """One ``search_read`` call. ``limit=0`` means unlimited (Odoo treats
        a falsy limit as "no LIMIT clause"), matching this method's default."""
        domain = _normalize_domain(domain)
        call_kwargs: dict = {'offset': offset, 'limit': limit or 0}
        if fields is not None:
            call_kwargs['fields'] = list(fields)
        if order:
            call_kwargs['order'] = order
        result = self._execute_kw(model, 'search_read', [domain], call_kwargs)
        return list(result)

    def search_count(self, model: str, domain: Optional[list] = None) -> int:
        domain = _normalize_domain(domain)
        return int(self._execute_kw(model, 'search_count', [domain]))

    def fields_get(self, model: str, attributes: Optional[list] = None) -> dict:
        call_kwargs: dict = {}
        if attributes is not None:
            call_kwargs['attributes'] = list(attributes)
        return self._execute_kw(model, 'fields_get', [], call_kwargs)

    def model_exists(self, model: str) -> bool:
        """True if ``model`` is a real, installed Odoo model.

        Queries ``ir.model`` by name rather than calling something like
        ``fields_get`` on the candidate model directly, because the latter
        raises a ``Fault`` for an absent model -- this method must return a
        plain bool, never raise, for exactly the models that do not exist.
        """
        try:
            count = self.search_count('ir.model', [('model', '=', model)])
        except xmlrpc.client.Fault:
            return False
        return count > 0

    # ------------------------------------------------------------------ #
    # snapshot
    # ------------------------------------------------------------------ #
    def snapshot(self, model: str, domain: Optional[list] = None,
                fields: Optional[Iterable[str]] = None,
                limit: int = 0) -> dict:
        """Read a model's rows in one shot, in the shape the verifier consumes.

        ``id`` and ``write_date`` are always fetched in addition to whatever
        ``fields`` asks for, via exactly ONE ``search_read`` -- never a loop of
        per-row reads, which would turn a single flaky page into a partially
        stale snapshot with no way to tell which rows came from which moment.

        ``read_complete`` is False whenever ``limit`` truncated the result
        (there were more matching rows than were fetched). This distinction is
        the whole point of the field: a short read and a mass deletion produce
        an identical row count, and only ``read_complete`` tells them apart.
        """
        domain = _normalize_domain(domain)
        requested = list(fields or [])
        fetch_fields: list = []
        for f in ('id', 'write_date', *requested):
            if f not in fetch_fields:
                fetch_fields.append(f)

        # Total count first (cheap, no row data) so `read_complete` can be
        # computed against the true match count rather than guessing from
        # `len(rows) == limit`, which is ambiguous when they happen to be equal
        # by coincidence.
        count = self.search_count(model, domain)
        rows = self.search_read(model, domain, fetch_fields, limit=limit, order='id')

        max_write_date: Optional[str] = None
        for row in rows:
            wd = row.get('write_date')
            if wd and (max_write_date is None or wd > max_write_date):
                max_write_date = wd

        read_complete = True
        if limit and limit > 0:
            read_complete = count <= limit

        return {
            'rows': rows,
            'count': count,
            'max_write_date': max_write_date,
            'read_complete': read_complete,
        }

    # ------------------------------------------------------------------ #
    # safe logging
    # ------------------------------------------------------------------ #
    def redacted(self) -> dict:
        """Safe to log: never includes the API key."""
        return {
            'odoo_url': self.cfg.odoo_url,
            'odoo_db': self.cfg.odoo_db,
            'odoo_login': self.cfg.odoo_login,
            'odoo_api_key': '***' if self.cfg.odoo_api_key else '(unset)',
            'uid': self._uid,
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f'OdooClient({self.redacted()!r})'
