# -*- coding: utf-8 -*-
# Part of gdrive_odoo_sync. See LICENSE (LGPL-3) for full copyright and licensing details.
{
    'name': 'Google Drive → Odoo Sync & Verification',
    'version': '18.0.1.0.0',
    'summary': 'Mirror Google Drive into Odoo, stage spreadsheet tabs, verify with content hashes, heal on approval.',
    'author': 'Avatar Natural Foods',
    'website': 'https://avatarnaturalfoods.com',
    'category': 'Productivity/Documents',
    'license': 'LGPL-3',
    'application': True,
    'installable': True,
    'auto_install': False,
    # 'base' for the ORM/ir.* plumbing, 'mail' because gdrive.node inherits
    # mail.thread so ingest failures leave an audit trail on the record itself.
    # We deliberately do NOT depend on Enterprise 'documents' — this module must
    # install on Community. See SPEC §1.
    'depends': ['base', 'mail'],
    # IMPORT names, not pip names. `google.oauth2` and `googleapiclient` come
    # from the pip packages `google-auth` and `google-api-python-client`
    # respectively (see repo-root requirements.txt). Declaring them here makes a
    # missing dependency a clean, actionable install-time error instead of an
    # AttributeError inside a cron at 03:00.
    'external_dependencies': {
        'python': ['google.oauth2', 'googleapiclient', 'openpyxl'],
    },
    # ORDER IS LOAD-BEARING:
    #   1. security groups first  — record rules and ACLs reference them by xml id
    #   2. ACLs, then record rules
    #   3. data (sequences/params/crons) — crons reference model_* ids created by
    #      the Python model registration, which happens before any XML loads
    #   4. views — they reference groups
    #   5. menus LAST — they reference the actions defined in the view files
    'data': [
        'security/gdrive_security.xml',
        'security/ir.model.access.csv',
        'security/gdrive_record_rules.xml',
        'data/ir_sequence_data.xml',
        'data/ir_config_parameter_data.xml',
        'data/ir_cron_data.xml',
        'views/gdrive_connection_views.xml',
        'views/gdrive_node_views.xml',
        'views/gdrive_dataset_views.xml',
        'views/gdrive_staged_row_views.xml',
        'views/gdrive_mapping_views.xml',
        'views/gdrive_verification_views.xml',
        'views/gdrive_drift_views.xml',
        'views/gdrive_plan_views.xml',
        'views/gdrive_sync_run_views.xml',
        'views/res_config_settings_views.xml',
        'wizard/gdrive_connection_test_wizard_views.xml',
        'wizard/gdrive_mapping_builder_wizard_views.xml',
        'wizard/gdrive_heal_wizard_views.xml',
        'views/gdrive_menus.xml',
    ],
    'images': [
        'static/description/icon.png',
    ],
    # Odoo 17/18 hook signature is `def hook(env)`. post_init forces a full
    # recompute so that an upgrade which changed CANON_VERSION can never serve a
    # hash computed by the previous normalizer as "verified" (SPEC §6, §9.1).
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
}
