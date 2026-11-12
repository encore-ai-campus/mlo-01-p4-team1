#!/usr/bin/env python3
import argparse
import os
import sys
from app_runtime import configuration, secret_environment, server_command

parser = argparse.ArgumentParser()
parser.add_argument('--config', default='/etc/mlo/service.json')
parser.add_argument('--account-mail-worker', action='store_true')
args = parser.parse_args()
try:
    config, host, port = configuration(args.config)
    if config.get('secret_env'):
        values = secret_environment(config['secret_env'])
        if any(key in os.environ and os.environ[key] != value for key, value in values.items()):
            raise ValueError('conflicting secret sources')
        os.environ.update(values)
    if not os.environ.get('DJANGO_SECRET_KEY'):
        raise ValueError('Django secret required')
    if args.account_mail_worker:
        if config['kind'] != 'web' or os.environ.get('C_LOCAL_DEMO') != '0':
            raise ValueError('account mail worker requires the production web image')
        command = ['-m', 'backend.manage', 'deliver_account_mail']
    else:
        command = server_command(config, host, port)
except (OSError, ValueError, KeyError, TypeError):
    raise SystemExit('app runtime inputs invalid') from None
# Exec preserves SIGTERM delivery to the real server; migrations are a separate step.
os.execv(sys.executable, [sys.executable, *command])
