import compression.zstd
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

SCRIPT = Path(__file__).with_name('codex-usage')


def usage(n=100):
    return dict(input_tokens=n, cached_input_tokens=20, cache_write_input_tokens=10,
                output_tokens=30, reasoning_output_tokens=5, total_tokens=n+30)


def line(kind, payload, stamp='2026-11-01T00:00:00Z'):
    return dict(type=kind, payload=payload, timestamp=stamp)


def session(source='cli', ident='root', parent=None):
    return [line('session_meta', dict(id=ident, source=source, parent_thread_id=parent,
                 model_provider='openai', timestamp='2026-10-01T00:00:00Z')),
            line('turn_context', dict(model='test'))]


def modern(response='r', n=100):
    return line('token_usage_record', dict(response_id=response, usage=usage(n),
                                         thread_token_usage=usage(n)))


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), 'codex-usage executable is required')
        self.m = runpy.run_path(str(SCRIPT))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home/'sessions').mkdir()

    def write(self, name, rows, compressed=False, directory='sessions'):
        path = self.home/directory/name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = ''.join(json.dumps(r)+'\n' for r in rows)
        if compressed:
            with compression.zstd.open(path, 'wt') as f:
                f.write(data)
        else:
            path.write_text(data)
        return path

    def read(self):
        return self.m['read_usage'](self.home, [])

    def test_plain_compressed_and_duplicate_response(self):
        rows = session()+[modern(), modern()]
        self.write('a.jsonl', rows)
        self.write('a.jsonl.zst', rows, True)
        self.write('copy.jsonl.zst', rows, True, 'archived_sessions')
        self.assertEqual(len(self.read()), 1)

    def test_cli_descendants_and_editor_exclusion(self):
        self.write('root.jsonl', session()+[modern()])
        self.write('child.jsonl', session({'subagent': {'thread_spawn': {'parent_thread_id':'root'}}}, 'child','root')+[modern('child')])
        self.write('editor.jsonl', session('vscode','editor')+[modern('editor')])
        self.write('orphan.jsonl', session({'subagent':'review'},'orphan','missing')+[modern('orphan')])
        self.assertEqual(len(self.read()), 2)

    def test_legacy_mixed_and_repeated_snapshots(self):
        old = line('event_msg', dict(type='token_count',info=dict(last_token_usage=usage(),total_token_usage=usage())))
        self.write('a.jsonl', session()+[old, old, modern('r2',200),line('event_msg',dict(type='token_count',info=dict(last_token_usage=usage(200),total_token_usage=usage(200))))])
        self.assertEqual(len(self.read()), 2)

    def test_counts_and_cache_pricing(self):
        record = dict(timestamp='2026-11-01T00:00:00Z',model='test',provider='openai',usage=usage())
        catalog = {'openai':{'models':{'test':{'cost':dict(input=2,output=10,cache_read=1,cache_write=3)}}}}
        result=self.m['aggregate']([record],catalog,datetime(2026,11,3,tzinfo=timezone.utc))
        self.assertEqual(result['2026-11']['tokens'],130)
        self.assertAlmostEqual(result['2026-11']['cost'],0.00049)

    def test_missing_price_and_month_window(self):
        records=[dict(timestamp=t,model='unknown',provider='openai',usage=usage()) for t in ['2026-09-01T00:00:00Z','2026-10-15T00:00:00Z','2026-11-01T00:00:00Z','2026-12-01T00:00:00Z']]
        result=self.m['aggregate'](records,{},datetime(2026,11,3,tzinfo=timezone.utc))
        self.assertEqual(sorted(result),['2026-10','2026-11'])
        self.assertIn('unknown',self.m['render'](result))

    def test_tier_boundary(self):
        cost=dict(input=2,output=10,cache_read=1,cache_write=3,tiers=[dict(input=4,output=20,cache_read=2,cache_write=6,tier=dict(type='context',size=100))])
        self.assertEqual(self.m['select_rate'](cost,100)['input'],2)
        self.assertEqual(self.m['select_rate'](cost,101)['input'],4)

    def test_malformed_and_missing_stores(self):
        with self.write('bad.jsonl',session()+[modern()]).open('a') as stream:
            stream.write('{broken\n')
        warnings=[]
        self.assertEqual(len(self.m['read_usage'](self.home,warnings)),1)
        self.assertTrue(warnings)
        with self.assertRaises(FileNotFoundError):
            self.m['read_usage'](self.home/'missing',[])

if __name__ == '__main__':
    unittest.main()

class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.m = runpy.run_path(str(SCRIPT))

    def test_zero_and_missing_required_rate(self):
        record=dict(provider='openai',model='test',usage=usage())
        catalog={'openai':{'models':{'test':{'cost':dict(input=0,output=0,cache_read=0,cache_write=0)}}}}
        self.assertEqual(self.m['price'](record,catalog),0)
        del catalog['openai']['models']['test']['cost']['cache_write']
        self.assertIsNone(self.m['price'](record,catalog))

    def test_fork_history_is_excluded(self):
        rows=session()+[modern()]
        meta=dict(forked_from_id='parent',timestamp='2026-11-02T00:00:00Z',model_provider='openai')
        self.assertEqual(list(self.m['candidates'](rows,meta,[])),[])

    def test_offline_cache_and_missing_cache(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            cache=Path(tmp)/'cache.json'
            cache.write_text('{"openai": {"models": {}}}')
            with patch('urllib.request.urlopen',side_effect=OSError('offline')):
                self.assertIn('openai',self.m['load_pricing'](cache,[]))
                self.assertEqual(self.m['load_pricing'](Path(tmp)/'absent',[]),{})

    def test_cli_help_empty_and_missing(self):
        import subprocess
        import os
        self.assertEqual(subprocess.run([str(SCRIPT),'--help'],capture_output=True).returncode,0)
        with tempfile.TemporaryDirectory() as tmp:
            env=dict(os.environ,CODEX_HOME=tmp)
            missing=subprocess.run([str(SCRIPT)],env=env,capture_output=True,text=True)
            self.assertEqual(missing.returncode,1)
            (Path(tmp)/'sessions').mkdir()
            empty=subprocess.run([str(SCRIPT)],env=env,capture_output=True,text=True)
            self.assertEqual(empty.returncode,0)
            self.assertIn('no usage',empty.stdout)

class CatalogValidationTests(unittest.TestCase):
    def test_malformed_provider_is_unpriced(self):
        module=runpy.run_path(str(SCRIPT))
        record=dict(provider='openai',model='test',usage=usage())
        self.assertIsNone(module['price'](record,{'openai': []}))
