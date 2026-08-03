# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. Licensed under LGPL-3.
"""Lane B — credentials, impersonation and log hygiene (SPEC §2.3, §2.5, §7.4).

Fully mocked: no network, no Google account, no key on disk.

WHY the ``with_subject`` test is the most important test in this file
====================================================================
``base_creds.with_subject(subject)`` returns a **new** credentials object; it
does not mutate the receiver. Writing::

    base_creds.with_subject(connection.subject_email)   # no assignment

is a silent no-op that leaves the code authenticating as the bare service
account. The service account is a separate principal whose own Drive is empty,
so every subsequent ``files.list`` returns ``{'files': []}`` — with **HTTP 200
and no error**. That is indistinguishable from "the user deleted everything",
and it is the reason SPEC §9.6 gives deletes a materially higher evidence bar
than creates. One missing assignment operator is enough to arm a mass-delete.

The redaction tests exist for a symmetric reason: ``google.auth`` includes the
signed JWT assertion — built from the private key — in some exception strings.
A single unguarded ``_logger.error("%s", exc)`` would write a usable bearer
credential for an entire Google Drive into a downloadable log file.
"""

import json
import os
from unittest import mock

from odoo.tests.common import BaseCase

from odoo.addons.gdrive_odoo_sync.services import google_auth
from odoo.addons.gdrive_odoo_sync.services.errors import (
    GDriveAuthError,
    GDriveScopeError,
    redact,
)
from odoo.addons.gdrive_odoo_sync.services.google_auth import (
    SCOPES,
    SCOPES_STRING,
    build_credentials,
    key_summary,
    load_service_account_info,
    parse_service_account_info,
    refresh_credentials,
)

FAKE_PRIVATE_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDsuperSecretBytes\n"
    "-----END PRIVATE KEY-----\n"
)

GOOD_KEY = {
    "type": "service_account",
    "project_id": "avatar-gdrive-sync",
    "client_email": "gdrive-odoo-sync@avatar-gdrive-sync.iam.gserviceaccount.com",
    "client_id": "104729384756102938475",
    "private_key": FAKE_PRIVATE_KEY,
    "private_key_id": "abc123",
    "token_uri": "https://oauth2.googleapis.com/token",
}


class _FakeCreds:
    """Stands in for ``google.oauth2.service_account.Credentials``."""

    def __init__(self, info, scopes, subject=None):
        self.info = dict(info)
        self.scopes = list(scopes or ())
        self.subject = subject
        self.refresh_calls = 0
        self.refresh_error = None

    def with_subject(self, subject):
        """Return a NEW object, exactly as google-auth does."""
        clone = _FakeCreds(self.info, self.scopes, subject)
        clone.refresh_error = self.refresh_error
        return clone

    def refresh(self, request):
        self.refresh_calls += 1
        if self.refresh_error is not None:
            raise self.refresh_error


class _MutatingCreds(_FakeCreds):
    """A deliberately broken google-auth: ``with_subject`` mutates in place."""

    def with_subject(self, subject):
        self.subject = subject
        return self


class _FakeServiceAccountModule:
    def __init__(self, creds_cls=_FakeCreds):
        self._creds_cls = creds_cls
        self.calls = []
        module = self

        class Credentials:
            @staticmethod
            def from_service_account_info(info, scopes=None):
                module.calls.append({"info": info, "scopes": list(scopes or ())})
                return module._creds_cls(info, scopes)

        self.Credentials = Credentials


class _FakeConfigParameter:
    def __init__(self, values, sudo_required=True):
        self._values = values
        self._sudo_required = sudo_required
        self._sudoed = False
        self.sudo_calls = 0

    def sudo(self):
        self.sudo_calls += 1
        clone = _FakeConfigParameter(self._values, self._sudo_required)
        clone._sudoed = True
        clone.sudo_calls = self.sudo_calls
        return clone

    def get_param(self, key, default=None):
        if self._sudo_required and not self._sudoed:
            # ir.config_parameter is restricted to base.group_system; without
            # sudo a cron running as anything less gets AccessError.
            raise PermissionError("AccessError: ir.config_parameter requires sudo")
        return self._values.get(key, default)


class _FakeEnv:
    def __init__(self, params):
        self.config_parameter = _FakeConfigParameter(params)

    def __getitem__(self, model):
        if model == "ir.config_parameter":
            return self.config_parameter
        raise KeyError(model)


