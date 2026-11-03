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
        from unittest.mock import patch
        clock=type('Clock',(datetime,),{'now':staticmethod(lambda:datetime(2026,11,3,tzinfo=timezone.utc))})
        frozen=patch.dict(self.m['read_usage'].__globals__,datetime=clock)
        frozen.start()
        self.addCleanup(frozen.stop)
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

class MalformedShapeTests(unittest.TestCase):
    def setUp(self):
        self.m=runpy.run_path(str(SCRIPT))

    def test_nonobject_legacy_info(self):
        warnings=[]
        rows=[line('event_msg',dict(type='token_count',info=['bad']))]
        self.assertEqual(list(self.m['candidates'](rows,{},warnings)),[])
        self.assertTrue(warnings)

    def test_nonstring_timestamp(self):
        warnings=[]
        row=modern()
        row['timestamp']=[]
        self.assertEqual(list(self.m['candidates']([row],{},warnings)),[])
        self.assertTrue(warnings)

    def test_invalid_tier_shapes(self):
        for tiers in (None,{},['bad'],[dict(tier=[])], [dict(tier=dict(type='context',size=float('nan'))) ]):
            with self.subTest(tiers=tiers):
                self.assertIsNone(self.m['select_rate'](dict(input=1,tiers=tiers),100))

    def test_invalid_model_provider_identity(self):
        for provider,model in (([], 'test'),('openai',[])):
            record=dict(provider=provider,model=model,usage=usage())
            self.assertIsNone(self.m['price'](record,{'openai':{'models':{}}}))

class ReviewRegressionTests(unittest.TestCase):
    setUp=UsageTests.setUp
    write=UsageTests.write
    read=UsageTests.read

    def test_missing_required_counters_are_skipped(self):
        for key in ('input_tokens','output_tokens'):
            with self.subTest(key=key):
                bad=modern('bad')
                bad['payload']['usage']={'input_tokens':100,'output_tokens':30,'total_tokens':130}
                del bad['payload']['usage'][key]
                bad['payload']['usage']['total_tokens']=sum(
                    bad['payload']['usage'].get(k,0) for k in ('input_tokens','output_tokens'))
                self.write('a.jsonl',session()+[modern(),bad])
                warnings=[]
                records=self.m['read_usage'](self.home,warnings)
                self.assertEqual(len(records),1)
                self.assertTrue(warnings)
                self.assertEqual(self.m['aggregate'](records,{},datetime(2026,11,3,tzinfo=timezone.utc))['2026-11']['tokens'],130)

    def test_malformed_counter_and_identity_shapes_are_skipped(self):
        cases=[('total',{'input_tokens':[]}),('response',['bad']),('response',{'bad':1}),
               ('response',42),('parent',[]),('parent',{}),('parent',42),
               ('nested_parent',['bad'])]
        for field,value in cases:
            with self.subTest(field=field,value=value):
                rows=session(ident='bad')+[modern('bad')]
                if field=='total':
                    rows[-1]['payload']['thread_token_usage']=value
                elif field=='response':
                    rows[-1]['payload']['response_id']=value
                elif field=='parent':
                    rows[0]['payload']['parent_thread_id']=value
                else:
                    rows[0]['payload']['source']={'subagent':{'thread_spawn':{'parent_thread_id':value}}}
                self.write('bad.jsonl',rows)
                self.write('good.jsonl',session()+[modern()])
                warnings=[]
                self.assertEqual(len(self.m['read_usage'](self.home,warnings)),1)
                self.assertTrue(warnings)

    def test_rejected_modern_record_keeps_legacy_fallback(self):
        old=line('event_msg',dict(type='token_count',info=dict(last_token_usage=usage(),total_token_usage=usage())))
        for response in (None,'',[],['bad'],42):
            with self.subTest(response=response):
                self.write('a.jsonl',session()+[modern(response),old])
                warnings=[]
                records=self.m['read_usage'](self.home,warnings)
                self.assertEqual(len(records),1)
                self.assertFalse(records[0]['modern'])
                self.assertEqual(self.m['aggregate'](records,{},datetime(2026,11,3,tzinfo=timezone.utc))['2026-11']['tokens'],130)
                self.assertTrue(warnings)

    def test_loader_discards_transcript_bodies(self):
        rows=session()+[modern(),line('event_msg',dict(type='token_count',info=dict(last_token_usage=usage(),total_token_usage=usage())))]
        for row in rows:
            row['payload']['unrelated']='x'*10000
        path=self.write('a.jsonl',rows+[line('event_msg',dict(type='agent_message',message='x'*10000))])
        retained=self.m['load_rows'](path,[])
        self.assertEqual(len(retained),4)
        self.assertNotIn('unrelated',json.dumps(retained))
        self.assertLess(len(json.dumps(retained)),2000)
        self.assertEqual(len(self.read()),1)

    def test_cache_failure_preserves_offline_catalog(self):
        from unittest.mock import patch
        import io
        cache=self.home/'prices.json'
        old=b'{"old": {"models": {}}}'
        live=b'{"live": {"models": {}}}'
        cache.write_bytes(old)
        def fail_write(path, raw):
            with path.open('wb') as stream:
                stream.write(raw[:5])
            raise OSError('disk full')
        with patch('urllib.request.urlopen',return_value=io.BytesIO(live)), patch('pathlib.Path.write_bytes',autospec=True,side_effect=fail_write):
            warnings=[]
            self.assertIn('live',self.m['load_pricing'](cache,warnings))
        self.assertTrue(warnings)
        self.assertEqual(cache.read_bytes(),old)
        with patch('urllib.request.urlopen',side_effect=OSError('offline')):
            self.assertIn('old',self.m['load_pricing'](cache,[]))

    def test_cache_replacement_publishes_complete_private_catalog(self):
        from unittest.mock import patch
        import io
        cache=self.home/'prices.json'
        cache.write_text('{}')
        live=b'{"live": {"models": {}}}'
        with patch('urllib.request.urlopen',return_value=io.BytesIO(live)):
            self.assertIn('live',self.m['load_pricing'](cache,[]))
        self.assertEqual(cache.read_bytes(),live)
        self.assertEqual(cache.stat().st_mode & 0o777,0o600)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()),['prices.json','sessions'])

