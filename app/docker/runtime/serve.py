#!/usr/bin/env python3
import argparse
import os
import sys
from app_runtime import configuration, secret_environment, server_command

parser = argparse.ArgumentParser()
parser.add_argument('--config', default='/etc/mlo/service.json')
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
    command = server_command(config, host, port)
except (OSError, ValueError, KeyError, TypeError):
    raise SystemExit('app runtime inputs invalid') from None
# Exec preserves SIGTERM delivery to the real server; migrations are a separate step.
os.execv(sys.executable, [sys.executable, *command])
