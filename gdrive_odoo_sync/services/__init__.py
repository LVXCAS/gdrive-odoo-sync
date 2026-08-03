# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Google API client layer — every byte that crosses the network.

WHY this package exists as a separate layer
-------------------------------------------
Model code and transport code fail for completely different reasons and need
completely different tests. A ``files.list`` that silently returns page 1 of 40
because ``nextPageToken`` was left out of the ``fields`` mask is a transport
bug; it has nothing to do with ``gdrive.node``. Isolating the transport means
lane F can test it against a stubbed HTTP layer with no database at all, and
means the ORM code never has to reason about ``HttpError.resp.status``.

Contract for everything in this package:

* It takes and returns plain dicts, lists and bytes. No recordsets.
* It imports nothing from ``..lib`` and nothing from ``..models``. The only
  Odoo imports permitted are ``odoo.exceptions`` and ``logging``.
* Every single network call goes through
  :func:`~.retry.execute_with_retry`. ``googleapiclient``'s built-in
  ``execute(num_retries=N)`` is not sufficient: it does not retry
  ``403 rateLimitExceeded``, which is precisely what Drive returns under load.
* Nothing here swallows an error. Failures are raised as the typed exceptions
  in :mod:`.errors` so callers can distinguish "retry later" from "your
  domain-wide delegation grant is wrong" — a distinction that determines
  whether the run is merely slow or whether ``complete_read`` must be set
  False and the delete planner disarmed.

WHY the re-exports below
------------------------
Model code should say ``from ..services import build_services, SheetsReader``
and never reach into a specific module. Note in particular that
``services/mimetypes.py`` deliberately shadows the standard library's
``mimetypes`` name *inside this package only*; Python 3 absolute imports make
that safe, but importing it via this package is what keeps it obviously
unambiguous at the call site.
"""

from .errors import (
    GDriveAuthError,
    GDriveError,
    GDriveExportTooLarge,
    GDriveIncompleteRead,
    GDrivePermanentError,
    GDriveQuotaError,
    GDriveScopeError,
    GDriveTokenInvalid,
    redact,
)
from .google_auth import SCOPES, build_credentials, load_service_account_info
from .google_client import build_services
from .retry import execute_with_retry
from .rate_limiter import TokenBucket
from .mimetypes import (
    EXPORT_MAP,
    classify,
    extension_for,
    is_folder,
    is_native,
    is_spreadsheet_blob,
)
from .drive_discovery import DriveDiscovery
from .drive_changes import DriveChanges
from .drive_download import DriveDownloader
from .sheets_reader import SheetsReader
from .xlsx_reader import XlsxReader

__all__ = [
    # Errors and log hygiene
    'GDriveError',
    'GDriveAuthError',
    'GDriveScopeError',
    'GDriveQuotaError',
    'GDrivePermanentError',
    'GDriveExportTooLarge',
    'GDriveTokenInvalid',
    'GDriveIncompleteRead',
    'redact',
    # Credentials and service construction
    'SCOPES',
    'load_service_account_info',
    'build_credentials',
    'build_services',
    # Transport policy
    'execute_with_retry',
    'TokenBucket',
    # Classification
    'classify',
    'EXPORT_MAP',
    'extension_for',
    'is_native',
    'is_folder',
    'is_spreadsheet_blob',
    # Readers
    'DriveDiscovery',
    'DriveChanges',
    'DriveDownloader',
    'SheetsReader',
    'XlsxReader',
]