class InterruptedInputTests(unittest.TestCase):
    setUp=UsageTests.setUp
    write=UsageTests.write

    def test_truncated_archive_preserves_readable_sessions(self):
        path=self.write('broken.jsonl.zst',session(ident='broken')+[modern('broken')],True)
        path.write_bytes(path.read_bytes()[:-1])
        self.write('good.jsonl',session()+[modern()])
        warnings=[]
        records=self.m['read_usage'](self.home,warnings)
        self.assertIn('r',[record['response'] for record in records])
        self.assertIn('unreadable session file skipped or partially read',warnings)

    def test_incomplete_http_body_uses_cached_prices(self):
        import http.client
        import io
        from unittest.mock import patch
        class Socket:
            def makefile(self, *args):
                return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{}')
        response=http.client.HTTPResponse(Socket())
        response.begin()
        cache=self.home/'prices.json'
        cache.write_text('{"cached": {"models": {}}}')
        warnings=[]
        with patch('urllib.request.urlopen',return_value=response):
            self.assertIn('cached',self.m['load_pricing'](cache,warnings))
        self.assertIn('pricing is STALE: live fetch failed',warnings)
        self.assertEqual(cache.read_text(),'{"cached": {"models": {}}}')

class ArchiveRetentionTests(unittest.TestCase):
    setUp=UsageTests.setUp
    write=UsageTests.write

    def test_historical_usage_is_not_retained(self):
        import tracemalloc
        old=modern('old')
        old['timestamp']='2020-01-01T00:00:00Z'
        self.write('history.jsonl',session()+[old]*10000+[modern('current')])
        tracemalloc.start()
        try:
            records=self.m['read_usage'](self.home,[])
            peak=tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual([r['response'] for r in records],['current'])
        self.assertLess(peak,2_000_000)

    def test_window_filter_preserves_model_and_legacy_snapshot_state(self):
        from unittest.mock import patch
        old=line('event_msg',dict(type='token_count',info=dict(last_token_usage=usage(),total_token_usage=usage())), '2020-01-01T00:00:00Z')
        repeat=line('event_msg',old['payload'])
        self.write('history.jsonl',session()+[old,repeat,modern('current')])
        with patch.dict(self.m['read_usage'].__globals__,datetime=type('Clock',(datetime,),{'now':staticmethod(lambda:datetime(2026,11,3,tzinfo=timezone.utc))})):
            records=self.m['read_usage'](self.home,[])
        self.assertEqual([r['response'] for r in records],['current'])
        self.assertEqual(records[0]['model'],'test')


