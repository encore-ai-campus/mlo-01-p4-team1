#!/usr/bin/env python3
import json
import os
import urllib.request
from app_runtime import configuration

try:
    config, host, port = configuration()
    if host == '0.0.0.0':
        host = '127.0.0.1'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://'+host+':'+port+'/readyz', timeout=4) as response:
        ready = response.status == 200 and json.loads(response.read(16384)).get('revision') == os.environ['APP_REVISION']
except (OSError, ValueError, KeyError, TypeError):
    ready = False
raise SystemExit(0 if ready else 1)
