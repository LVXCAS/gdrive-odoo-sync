"""Binary download and native export (lane B).

There is **no unified download call in Drive**. Which method you must use is
decided entirely by the MIME prefix (SPEC §4.5)::

    is_native = mime.startswith('application/vnd.google-apps.')

* ``get_media()`` on a native file → ``403 Only files with binary content can be
  downloaded``.
* ``export_media()`` on a binary → ``403 Export only supports Docs Editors files``.

Two further asymmetries that are easy to get wrong and expensive to debug:

* ``files.export`` accepts **no** ``supportsAllDrives`` parameter and no range
  requests. Passing ``supportsAllDrives=True`` to it is a ``400``.
* The export size ceiling (10 MB) applies to the **generated artefact**, not the
  transfer, so chunking does not help. ``403 exportSizeLimitExceeded`` is terminal
  in v1; the ``files.download`` long-running-operation path is the v2 remedy.
"""

from __future__ import annotations

import io
import logging
from typing import Any, Optional, Tuple

from .errors import (
    CODE_FOLDER,
    CODE_NO_DOWNLOAD_PERMISSION,
    CODE_SHORTCUT,
    CODE_TOO_LARGE,
    CODE_UNSUPPORTED_MIME,
    GDriveExportTooLarge,
    GDrivePermanentError,
)
from .google_client import ConnectionContext
from .mimetypes import (
    MIME_SPREADSHEET,
    classify,
    export_targets_for,
    is_folder,
    is_legacy_xls,
    is_native,
    is_shortcut,
)
from .retry import DEFAULT_MAX_ATTEMPTS, execute_with_retry

_logger = logging.getLogger(__name__)

__all__ = ['DriveDownloader', 'DEFAULT_CHUNK_SIZE', 'DEFAULT_MAX_BLOB_BYTES']

#: 10 MB chunks. Large enough that a 100 MB file is 10 round trips rather than
#: 100; small enough that a chunk failure costs 10 MB of re-transfer, and small
#: enough to keep peak memory per worker bounded and predictable.
DEFAULT_CHUNK_SIZE = 10 * 1024 * 1024

#: Matches ``gdrive.connection.max_blob_bytes`` (SPEC §3.1). Files above this are
#: recorded with full metadata and ``state='skipped'``, reason ``too_large`` —
#: recorded, never silently ignored, so the omission is auditable.
DEFAULT_MAX_BLOB_BYTES = 104857600

#: Downloading is the most quota-expensive Drive operation; charge the bucket more.
COST_MEDIA = 3


