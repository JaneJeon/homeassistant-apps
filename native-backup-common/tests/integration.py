#!/usr/bin/env python3
"""Exercise the actual images. Run after building native-vm:test and native-vl:test."""
import json, os, shutil, subprocess, tempfile, time, urllib.request, urllib.parse
from pathlib import Path

def docker(*args): return subprocess.check_output(['docker', *args], text=True).strip()
def request(url, data=None):
    req = urllib.request.Request(url, data=data, headers={'Content-Type':'application/stream+json'} if data is not None else {})
    with urllib.request.urlopen(req, timeout=10) as r: return r.read().decode()
def wait(url):
    for i in range(60):
        try: return request(url)
        except Exception: time.sleep(1)
    raise RuntimeError('server did not start')

def exercise(image, port, engine):
    with tempfile.TemporaryDirectory(prefix='native-integration-', dir=os.getcwd()) as tmp:
        root=Path(tmp); container=None
        config={'bootstrap_mode':'restore_only','retention':'1y' if engine=='vm' else '2w','additionalArguments':'','enableHTTPAuth':False}
        (root/'options.json').write_text(json.dumps(config))
        vol=str(root)+':/data'
        docker('run','--rm','--platform',os.environ.get('TEST_PLATFORM','linux/amd64'),'-v',vol,'--entrypoint','/usr/local/bin/native-backup', image,'startup','init-empty')
        def start(extra=()):
            cid=docker('run','-d','--platform',os.environ.get('TEST_PLATFORM','linux/amd64'),'-v',vol,'-p',f'127.0.0.1::{port}',*extra,image)
            hostport=docker('port',cid,f'{port}/tcp').rsplit(':',1)[1]
            base='http://127.0.0.1:'+hostport
            try: wait(base+'/health')
            except Exception:
                print(docker('logs',cid)); docker('rm','-f',cid); raise
            return cid,base
        try:
            container,base=start()
            if engine=='vm':
                print('ingest:', request(base+'/api/v1/import/prometheus', ('native_backup_test{source="integration"} 42 '+str(int(time.time()*1000)-30000)+'\n').encode()))
                request(base+'/internal/force_flush')
                url=base+'/api/v1/query?'+urllib.parse.urlencode({'query':'native_backup_test'})
                def check(): return '42' in request(url)
            else:
                print('log ingest:',request(base+'/insert/jsonline?_stream_fields=host', (json.dumps({'_time':int(time.time())-30,'_msg':'native backup integration marker','host':'test'})+'\n').encode()))
                request(base+'/internal/force_flush')
                url=base+'/select/logsql/query?'+urllib.parse.urlencode({'query':'"native backup integration marker"'})
                def check(): return 'native backup integration marker' in request(url)
            for attempt in range(15):
                if check(): break
                time.sleep(1)
            else: raise AssertionError('ingested record not found before export: ' + request(url))
            pid_before=docker('inspect','-f','{{.State.Pid}}',container)
            docker('exec',container,'/backup-pre')
            if engine == 'vl' and os.environ.get('CAPTURE_VL'): 
                shutil.rmtree('/tmp/native-vl-capture', ignore_errors=True); shutil.copytree(root/'backup'/'payload', '/tmp/native-vl-capture')
            assert docker('inspect','-f','{{.State.Pid}}',container)==pid_before
            assert check(), 'database unavailable after export'
            docker('stop',container); docker('rm',container); container=None
            legacy = root / 'legacy'; shutil.copytree(root / 'live', legacy)
            imported = root / 'imported'; imported.mkdir()
            imported_config = dict(config, bootstrap_mode='import_legacy')
            (imported/'options.json').write_text(json.dumps(imported_config))
            source_name = 'victoria-metrics-data' if engine == 'vm' else 'victoria-logs-data'
            original_vol = vol; vol = str(imported)+':/data'
            container,base=start(('-v',str(legacy)+':/share/'+source_name+':ro'))
            if engine=='vm': url=base+'/api/v1/query?'+urllib.parse.urlencode({'query':'native_backup_test'})
            else: url=base+'/select/logsql/query?'+urllib.parse.urlencode({'query':'"native backup integration marker"'})
            if not check(): raise AssertionError('legacy import record missing')
            docker('rm','-f',container); container=None; vol=original_vol
            shutil.rmtree(root/'live')
            container,base=start()
            if engine=='vm': url=base+'/api/v1/query?'+urllib.parse.urlencode({'query':'native_backup_test'})
            else: url=base+'/select/logsql/query?'+urllib.parse.urlencode({'query':'"native backup integration marker"'})
            if not check(): raise AssertionError('restored record missing')
            print(engine+': export, live continuity, legacy import and restore query passed')
        finally:
            if container: docker('rm','-f',container)

tag = os.environ.get('TEST_IMAGE_TAG', 'test')
for image,port,engine in [(os.environ.get('TEST_VM_IMAGE',f'native-vm:{tag}'),8428,'vm'),(os.environ.get('TEST_VL_IMAGE',f'native-vl:{tag}'),9428,'vl')]:
    if not os.environ.get('TEST_ONLY_ENGINE') or os.environ['TEST_ONLY_ENGINE'] == engine:
        exercise(image,port,engine)