class _Connection:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.subject_email = kw.get("subject_email", "lucaso@avatarnaturalfoods.com")
        self.auth_mode = kw.get("auth_mode", "dwd")
        self.sa_key_env_var = kw.get("sa_key_env_var", "GDRIVE_ODOO_SYNC_SA_KEY")
        self.sa_key_param_key = kw.get("sa_key_param_key", "gdrive_odoo_sync.sa_key_json")


class TestScopeDiscipline(BaseCase):
    """SPEC §2.2 — the frozen read-only pair, and nothing else."""

    def test_exactly_two_scopes(self):
        self.assertEqual(len(SCOPES), 2)

    def test_both_are_readonly(self):
        self.assertIn("https://www.googleapis.com/auth/drive.readonly", SCOPES)
        self.assertIn("https://www.googleapis.com/auth/spreadsheets.readonly", SCOPES)
        for scope in SCOPES:
            self.assertTrue(scope.endswith(".readonly"))

    def test_no_write_scope_is_reachable(self):
        # v1 structurally cannot damage Drive. This is a safety property, not a
        # feature gap, and it is why _sync_id write-back is unavailable.
        joined = SCOPES_STRING
        self.assertNotIn("drive.file", joined)
        self.assertNotIn("auth/drive,", joined + ",")
        self.assertEqual(joined, ",".join(SCOPES))


class TestKeyParsing(BaseCase):
    """Probe P1 — structural validation, with actionable messages."""

    def test_valid_key_parses_from_text(self):
        info = parse_service_account_info(json.dumps(GOOD_KEY))
        self.assertEqual(info["client_id"], GOOD_KEY["client_id"])

    def test_valid_key_parses_from_dict(self):
        self.assertEqual(parse_service_account_info(GOOD_KEY)["client_email"],
                         GOOD_KEY["client_email"])

    def test_empty_raises_with_setup_instructions(self):
        with self.assertRaises(GDriveAuthError) as ctx:
            parse_service_account_info("")
        self.assertIn("service account", str(ctx.exception).lower())

    def test_malformed_json_raises(self):
        with self.assertRaises(GDriveAuthError):
            parse_service_account_info("{not json")

    def test_oauth_client_secret_file_is_rejected_by_type(self):
        # A very common mistake: downloading the OAuth client secret instead of
        # a service-account key. Both are JSON with a client_id in them.
        wrong = dict(GOOD_KEY, type="authorized_user")
        with self.assertRaises(GDriveAuthError) as ctx:
            parse_service_account_info(wrong)
        self.assertIn("service account", str(ctx.exception).lower())

    def test_missing_required_fields_are_named(self):
        for field in ("client_email", "client_id", "private_key"):
            with self.subTest(missing=field):
                truncated = {k: v for k, v in GOOD_KEY.items() if k != field}
                with self.assertRaises(GDriveAuthError) as ctx:
                    parse_service_account_info(truncated)
                self.assertIn(field, str(ctx.exception))

    def test_parse_errors_never_leak_the_private_key(self):
        broken = dict(GOOD_KEY)
        broken.pop("client_id")
        with self.assertRaises(GDriveAuthError) as ctx:
            parse_service_account_info(broken)
        self.assertNotIn("superSecretBytes", str(ctx.exception))

    def test_key_summary_omits_the_secret(self):
        summary = key_summary(GOOD_KEY)
        self.assertEqual(summary["client_id"], GOOD_KEY["client_id"])
        self.assertNotIn("private_key", summary)
        self.assertNotIn("superSecretBytes", json.dumps(summary))