class DriveDownloader:
    """Fetch file bytes, branching correctly between blob download and export.

    :param drive: a built Drive v3 service belonging to the calling thread.
    :param ctx: the :class:`~.google_client.ConnectionContext`, supplying
        ``max_blob_bytes``, the retry budget and the rate limiter.
    :param max_blob_bytes: overrides ``ctx.max_blob_bytes``.
    :param chunk_size: media download chunk size in bytes.
    """

    def __init__(
        self,
        drive: Any,
        ctx: Optional[ConnectionContext] = None,
        limiter: Any = None,
        max_blob_bytes: Optional[int] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.drive = drive
        self.ctx = ctx
        self.limiter = limiter if limiter is not None else (ctx.drive_bucket() if ctx else None)
        self.max_blob_bytes = int(
            max_blob_bytes
            if max_blob_bytes is not None
            else getattr(ctx, 'max_blob_bytes', DEFAULT_MAX_BLOB_BYTES) or DEFAULT_MAX_BLOB_BYTES
        )
        self.chunk_size = int(chunk_size or DEFAULT_CHUNK_SIZE)
        self.max_attempts = int(getattr(ctx, 'max_retry_attempts', DEFAULT_MAX_ATTEMPTS) or
                                DEFAULT_MAX_ATTEMPTS)
        self.log = logger or _logger
        self.requests_made = 0
        self.units_used = 0
        self.bytes_downloaded = 0

    # -- guards ----------------------------------------------------------- #

    def _check_downloadable(
        self, file_id: str, mime: str, size_bytes: Optional[int], can_download: bool
    ) -> None:
        """Refuse, with a ``skip_reason``-shaped code, before spending a request.

        Every ``code`` raised here is byte-identical to a
        ``gdrive.node.skip_reason`` selection key, so lane D writes
        ``node.skip_reason = exc.code`` with no translation table.
        """
        if is_folder(mime):
            raise GDrivePermanentError(
                'Refusing to download folder %s: folders have no content.' % file_id,
                code=CODE_FOLDER,
            )
        if is_shortcut(mime):
            raise GDrivePermanentError(
                'Refusing to download shortcut %s: shortcuts are resolved to their '
                'target and never ingested themselves.' % file_id,
                code=CODE_SHORTCUT,
            )
        if not can_download:
            raise GDrivePermanentError(
                'Drive reports capabilities.canDownload = false for %s (the file is '
                'view-only or download-restricted by its owner).' % file_id,
                code=CODE_NO_DOWNLOAD_PERMISSION,
            )
        if size_bytes is not None and self.max_blob_bytes and int(size_bytes) > self.max_blob_bytes:
            raise GDrivePermanentError(
                'File %s is %d bytes, above the %d-byte limit for this connection; '
                'recorded but not downloaded.' % (file_id, int(size_bytes), self.max_blob_bytes),
                code=CODE_TOO_LARGE,
                details={'size_bytes': int(size_bytes), 'limit': self.max_blob_bytes},
            )

    # -- blob path -------------------------------------------------------- #

    def fetch_blob(
        self,
        file_id: str,
        mime: str = '',
        size_bytes: Optional[int] = None,
        can_download: bool = True,
    ) -> bytes:
        """Stream a binary file's bytes via ``files.get_media``.

        Uses ``MediaIoBaseDownload`` with :data:`DEFAULT_CHUNK_SIZE` chunks, each
        chunk individually retried. The size limit is re-checked **during** the
        transfer as well as before it, because Drive's reported ``size`` is absent
        for some uploads and wrong for others; without the in-flight check a
        mis-reported 4 GB video would be buffered entirely into RAM.
        """
        self._check_downloadable(file_id, mime, size_bytes, can_download)
        try:  # pragma: no cover - environment dependent
            from googleapiclient.http import MediaIoBaseDownload
        except Exception as exc:  # pragma: no cover
            raise GDrivePermanentError(
                'google-api-python-client is not installed; cannot download media.',
                code='missing_dependency',
            ) from exc

        request = self.drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request, chunksize=self.chunk_size)
        done = False
        chunk_index = 0
        while not done:
            chunk_index += 1
            self.requests_made += 1
            self.units_used += COST_MEDIA
            # next_chunk() is not an HttpRequest, so the callable form of the
            # retry wrapper is used. num_retries=0 keeps retry policy in one place.
            _status, done = execute_with_retry(
                lambda: downloader.next_chunk(num_retries=0),
                max_attempts=self.max_attempts,
                label='files.get_media(%s) chunk=%d' % (file_id, chunk_index),
                limiter=self.limiter,
                cost=COST_MEDIA,
            )
            if self.max_blob_bytes and buffer.tell() > self.max_blob_bytes:
                raise GDrivePermanentError(
                    'File %s exceeded the %d-byte limit mid-transfer (Drive reported '
                    'size=%r); aborting the download.'
                    % (file_id, self.max_blob_bytes, size_bytes),
                    code=CODE_TOO_LARGE,
                    details={'limit': self.max_blob_bytes, 'reported_size': size_bytes},
                )
        data = buffer.getvalue()
        buffer.close()
        self.bytes_downloaded += len(data)
        self.log.debug('Downloaded %d bytes for file %s in %d chunk(s).',
                       len(data), file_id, chunk_index)
        return data

    # -- native path ------------------------------------------------------ #

    def export(self, file_id: str, export_mime: str) -> bytes:
        """Export a native Google file to ``export_mime`` via ``files.export_media``.

        :raises GDriveExportTooLarge: on ``403 exportSizeLimitExceeded``. The
            caller records ``EXPORT_SIZE_LIMIT`` and marks the node skipped; it
            must **not** retry, and must not fall back to a chunked download,
            because the ceiling is on the artefact rather than the transfer.

        Note the absent ``supportsAllDrives``: ``export_media`` does not accept it.
        This is not an oversight to be "fixed"; adding it returns ``400``.
        """
        request = self.drive.files().export_media(fileId=file_id, mimeType=export_mime)
        self.requests_made += 1
        self.units_used += COST_MEDIA
        try:
            data = execute_with_retry(
                request,
                max_attempts=self.max_attempts,
                label='files.export_media(%s -> %s)' % (file_id, export_mime),
                limiter=self.limiter,
                cost=COST_MEDIA,
            )
        except GDriveExportTooLarge:
            self.log.warning(
                'Export of %s to %s exceeded the 10 MB Drive export limit; the node '
                'will be recorded as skipped. Chunked download does not help: the '
                'limit is on the generated artefact.', file_id, export_mime,
            )
            raise
        if isinstance(data, str):  # pragma: no cover - transport dependent
            data = data.encode('utf-8')
        data = data or b''
        self.bytes_downloaded += len(data)
        return data

    # -- the dispatcher --------------------------------------------------- #

    def fetch(
        self,
        file_id: str,
        mime: str,
        size_bytes: Optional[int] = None,
        can_download: bool = True,
        export_mime: Optional[str] = None,
    ) -> Tuple[bytes, str]:
        """Fetch content for ``file_id`` and return ``(bytes, effective_mime)``.

        ``effective_mime`` is what the bytes actually are — the source MIME for a
        blob, the export MIME for a native document. Lane D writes it to
        ``ir.attachment.mimetype`` and uses it to pick the filename extension,
        which is why it must be returned rather than inferred again.

        :param export_mime: override the export target. Ignored for blobs.
        :raises GDrivePermanentError: for anything that can never be fetched —
            folders, shortcuts, native Sheets, legacy ``.xls``, ``other_google``
            types, download-restricted files and oversize files. The ``code`` maps
            onto ``gdrive.node.skip_reason``.
        """
        node_type = classify(mime, None)

        if not is_native(mime):
            if is_legacy_xls(mime):
                raise GDrivePermanentError(
                    'Legacy .xls workbook %s is not supported in v1 (openpyxl reads '
                    'only the OOXML format). Ask the owner to re-save it as .xlsx.'
                    % file_id,
                    code=CODE_UNSUPPORTED_MIME,
                )
            data = self.fetch_blob(file_id, mime, size_bytes, can_download)
            return data, mime

        if mime == MIME_SPREADSHEET:
            # Structural refusal, not a policy check. Exporting a native Sheet to
            # CSV silently returns only the FIRST TAB, and to xlsx it hard-fails
            # above 10 MB. Sheet content is read through the Sheets API, which has
            # neither limitation (SPEC §3.4).
            raise GDrivePermanentError(
                'Refusing to export native Google Sheet %s. Its content is read '
                'through the Sheets API; exporting truncates multi-tab workbooks '
                'to the first tab and fails above 10 MB.' % file_id,
                code=CODE_UNSUPPORTED_MIME,
                details={'node_type': node_type},
            )

        target = export_mime or export_targets_for(node_type).get('primary')
        if not target:
            raise GDrivePermanentError(
                'No export format is defined for %s (%s); recorded as metadata only.'
                % (file_id, mime),
                code=CODE_UNSUPPORTED_MIME,
                details={'node_type': node_type},
            )
        if not can_download:
            raise GDrivePermanentError(
                'Drive reports capabilities.canDownload = false for %s.' % file_id,
                code=CODE_NO_DOWNLOAD_PERMISSION,
            )
        return self.export(file_id, target), target

    def fetch_secondary(self, file_id: str, mime: str) -> Optional[Tuple[bytes, str]]:
        """Fetch the optional secondary export (plain text for Google Docs).

        Returns ``None`` when the node type has no secondary target, so the caller
        can write ``node.text_attachment_id`` conditionally without a type test.
        Export size failures are downgraded to a warning here: the *primary* PDF
        is the deliverable, and losing the searchable-text companion is not worth
        failing the node over.
        """
        node_type = classify(mime, None)
        target = export_targets_for(node_type).get('secondary')
        if not target:
            return None
        try:
            return self.export(file_id, target), target
        except GDriveExportTooLarge:
            self.log.warning(
                'Secondary %s export of %s exceeded the export limit; keeping the '
                'primary export only.', target, file_id,
            )
            return None

    def should_skip_unchanged(
        self, md5_checksum: Optional[str], stored_md5: Optional[str], has_attachment: bool
    ) -> bool:
        """True when the blob is provably unchanged and already mirrored.

        SPEC §4.5: a blob whose ``md5Checksum`` equals the stored one **and** which
        already has an attachment is skipped without downloading. Both conditions
        are required — a matching checksum with no attachment means a previous run
        recorded metadata and then failed before writing content.

        Always False when either checksum is absent, which is the case for **every
        native Google type**: ``md5Checksum`` is blob-only. Using it as a
        change-detector for Sheets would mean never detecting a change at all.
        """
        return bool(md5_checksum and stored_md5 and md5_checksum == stored_md5 and has_attachment)

    def stats(self) -> dict:
        """Counters for ``gdrive.sync.run`` quota accounting."""
        return {
            'requests_made': self.requests_made,
            'drive_units_used': self.units_used,
            'bytes_downloaded': self.bytes_downloaded,
        }
