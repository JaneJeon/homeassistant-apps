#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

def validate_args(extra):
    # Managed flags cannot be overridden by a user-supplied Go flag.
    reserved = ('-storageDataPath', '-httpListenAddr', '-httpAuth.username', '-httpAuth.password', '-snapshotAuthKey', '-partitionManageAuthKey', '-promscrape.config')
    if any(('-' + arg.lstrip('-')).split('=')[0] in reserved for arg in extra):
        raise ValueError('additionalArguments overrides a managed flag')


def main():
    engine = os.environ['VICTORIA_ENGINE']
    with open('/data/options.json') as f: config = json.load(f)
    subprocess.run(['/usr/local/bin/native-backup', 'startup', config.get('bootstrap_mode', 'restore_only')], check=True)
    args = ['/'+('victoria-metrics-prod' if engine == 'victoriametrics' else 'victoria-logs-prod'), '-storageDataPath=/data/live', '-retentionPeriod='+config['retention']]
    if config.get('enableHTTPAuth'):
        args += ['-httpAuth.username='+config['username'], '-httpAuth.password='+config['password']]
    if engine == 'victoriametrics' and config.get('enablePrometheusScrape'):
        # JSON strings are valid YAML strings and safely quote arbitrary credentials and URLs.
        q = json.dumps
        scheme = 'https' if config.get('prometheusScrapeHTTPS') else 'http'
        scrape = ('global:\n  scrape_interval: 1m\n  scrape_timeout: 15s\nscrape_config_files:\n- /config/*.yaml\n- /config/*.yml\nscrape_configs:\n  - job_name: home-assistant\n    scrape_interval: '+q(config['prometheusScrapeInterval'])+'\n    scrape_timeout: '+q(config['prometheusScrapeTimeout'])+'\n    metrics_path: /api/prometheus\n    authorization:\n      credentials: '+q(config['longelivedToken'])+'\n    scheme: '+scheme+'\n    static_configs:\n      - targets: ['+q(config['homeassistantUrl'])+']\n')
        Path('/data/work').mkdir(exist_ok=True)
        Path('/data/work/prometheus.yml').write_text(scrape)
        args.append('-promscrape.config=/data/work/prometheus.yml')
    extra = shlex.split(config.get('additionalArguments', ''))
    validate_args(extra)
    os.execv(args[0], args + extra)

if __name__ == '__main__':
    main()