class TestKeyResolutionOrder(BaseCase):
    """SPEC §2.5 — env var first, ``ir.config_parameter`` second, both sudo-safe."""

    def setUp(self):
        super().setUp()
        self._saved = os.environ.pop("GDRIVE_ODOO_SYNC_SA_KEY", None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.pop("GDRIVE_ODOO_SYNC_SA_KEY", None)
        if self._saved is not None:
            os.environ["GDRIVE_ODOO_SYNC_SA_KEY"] = self._saved

    def test_environment_variable_wins(self):
        env_key = dict(GOOD_KEY, client_id="111111111111111111111")
        param_key = dict(GOOD_KEY, client_id="222222222222222222222")
        os.environ["GDRIVE_ODOO_SYNC_SA_KEY"] = json.dumps(env_key)
        env = _FakeEnv({"gdrive_odoo_sync.sa_key_json": json.dumps(param_key)})

        info = load_service_account_info(env, _Connection())
        self.assertEqual(info["client_id"], "111111111111111111111")

    def test_falls_back_to_config_parameter(self):
        env = _FakeEnv({"gdrive_odoo_sync.sa_key_json": json.dumps(GOOD_KEY)})
        info = load_service_account_info(env, _Connection())
        self.assertEqual(info["client_id"], GOOD_KEY["client_id"])

    def test_config_parameter_access_uses_sudo(self):
        # Without .sudo() a cron user without base.group_system gets AccessError,
        # which surfaces as "the sync stopped working" long after deployment.
        env = _FakeEnv({"gdrive_odoo_sync.sa_key_json": json.dumps(GOOD_KEY)})
        load_service_account_info(env, _Connection())
        self.assertGreaterEqual(env.config_parameter.sudo_calls, 1)

    def test_blank_environment_variable_does_not_shadow_the_parameter(self):
        os.environ["GDRIVE_ODOO_SYNC_SA_KEY"] = "   "
        env = _FakeEnv({"gdrive_odoo_sync.sa_key_json": json.dumps(GOOD_KEY)})
        self.assertEqual(
            load_service_account_info(env, _Connection())["client_id"],
            GOOD_KEY["client_id"],
        )

    def test_custom_env_var_name_is_honoured(self):
        os.environ["MY_OWN_KEY"] = json.dumps(GOOD_KEY)
        self.addCleanup(os.environ.pop, "MY_OWN_KEY", None)
        conn = _Connection(sa_key_env_var="MY_OWN_KEY")
        self.assertEqual(
            load_service_account_info(None, conn)["client_id"], GOOD_KEY["client_id"]
        )

    def test_nothing_configured_raises_with_instructions(self):
        env = _FakeEnv({})
        with self.assertRaises(Exception) as ctx:
            load_service_account_info(env, _Connection())
        self.assertIn("GDRIVE_ODOO_SYNC_SA_KEY", str(ctx.exception))


class TestImpersonation(BaseCase):
    """SPEC §2.3 — the return value of ``with_subject`` must be captured."""

    def test_with_subject_returns_a_new_object(self):
        fake = _FakeServiceAccountModule()
        with mock.patch.object(google_auth, "_service_account", fake):
            creds = build_credentials(GOOD_KEY, subject="lucaso@avatarnaturalfoods.com")
        self.assertEqual(creds.subject, "lucaso@avatarnaturalfoods.com")

    def test_returned_credentials_are_not_the_base_credentials(self):
        captured = {}
        original = _FakeCreds.with_subject

        def spy(self, subject):
            result = original(self, subject)
            captured["base"] = self
            captured["delegated"] = result
            return result

        fake = _FakeServiceAccountModule()
        with mock.patch.object(google_auth, "_service_account", fake), \
                mock.patch.object(_FakeCreds, "with_subject", spy):
            build_credentials(GOOD_KEY, subject="lucaso@avatarnaturalfoods.com")

        self.assertIsNot(captured["delegated"], captured["base"])
        self.assertIsNone(captured["base"].subject,
                          "with_subject must not mutate the receiver")

    def test_a_mutating_google_auth_is_refused_loudly(self):
        # If a future google-auth ever made with_subject mutate and return self,
        # the delegation would silently be a no-op. Refuse rather than run.
        fake = _FakeServiceAccountModule(creds_cls=_MutatingCreds)
        with mock.patch.object(google_auth, "_service_account", fake):
            with self.assertRaises(GDriveAuthError):
                build_credentials(GOOD_KEY, subject="lucaso@avatarnaturalfoods.com")

    def test_sa_direct_mode_uses_the_bare_service_account(self):
        fake = _FakeServiceAccountModule()
        with mock.patch.object(google_auth, "_service_account", fake):
            creds = build_credentials(GOOD_KEY, subject=None)
        self.assertIsNone(creds.subject)

    def test_default_scopes_are_the_frozen_pair(self):
        fake = _FakeServiceAccountModule()
        with mock.patch.object(google_auth, "_service_account", fake):
            build_credentials(GOOD_KEY, subject="lucaso@avatarnaturalfoods.com")
        self.assertEqual(fake.calls[0]["scopes"], list(SCOPES))

    def test_missing_google_auth_library_says_what_to_install(self):
        with mock.patch.object(google_auth, "_service_account", None):
            with self.assertRaises(GDriveAuthError) as ctx:
                build_credentials(GOOD_KEY, subject="x@example.com")
        self.assertIn("google-auth", str(ctx.exception))


class TestTokenRefresh(BaseCase):
    """Probe P2 — and the one error message that never mentions its own cause."""

    def test_successful_refresh_returns_the_credentials(self):
        creds = _FakeCreds(GOOD_KEY, SCOPES, subject="lucaso@avatarnaturalfoods.com")
        result = refresh_credentials(creds, subject=creds.subject, request=object())
        self.assertIs(result, creds)
        self.assertEqual(creds.refresh_calls, 1)

    def test_unauthorized_client_becomes_a_scope_error_with_remediation(self):
        creds = _FakeCreds(GOOD_KEY, SCOPES, subject="lucaso@avatarnaturalfoods.com")
        creds.refresh_error = RuntimeError(
            '("unauthorized_client: Client is unauthorized to retrieve access '
            'tokens using this method", {})'
        )
        with self.assertRaises(GDriveScopeError) as ctx:
            refresh_credentials(creds, subject=creds.subject, request=object())

        message = str(ctx.exception)
        # Google's own text mentions neither scopes nor the client id, which is
        # why the remediation checklist has to be attached here.
        self.assertIn("NUMERIC Client ID", message)
        self.assertIn("drive.readonly", message)
        self.assertIn("lucaso@avatarnaturalfoods.com", message)

    def test_other_failures_are_plain_auth_errors(self):
        creds = _FakeCreds(GOOD_KEY, SCOPES)
        creds.refresh_error = RuntimeError("connection reset by peer")
        with self.assertRaises(GDriveAuthError) as ctx:
            refresh_credentials(creds, request=object())
        self.assertNotIsInstance(ctx.exception, GDriveScopeError)

    def test_refresh_failures_are_redacted(self):
        creds = _FakeCreds(GOOD_KEY, SCOPES)
        creds.refresh_error = RuntimeError("assertion rejected: " + FAKE_PRIVATE_KEY)
        with self.assertRaises(GDriveAuthError) as ctx:
            refresh_credentials(creds, request=object())
        self.assertNotIn("superSecretBytes", str(ctx.exception))


class TestRedaction(BaseCase):
    """SPEC §7.4 — a redactor that can be bypassed is not a redactor."""

    def test_pem_block_is_removed(self):
        self.assertNotIn("superSecretBytes", redact("boom: " + FAKE_PRIVATE_KEY))
        self.assertIn("REDACTED", redact("boom: " + FAKE_PRIVATE_KEY))

    def test_pem_with_literal_backslash_n_is_removed(self):
        # This is the form a JSON key takes when dumped straight into a log.
        one_line = FAKE_PRIVATE_KEY.replace("\n", "\\n")
        self.assertNotIn("superSecretBytes", redact(one_line))

    def test_json_private_key_field_is_removed(self):
        blob = json.dumps(GOOD_KEY)
        cleaned = redact(blob)
        self.assertNotIn("superSecretBytes", cleaned)
        self.assertIn("client_email", cleaned, "non-secret context must survive")

    def test_bearer_tokens_are_removed(self):
        self.assertNotIn("abcdef123456", redact("Authorization: Bearer abcdef123456"))

    def test_google_access_tokens_are_removed(self):
        self.assertNotIn("ya29.AbCdEfGhIj", redact("token=ya29.AbCdEfGhIjKlMnOp"))

    def test_total_function(self):
        # A redactor that raises will end up inside a bare except and bypassed.
        self.assertEqual(redact(None), "")
        self.assertIsInstance(redact(RuntimeError("x")), str)
        self.assertIsInstance(redact({"a": 1}), str)
        self.assertIsInstance(redact(12345), str)

    def test_harmless_text_is_untouched(self):
        self.assertEqual(redact("files.list returned 0 files"), "files.list returned 0 files")