class FixtureClockTests(unittest.TestCase):
    def test_reader_fixtures_ignore_the_real_calendar(self):
        from unittest.mock import patch
        clock=type('Clock',(datetime,),{'now':staticmethod(lambda:datetime(2027,1,3,tzinfo=timezone.utc))})
        for case in (UsageTests,ReviewRegressionTests,InterruptedInputTests,ArchiveRetentionTests):
            with self.subTest(case=case.__name__), patch('datetime.datetime',clock):
                fixture=case()
                try:
                    fixture.setUp()
                    fixture.write('current.jsonl',session()+[modern()])
                    records=fixture.m['read_usage'](fixture.home,[])
                    self.assertEqual([r['response'] for r in records],['r'])
                finally:
                    fixture.doCleanups()


class TimestampOverflowTests(unittest.TestCase):
    setUp=UsageTests.setUp
    write=UsageTests.write

    def test_overflowing_timestamps_preserve_readable_usage(self):
        for field in ('usage','fork'):
            with self.subTest(field=field):
                rows=session(ident='bad')+[modern('bad')]
                if field=='usage':
                    rows[-1]['timestamp']='9999-12-31T23:59:59-14:00'
                else:
                    rows[0]['payload'].update(forked_from_id='parent',timestamp='9999-12-31T23:59:59-14:00')
                self.write('bad.jsonl',rows)
                self.write('good.jsonl',session()+[modern()])
                warnings=[]
                records=self.m['read_usage'](self.home,warnings)
                self.assertEqual([r['response'] for r in records],['r'])
                self.assertIn('invalid usage timestamp skipped',warnings)


class DeepAncestryTests(unittest.TestCase):
    def test_deep_ancestry_preserves_source_and_cycle_rules(self):
        import sys
        module=runpy.run_path(str(SCRIPT))
        metadata={'root':dict(source='cli')}
        parent='root'
        for index in range(sys.getrecursionlimit()+100):
            ident=str(index)
            metadata[ident]=dict(source={'subagent':{'thread_spawn':{'parent_thread_id':parent}}})
            parent=ident
        self.assertTrue(module['included'](parent,metadata))
        metadata['root']['source']='vscode'
        self.assertFalse(module['included'](parent,metadata))
        metadata['root']=dict(parent_thread_id=parent,source='cli')
        self.assertFalse(module['included'](parent,metadata))
        del metadata['root']
        self.assertFalse(module['included'](parent,metadata))


class OversizedPricingTests(unittest.TestCase):
    def test_oversized_rates_and_tiers_preserve_tokens_with_unknown_cost(self):
        module=runpy.run_path(str(SCRIPT))
        record=dict(timestamp='2026-11-01T00:00:00Z',provider='openai',model='test',usage=usage())
        for cost in (dict(input=10**400,output=1,cache_read=1,cache_write=1),
                     dict(input=-1,output=1,cache_read=1,cache_write=1),
                     dict(input=1,output=1,cache_read=1,cache_write=1,
                          tiers=[dict(input=2,tier=dict(type='context',size=10**400))]),
                     dict(input=1e308,output=1,cache_read=1,cache_write=1)):
            with self.subTest(cost=cost):
                catalog={'openai':{'models':{'test':{'cost':cost}}}}
                result=module['aggregate']([record],catalog,datetime(2026,11,3,tzinfo=timezone.utc))
                self.assertEqual(result['2026-11']['tokens'],130)
                self.assertTrue(result['2026-11']['unknown'])
                self.assertIn('Cost: unknown',module['render'](result))

    def test_oversized_token_arithmetic_preserves_unknown_cost(self):
        module=runpy.run_path(str(SCRIPT))
        record=dict(timestamp='2026-11-01T00:00:00Z',provider='openai',model='test',usage=usage(10**400))
        for rate in (1.0,1):
            with self.subTest(rate=rate):
                cost=dict(input=rate,output=1,cache_read=1,cache_write=1)
                catalog={'openai':{'models':{'test':{'cost':cost}}}}
                result=module['aggregate']([record],catalog,datetime(2026,11,3,tzinfo=timezone.utc))
                self.assertEqual(result['2026-11']['tokens'],10**400+30)
                self.assertTrue(result['2026-11']['unknown'])


