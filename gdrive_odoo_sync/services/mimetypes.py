"""MIME classification and the native-export map (lane B).

Pure, dependency-free, side-effect-free. Everything here is a lookup or a prefix
test, which is deliberate: classification is on the hot path of every discovered
file and it must be trivially unit-testable and impossible to get wrong twice.

The one structural fact this module encodes is that Drive has **two kinds of
file** and there is no unified way to read them:

* *Native* Google types (``application/vnd.google-apps.*``) have no bytes. They
  must be ``files.export``-ed to some other format, or read through their own API.
* *Blobs* (everything else) have bytes and must be ``files.get_media``-ed.

Calling the wrong one is not a soft failure: ``get_media`` on a native file returns
``403 Only files with binary content can be downloaded`` and ``export_media`` on a
blob returns ``403 Export only supports Docs Editors files``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = [
    'GOOGLE_APPS_PREFIX',
    'MIME_FOLDER',
    'MIME_SHORTCUT',
    'MIME_SPREADSHEET',
    'MIME_DOCUMENT',
    'MIME_PRESENTATION',
    'MIME_DRAWING',
    'MIME_PDF',
    'MIME_TEXT',
    'XLSX_MIMES',
    'XLS_MIMES',
    'NODE_TYPES',
    'EXPORT_MAP',
    'EXPORT_MAP_BY_MIME',
    'classify',
    'classify_shortcut_target',
    'is_native',
    'is_folder',
    'is_shortcut',
    'is_spreadsheet_blob',
    'is_legacy_xls',
    'is_native_spreadsheet',
    'extension_for',
    'export_targets_for',
    'filename_for',
    'yields_datasets',
    'is_content_bearing',
]

GOOGLE_APPS_PREFIX = 'application/vnd.google-apps.'

MIME_FOLDER = 'application/vnd.google-apps.folder'
MIME_SHORTCUT = 'application/vnd.google-apps.shortcut'
MIME_SPREADSHEET = 'application/vnd.google-apps.spreadsheet'
MIME_DOCUMENT = 'application/vnd.google-apps.document'
MIME_PRESENTATION = 'application/vnd.google-apps.presentation'
MIME_DRAWING = 'application/vnd.google-apps.drawing'

MIME_PDF = 'application/pdf'
MIME_TEXT = 'text/plain'

#: Uploaded Excel workbooks. openpyxl reads all of these; ``.xlsm`` differs from
#: ``.xlsx`` only by carrying a VBA part, which ``read_only`` mode ignores.
XLSX_MIMES = frozenset({
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.ms-excel.sheet.macroEnabled.12',
    'application/vnd.ms-excel.sheet.macroenabled.12',
    'application/vnd.ms-excel.template.macroEnabled.12',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.template',
})

#: Legacy BIFF workbooks. openpyxl cannot read these at all and v1 does not ship
#: ``xlrd``; the node is recorded and skipped with ``unsupported_mime``.
XLS_MIMES = frozenset({
    'application/vnd.ms-excel',
    'application/msexcel',
    'application/x-msexcel',
    'application/x-excel',
})

#: Exactly the ``gdrive.node.node_type`` selection keys from SPEC §3.4.
NODE_TYPES = (
    'folder',
    'spreadsheet',
    'document',
    'presentation',
    'drawing',
    'blob',
    'shortcut',
    'other_google',
)

#: Native Google types that can be exported, keyed by ``node_type``.
#:
#: **Native Sheets are deliberately absent.** ``files.export`` hard-fails at 10 MB
#: with ``exportSizeLimitExceeded`` and chunked download does not help (the limit
#: is on the generated artefact). Worse, exporting a multi-tab Sheet to ``text/csv``
#: silently returns *only the first tab* — a data-loss bug that produces no error
#: and no warning. Sheets content is read through the Sheets API instead (SPEC
#: §3.4, §4.6). An archival ``.xlsx`` snapshot is available behind the opt-in
#: ``gdrive.connection.mirror_sheet_snapshot`` flag and is handled by lane D, not
#: by this map.
EXPORT_MAP: Mapping[str, Mapping[str, Optional[str]]] = {
    'document': {'primary': MIME_PDF, 'secondary': MIME_TEXT},
    'presentation': {'primary': MIME_PDF, 'secondary': None},
    'drawing': {'primary': MIME_PDF, 'secondary': None},
}

#: The same map keyed by the source Drive MIME, for call sites that have the raw
#: ``mimeType`` and not the classified node type.
EXPORT_MAP_BY_MIME: Mapping[str, Mapping[str, Optional[str]]] = {
    MIME_DOCUMENT: EXPORT_MAP['document'],
    MIME_PRESENTATION: EXPORT_MAP['presentation'],
    MIME_DRAWING: EXPORT_MAP['drawing'],
}

#: Node types produced by native Google MIME types other than the four handled
#: above (forms, scripts, sites, maps, jamboards, fusiontables…).
_NATIVE_NODE_TYPES = {
    MIME_FOLDER: 'folder',
    MIME_SHORTCUT: 'shortcut',
    MIME_SPREADSHEET: 'spreadsheet',
    MIME_DOCUMENT: 'document',
    MIME_PRESENTATION: 'presentation',
    MIME_DRAWING: 'drawing',
}

#: Extension table for attachment naming. Explicit rather than
#: ``mimetypes.guess_extension`` because that function is (a) shadowed by this
#: module's own name in some import layouts and (b) non-deterministic across
#: Python versions for ``image/jpeg`` (``.jpe`` vs ``.jpg``), which would rename
#: every attachment on an interpreter upgrade.
_EXTENSIONS = {
    MIME_PDF: '.pdf',
    MIME_TEXT: '.txt',
    'text/csv': '.csv',
    'text/html': '.html',
    'text/markdown': '.md',
    'application/rtf': '.rtf',
    'application/json': '.json',
    'application/xml': '.xml',
    'application/zip': '.zip',
    'application/gzip': '.gz',
    'application/x-tar': '.tar',
    'application/octet-stream': '.bin',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.template': '.xltx',
    'application/vnd.ms-excel.sheet.macroEnabled.12': '.xlsm',
    'application/vnd.ms-excel.sheet.macroenabled.12': '.xlsm',
    'application/vnd.ms-excel.template.macroEnabled.12': '.xltm',
    'application/vnd.ms-excel': '.xls',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/msword': '.doc',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
    'application/vnd.ms-powerpoint': '.ppt',
    'application/vnd.oasis.opendocument.spreadsheet': '.ods',
    'application/vnd.oasis.opendocument.text': '.odt',
    'application/vnd.oasis.opendocument.presentation': '.odp',
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'image/svg+xml': '.svg',
    'image/tiff': '.tif',
    'image/heic': '.heic',
    'image/bmp': '.bmp',
    'audio/mpeg': '.mp3',
    'audio/wav': '.wav',
    'video/mp4': '.mp4',
    'video/quicktime': '.mov',
    'video/x-msvideo': '.avi',
}


def _mime(value: Any) -> str:
    """Normalize a Drive ``mimeType`` for comparison.

    Drive occasionally returns a parameterised type (``text/csv; charset=utf-8``).
    Case is normalized because ``macroEnabled`` is spelled with different casing by
    different uploaders.
    """
    if not value:
        return ''
    s = str(value).split(';', 1)[0].strip()
    return s


def is_native(mime: Any) -> bool:
    """True for ``application/vnd.google-apps.*`` — a file with no bytes.

    This single prefix test is the branch that decides ``export_media`` vs
    ``get_media`` (SPEC §4.5). Getting it wrong yields a 403 on every file.
    """
    return _mime(mime).startswith(GOOGLE_APPS_PREFIX)


def is_folder(mime: Any) -> bool:
    """True for the Drive folder pseudo-type."""
    return _mime(mime) == MIME_FOLDER


def is_shortcut(mime: Any) -> bool:
    """True for a Drive shortcut. Shortcuts are recorded but never ingested."""
    return _mime(mime) == MIME_SHORTCUT


def is_native_spreadsheet(mime: Any) -> bool:
    """True for a native Google Sheet — read via the Sheets API, never exported."""
    return _mime(mime) == MIME_SPREADSHEET


def is_spreadsheet_blob(mime: Any) -> bool:
    """True for an uploaded ``.xlsx`` / ``.xlsm``: a blob that also yields datasets.

    These are the files that get **both** an ``ir.attachment`` (the bytes) and a
    set of ``gdrive.dataset`` records (the parsed tabs), because the Sheets API
    cannot read them — an uploaded xlsx has no ``spreadsheetId`` and
    ``spreadsheets.get`` on its file id returns 404 (SPEC §4.7).
    """
    return _mime(mime) in XLSX_MIMES


def is_legacy_xls(mime: Any) -> bool:
    """True for legacy BIFF ``.xls``, which v1 skips as ``unsupported_mime``."""
    return _mime(mime) in XLS_MIMES


def classify(mime: Any, shortcut_details: Optional[Mapping[str, Any]] = None) -> str:
    """Map a Drive ``mimeType`` onto a ``gdrive.node.node_type`` value.

    :param mime: the raw ``file['mimeType']`` from the Drive API.
    :param shortcut_details: the raw ``file['shortcutDetails']``, when present.
        Accepted so a caller can pass the whole Drive record's field through
        without a conditional; its presence alone marks the node as a shortcut
        even in the (observed) case where Drive returns the *target's* MIME type
        on the shortcut record itself.
    :returns: one of :data:`NODE_TYPES`.

    Classification never guesses from the file *name*. A file called
    ``budget.xlsx`` that Drive reports as ``application/pdf`` is a PDF; the
    extension is a user-editable display string and the MIME type is the fact.
    """
    m = _mime(mime)
    if shortcut_details:
        return 'shortcut'
    if m in _NATIVE_NODE_TYPES:
        return _NATIVE_NODE_TYPES[m]
    if m.startswith(GOOGLE_APPS_PREFIX):
        # Forms, scripts, sites, maps, jamboards, fusiontables, unknown future
        # types. Metadata only — there is no export target we can rely on.
        return 'other_google'
    return 'blob'


def classify_shortcut_target(shortcut_details: Optional[Mapping[str, Any]]) -> str:
    """Classify what a shortcut points at, from ``shortcutDetails.targetMimeType``.

    Used by lane D to decide whether the *target* is worth resolving before the
    shortcut's own record is finalized. Returns ``'other_google'`` when the target
    MIME is absent, which is the conservative answer: it ingests nothing.
    """
    if not shortcut_details:
        return 'other_google'
    target_mime = shortcut_details.get('targetMimeType')
    if not target_mime:
        return 'other_google'
    return classify(target_mime, None)


def export_targets_for(node_type: str) -> Mapping[str, Optional[str]]:
    """Return ``{'primary': mime|None, 'secondary': mime|None}`` for a node type.

    Returns both keys set to ``None`` for anything not in :data:`EXPORT_MAP`,
    including ``spreadsheet`` — see the note on that constant for why.
    """
    return EXPORT_MAP.get(node_type, {'primary': None, 'secondary': None})


def extension_for(mime: Any) -> str:
    """Return the canonical file extension (with dot) for ``mime``, or ``''``.

    Used to append a correct extension to the Drive title when building the
    ``ir.attachment`` name, so a user downloading a mirrored file gets something
    their OS can open. Native types map to their *export* extension because the
    attachment holds exported bytes, not the native document.
    """
    m = _mime(mime)
    if m in _EXTENSIONS:
        return _EXTENSIONS[m]
    if m in (MIME_DOCUMENT, MIME_PRESENTATION, MIME_DRAWING):
        return '.pdf'
    if m == MIME_SPREADSHEET:
        return '.xlsx'
    return ''


def filename_for(title: Any, mime: Any) -> str:
    """Build an attachment filename from a Drive title and the effective MIME.

    Appends the extension only when the title does not already end with it, so
    ``Cashflow.xlsx`` does not become ``Cashflow.xlsx.xlsx``. The comparison is
    case-insensitive because Drive titles are user-typed.
    """
    name = (str(title) if title is not None else '').strip() or 'untitled'
    ext = extension_for(mime)
    if ext and not name.lower().endswith(ext.lower()):
        name = name + ext
    return name


def yields_datasets(node_type: str, mime: Any) -> bool:
    """True when this node produces ``gdrive.dataset`` records (spreadsheet tabs).

    Two disjoint sources: native Sheets (read via the Sheets API) and uploaded
    xlsx blobs (downloaded and parsed with openpyxl). Legacy ``.xls`` returns
    False — it is skipped, not silently treated as empty.
    """
    if node_type == 'spreadsheet':
        return True
    if node_type == 'blob' and is_spreadsheet_blob(mime):
        return True
    return False


def is_content_bearing(node_type: str) -> bool:
    """True when the node has bytes worth mirroring into an ``ir.attachment``.

    Folders have no content; shortcuts are pointers and are resolved instead of
    ingested; ``other_google`` types have no reliable export target.
    """
    return node_type in {'blob', 'document', 'presentation', 'drawing'}