class ProtocolFailureTests(unittest.TestCase):
    setUp=UsageTests.setUp
    write=UsageTests.write

    def test_malformed_http_status_preserves_cached_report(self):
        import http.client
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from unittest.mock import patch
        class Socket:
            def makefile(self, *args):
                return io.BytesIO(b'NOT HTTP\r\n\r\n')
        def fetch(*args, **kwargs):
            response=http.client.HTTPResponse(Socket())
            response.begin()
            return response
        self.write('current.jsonl',session()+[modern()])
        cache=self.home/'.cache/codex-usage/models-dev.json'
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({'openai':{'models':{'test':{'cost':dict(input=2,output=10,cache_read=1,cache_write=3)}}}}))
        out,err=io.StringIO(),io.StringIO()
        with patch('urllib.request.urlopen',side_effect=fetch), patch('pathlib.Path.home',return_value=self.home), patch.dict('os.environ',CODEX_HOME=str(self.home)), patch('sys.argv',['codex-usage']), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(self.m['main'](),0)
        self.assertIn('130 tokens, Cost: $0.00',out.getvalue())
        self.assertIn('pricing is STALE: live fetch failed',err.getvalue())


class AncestryReuseTests(unittest.TestCase):
    setUp=UsageTests.setUp
    write=UsageTests.write

    def test_reader_reuses_ancestry_without_cross_report_cache(self):
        from unittest.mock import patch
        self.write('0000.jsonl',session()+[modern()])
        for index in range(1,81):
            parent='root' if index==1 else str(index-1)
            self.write(f'{index:04}.jsonl',session(ident=str(index),parent=parent)+[modern(str(index))])
        original=self.m['iter_rows']
        visits=[]
        class Metadata(dict):
            def get(self, key, default=None):
                if key=='parent_thread_id':
                    visits.append(key)
                return super().get(key,default)
        def rows(path,warnings):
            for row in original(path,warnings):
                if row['type']=='session_meta':
                    row['payload']=Metadata(row['payload'])
                yield row
        with patch.dict(self.m['read_usage'].__globals__,iter_rows=rows):
            self.assertEqual(len(self.m['read_usage'](self.home,[])),81)
            self.assertLessEqual(len(visits),162)
            self.write('0000.jsonl',session('vscode')+[modern()])
            self.assertEqual(self.m['read_usage'](self.home,[]),[])
            self.write('0000.jsonl',session(parent='80')+[modern()])
            self.assertEqual(self.m['read_usage'](self.home,[]),[])
            self.write('0000.jsonl',session(parent='missing')+[modern()])
            self.assertEqual(self.m['read_usage'](self.home,[]),[])


class MonthlyOverflowTests(unittest.TestCase):
    def test_finite_record_costs_overflow_to_unknown_month(self):
        module=runpy.run_path(str(SCRIPT))
        record=dict(timestamp='2026-11-01T00:00:00Z',provider='openai',model='test',
                    usage=dict(input_tokens=1_000_000,output_tokens=0))
        catalog={'openai':{'models':{'test':{'cost':dict(input=10**308)}}}}
        self.assertEqual(module['price'](record,catalog),1e308)
        result=module['aggregate']([record,record],catalog,datetime(2026,11,3,tzinfo=timezone.utc))
        self.assertEqual(result['2026-11']['tokens'],2_000_000)
        self.assertIn('Cost: unknown',module['render'](result))

    def test_finite_month_and_zero_cost_remain_known(self):
        module=runpy.run_path(str(SCRIPT))
        record=dict(timestamp='2026-11-01T00:00:00Z',provider='openai',model='test',
                    usage=dict(input_tokens=1_000_000,output_tokens=0))
        for rate,want in ((2,'Cost: $2.00'),(0,'Cost: $0.00')):
            with self.subTest(rate=rate):
                catalog={'openai':{'models':{'test':{'cost':dict(input=rate)}}}}
                result=module['aggregate']([record],catalog,datetime(2026,11,3,tzinfo=timezone.utc))
                self.assertIn(want,module['render'](result))
