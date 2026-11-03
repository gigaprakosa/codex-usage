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

class LegacyTimestampTests(unittest.TestCase):
    setUp = UsageTests.setUp
    write = UsageTests.write

    def test_rejected_legacy_timestamp_does_not_consume_snapshot(self):
        payload=dict(type='token_count',info=dict(last_token_usage=usage(),total_token_usage=usage()))
        cases=[('bad',False,1), ('2026-09-01T00:00:00Z',True,1),
               ('2026-09-01T00:00:00Z',False,0)]
        for first,forked,want in cases:
            with self.subTest(first=first,forked=forked):
                rows=session()
                if forked:
                    rows[0]['payload']['forked_from_id']='parent'
                self.write('legacy.jsonl',rows+[line('event_msg',payload,first),
                                               line('event_msg',payload)])
                warnings=[]
                records=self.m['read_usage'](self.home,warnings)
                self.assertEqual(len(records),want)
                if want:
                    result=self.m['aggregate'](records,{},datetime(2026,11,3,tzinfo=timezone.utc))
                    self.assertEqual(result['2026-11']['tokens'],130)
                if first == 'bad':
                    self.assertIn('invalid usage timestamp skipped',warnings)

class RecoveryTests(unittest.TestCase):
    setUp = UsageTests.setUp
    write = UsageTests.write
    read = UsageTests.read

    def broken(self, response='gap'):
        row=modern(response)
        row['payload']['usage']=None
        return row

    def test_window_diagnostics_and_inactive_orphan(self):
        bad=modern(); bad['payload']['usage']={}; bad['timestamp']='2026-07-01T00:00:00Z'
        self.write('old.jsonl',session()+[bad])
        self.write('orphan.jsonl',session(ident='orphan',parent='missing'))
        warnings=[]
        self.assertEqual(self.m['read_usage'](self.home,warnings),[])
        self.assertEqual(warnings,[])

    def test_editor_descendant_has_no_coverage_warning(self):
        self.write('editor.jsonl',session('vscode','editor'))
        self.write('child.jsonl',session(ident='child',parent='editor')+[modern()])
        warnings=[]
        self.assertEqual(self.m['read_usage'](self.home,warnings),[])
        self.assertEqual(warnings,[])

    def test_state_threads_and_spawn_edges_recover_ancestry_readonly(self):
        import sqlite3
        path=self.home/'state_5.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('create table threads (id text, source text, model_provider text)')
            db.execute('insert into threads values (?,?,?)',('root','cli','openai'))
            db.execute('create table thread_spawn_edges (parent_thread_id text, child_thread_id text)')
            db.execute('insert into thread_spawn_edges values (?,?)',('root','child'))
        db.close()
        before=path.read_bytes()
        self.write('child.jsonl',session({'subagent':'review'},'child')+[modern()])
        self.assertEqual(len(self.read()),1)
        self.assertEqual(path.read_bytes(),before)

    def test_unknown_state_source_is_not_cli(self):
        import sqlite3
        with sqlite3.connect(self.home/'state_5.sqlite') as db:
            db.execute('create table threads (id text, source text)')
            db.execute('insert into threads values (?,?)',('parent','unknown'))
        db.close()
        self.write('child.jsonl',session(ident='child',parent='parent')+[modern()])
        warnings=[]
        self.assertEqual(self.m['read_usage'](self.home,warnings),[])
        self.assertTrue(warnings)

    def test_compressed_alternate_recovers_before_estimation(self):
        self.write('a.jsonl',session()+[self.broken()])
        self.write('a.jsonl.zst',session()+[modern('gap',240)],True)
        warnings=[]; records=self.m['read_usage'](self.home,warnings)
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['usage']['total_tokens'],270)
        self.assertEqual(records[0]['status'],'recovered')
        self.assertEqual(warnings,[])

    def cumulative_rows(self):
        rows=[modern('left'),self.broken(),modern('right')]
        rows[1]['payload']['thread_token_usage']=None
        rows[2]['payload']['thread_token_usage']={k:v*3 for k,v in usage().items()}
        return rows

    def test_isolated_gap_reconstructs_cumulative_difference(self):
        self.write('a.jsonl',session()+self.cumulative_rows())
        records=self.read()
        self.assertEqual(len(records),3)
        gap=next(r for r in records if r['response']=='gap')
        self.assertEqual(gap['usage'],usage())
        self.assertEqual(gap['status'],'recovered')

    def test_reset_model_change_and_adjacent_gaps_reject_reconstruction(self):
        for mode in ('reset','model','adjacent','negative_category'):
            with self.subTest(mode=mode):
                rows=self.cumulative_rows()
                if mode=='reset': rows[-1]['payload']['thread_token_usage']=usage()
                if mode=='model': rows.insert(2,line('turn_context',dict(model='other')))
                if mode=='adjacent': rows.insert(2,self.broken('gap2'))
                if mode=='negative_category': rows[-1]['payload']['thread_token_usage']['cached_input_tokens']=0
                self.write('a.jsonl',session()+rows)
                self.assertFalse(any(r['response']=='gap' for r in self.read()))

    def test_five_unique_exact_samples_estimate_disjoint_medians(self):
        rows=[modern(str(i),n) for i,n in enumerate((100,200,300,400,500))]
        gap=self.broken(); gap['payload']['thread_token_usage']=None
        self.write('a.jsonl',session()+rows+[gap])
        warnings=[]; records=self.m['read_usage'](self.home,warnings)
        self.assertEqual(len(records),6)
        gap=next(r for r in records if r['response']=='gap')
        self.assertEqual(gap['usage'],usage(300))
        self.assertEqual(gap['status'],'estimated')
        buckets=self.m['aggregate'](records,{})
        self.assertEqual(buckets['2026-11']['estimated'],1)
        self.assertIn('estimated',self.m['render'](buckets))
        self.assertFalse(warnings)

    def test_estimation_requires_unique_window_model_samples_and_identity(self):
        for mode in ('four','duplicate','old','model','identity','adjacent'):
            with self.subTest(mode=mode):
                rows=[modern(str(i)) for i in range(5)]
                gap=self.broken()
                if mode=='four': rows.pop()
                if mode=='duplicate': rows[-1]=modern('0')
                if mode=='old': rows[-1]['timestamp']='2026-07-01T00:00:00Z'
                if mode=='model': rows.append(line('turn_context',dict(model='other')))
                if mode=='identity': gap['payload']['response_id']=None
                if mode=='adjacent': rows.append(self.broken('other'))
                self.write('a.jsonl',session()+rows+[gap])
                warnings=[]; records=self.m['read_usage'](self.home,warnings)
                self.assertFalse(any(r['response']=='gap' for r in records))
                self.assertTrue(warnings)

    def test_unresolved_gaps_count_once_per_month(self):
        self.write('a.jsonl',session()+[self.broken(),self.broken()])
        warnings=[]; self.m['read_usage'](self.home,warnings)
        self.assertEqual(sum('2026-11: 1 unresolved' in w for w in warnings),1)

    def test_pricing_notes_do_not_claim_token_incompleteness(self):
        self.assertNotIn('incomplete',self.m['diagnostic']('pricing is STALE: live fetch failed'))
        self.assertIn('incomplete',self.m['diagnostic']('invalid token counters skipped'))

    def test_transient_pricing_retry_and_cache_date(self):
        import io
        from unittest.mock import patch
        cache=self.home/'prices.json'
        attempts=[]
        def fetch(request,timeout):
            attempts.append(timeout)
            if len(attempts)==1: raise TimeoutError()
            return io.BytesIO(b'{"live": {}}')
        with patch('urllib.request.urlopen',side_effect=fetch):
            self.assertEqual(self.m['load_pricing'](cache,[]),{'live':{}})
        self.assertEqual(attempts,[5,5])
        warnings=[]
        with patch('urllib.request.urlopen',side_effect=TimeoutError()):
            self.m['load_pricing'](cache,warnings)
        self.assertTrue(any('cache date:' in w for w in warnings))

    def test_invalid_catalog_and_permanent_http_are_not_retried(self):
        import io,urllib.error
        from unittest.mock import patch
        for error in (None,urllib.error.HTTPError('https://example.test',404,'missing',{},None)):
            attempts=[]
            def fetch(*args,**kwargs):
                attempts.append(1)
                if error: raise error
                return io.BytesIO(b'[]')
            with patch('urllib.request.urlopen',side_effect=fetch):
                self.assertEqual(self.m['load_pricing'](self.home/'absent',[]),{})
            self.assertEqual(len(attempts),1)
            if error:
                error.close()

class RecoveryBoundaryTests(unittest.TestCase):
    setUp = UsageTests.setUp
    write = UsageTests.write
    read = UsageTests.read
    broken = RecoveryTests.broken
    cumulative_rows = RecoveryTests.cumulative_rows
    def test_legacy_empty_info_is_not_a_usage_gap(self):
        self.write('a.jsonl',session()+[line('event_msg',dict(type='token_count',info=None))])
        warnings=[]
        self.assertEqual(self.m['read_usage'](self.home,warnings),[])
        self.assertEqual(warnings,[])

    def test_historical_malformed_payload_has_no_warning(self):
        self.write('a.jsonl',session()+[line('event_msg',[], '2026-07-01T00:00:00Z')])
        warnings=[]
        self.assertEqual(self.m['read_usage'](self.home,warnings),[])
        self.assertEqual(warnings,[])

    def test_duplicate_neighbor_is_not_known_response_count(self):
        rows=self.cumulative_rows()
        rows.insert(1,modern('left'))
        self.write('a.jsonl',session()+rows)
        self.assertFalse(any(r['response']=='gap' for r in self.read()))

    def test_conflicting_gap_copies_are_not_estimated(self):
        rows=self.cumulative_rows()
        self.write('a.jsonl',session()+rows)
        other=self.broken(); other['timestamp']='2026-11-02T00:00:00Z'
        self.write('b.jsonl',session()+[other])
        self.assertFalse(any(r['response']=='gap' for r in self.read()))

    def test_estimates_and_recoveries_never_train_estimates(self):
        rows=[modern(str(i)) for i in range(4)]
        self.write('a.jsonl',session()+rows+self.cumulative_rows()[1:])
        self.assertFalse(any(r['status']=='estimated' for r in self.read()))

    def test_missing_identity_in_inactive_session_has_no_warning(self):
        self.write('a.jsonl',[line('session_meta',dict(source='cli'))])
        warnings=[]
        self.assertEqual(self.m['read_usage'](self.home,warnings),[])
        self.assertEqual(warnings,[])

    def test_even_disjoint_medians_round_each_category(self):
        values=[dict(input_tokens=10,cached_input_tokens=2,cache_write_input_tokens=1,output_tokens=4,reasoning_output_tokens=1,total_tokens=14),
                dict(input_tokens=13,cached_input_tokens=3,cache_write_input_tokens=2,output_tokens=7,reasoning_output_tokens=2,total_tokens=20)]
        rows=[]
        for i in range(6):
            row=modern(str(i)); row['payload'].update(usage=values[i%2],thread_token_usage=values[i%2]); rows.append(row)
        missing=self.broken(); missing['payload']['thread_token_usage']=None
        self.write('a.jsonl',session()+rows+[missing])
        gap=next(r for r in self.read() if r['response']=='gap')
        self.assertEqual(gap['usage'],dict(input_tokens=12,cached_input_tokens=2,cache_write_input_tokens=2,output_tokens=6,reasoning_output_tokens=2,total_tokens=18))

    def test_unhashable_model_is_excluded_from_recovery_evidence(self):
        self.write('a.jsonl',session()+[line('turn_context',dict(model=[])),modern()])
        self.assertEqual(len(self.read()),1)

    def test_state_missing_schema_and_unavailable_database_are_safe(self):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db:
            db.execute('create table threads (unrelated text)')
        self.write('a.jsonl',session()+[modern()])
        self.assertEqual(len(self.read()),1)
        (self.home/'state_6.sqlite').write_bytes(b'not sqlite')
        warnings=[]
        self.assertEqual(len(self.m['read_usage'](self.home,warnings)),1)
        self.assertTrue(any('ancestry state unavailable' in w for w in warnings))

    def test_out_of_order_cumulative_context_is_rejected(self):
        rows=self.cumulative_rows(); rows[-1]['timestamp']='2026-10-01T00:00:00Z'
        self.write('a.jsonl',session()+rows)
        self.assertFalse(any(r['response']=='gap' for r in self.read()))

class AdditionalRecoveryTests(unittest.TestCase):
    setUp = UsageTests.setUp
    write = UsageTests.write
    read = UsageTests.read
    broken = RecoveryTests.broken

    def test_first_isolated_response_can_be_estimated(self):
        self.write('a.jsonl',session()+[self.broken()]+[modern(str(i)) for i in range(5)])
        gap=next(r for r in self.read() if r['response']=='gap')
        self.assertEqual(gap['status'],'estimated')

    def test_invalid_fork_metadata_outside_window_has_no_warning(self):
        rows=session(); rows[0]['payload'].update(forked_from_id='fork',timestamp='bad')
        row=modern(); row['timestamp']='2026-07-01T00:00:00Z'
        self.write('a.jsonl',rows+[row])
        warnings=[]
        self.assertEqual(self.m['read_usage'](self.home,warnings),[])
        self.assertEqual(warnings,[])

    def test_permanent_tls_error_is_not_retried(self):
        import ssl,urllib.error
        from unittest.mock import patch
        attempts=[]
        def fetch(*args,**kwargs):
            attempts.append(1)
            raise urllib.error.URLError(ssl.SSLCertVerificationError('invalid certificate'))
        with patch('urllib.request.urlopen',side_effect=fetch):
            self.assertEqual(self.m['load_pricing'](self.home/'missing',[]),{})
        self.assertEqual(len(attempts),1)

    def test_state_edges_do_not_override_recorded_editor_parent(self):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db:
            db.execute('create table thread_spawn_edges (parent_thread_id text,child_thread_id text)')
            db.execute("insert into thread_spawn_edges values ('cli','child')")
            db.commit()
        self.write('cli.jsonl',session(ident='cli'))
        self.write('editor.jsonl',session('vscode','editor'))
        source={'subagent':{'thread_spawn':{'parent_thread_id':'editor'}}}
        self.write('child.jsonl',session(source,'child')+[modern()])
        self.assertEqual(self.read(),[])

    def test_estimated_tokens_are_explicit_in_monthly_output(self):
        gap=self.broken(); gap['payload']['thread_token_usage']=None
        self.write('a.jsonl',session()+[modern(str(i)) for i in range(5)]+[gap])
        buckets=self.m['aggregate'](self.read(),{})
        self.assertIn('130 estimated tokens',self.m['render'](buckets))

    def test_recovery_does_not_modify_session_sources(self):
        import hashlib
        gap=self.broken(); gap['payload']['thread_token_usage']=None
        path=self.write('a.jsonl',session()+[modern(str(i)) for i in range(5)]+[gap])
        before=(hashlib.sha256(path.read_bytes()).digest(),path.stat().st_size,path.stat().st_mtime_ns)
        self.assertEqual(len(self.read()),6)
        self.assertEqual((hashlib.sha256(path.read_bytes()).digest(),path.stat().st_size,path.stat().st_mtime_ns),before)

class AmbiguityTests(unittest.TestCase):
    setUp = UsageTests.setUp
    write = UsageTests.write
    read = UsageTests.read
    broken = RecoveryTests.broken
    cumulative_rows = RecoveryTests.cumulative_rows

    def test_invalid_neighbor_timestamp_cannot_reconstruct(self):
        rows=self.cumulative_rows(); rows[-1]['timestamp']='bad'
        self.write('a.jsonl',session()+rows)
        self.assertFalse(any(r['response']=='gap' for r in self.read()))

    def test_malformed_intervening_event_blocks_inference(self):
        rows=self.cumulative_rows(); rows.insert(2,line('token_usage_record',[]))
        self.write('a.jsonl',session()+rows)
        self.assertFalse(any(r['response']=='gap' for r in self.read()))

    def test_historical_checkpoint_can_reconstruct_window_gap(self):
        rows=self.cumulative_rows(); rows[0]['timestamp']='2026-09-30T23:00:00+07:00'
        self.write('a.jsonl',session()+rows)
        gap=next(r for r in self.read() if r['response']=='gap')
        self.assertEqual(gap['status'],'recovered')
        self.assertEqual(gap['usage'],usage())

class DiagnosticFlowTests(unittest.TestCase):
    setUp = UsageTests.setUp
    write = UsageTests.write

    def test_cli_pricing_notes_keep_tokens_complete(self):
        import io
        from contextlib import redirect_stdout,redirect_stderr
        from unittest.mock import patch
        self.write('a.jsonl',session()+[modern()])
        out,err=io.StringIO(),io.StringIO()
        with patch('urllib.request.urlopen',side_effect=TimeoutError()), patch('pathlib.Path.home',return_value=self.home), patch.dict('os.environ',CODEX_HOME=str(self.home)), patch('sys.argv',['codex-usage']), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(self.m['main'](),0)
        self.assertIn('130 tokens',out.getvalue())
        self.assertIn('cost is unknown',err.getvalue())
        self.assertNotIn('incomplete',err.getvalue())

class HistoricalSnapshotRecoveryTests(unittest.TestCase):
    setUp=UsageTests.setUp

    def test_historical_legacy_snapshot_blocks_window_repeat_without_modern_twin(self):
        old=line('event_msg',dict(type='token_count',info=dict(
            last_token_usage=usage(),total_token_usage=usage())), '2020-01-01T00:00:00Z')
        repeat=line('event_msg',old['payload'])
        rows=session()+[old,repeat]
        self.assertEqual(list(self.m['candidates'](rows,rows[0]['payload'],[],2026*12+10)),[])


class AuthorRuntimeRegressionTests(unittest.TestCase):
    setUp = UsageTests.setUp
    write = UsageTests.write

    def test_nested_editor_descendants_have_no_coverage_warning(self):
        self.write('editor.jsonl',session('vscode','editor'))
        for ident,parent in (('child','editor'),('grandchild','child')):
            source={'subagent':{'thread_spawn':{'parent_thread_id':parent}}}
            self.write(ident+'.jsonl',session(source,ident)+[modern(ident)])
        warnings=[]
        self.assertEqual(self.m['read_usage'](self.home,warnings),[])
        self.assertEqual(warnings,[])

    def test_advancing_legacy_total_with_null_usage_is_diagnosed(self):
        first=line('event_msg',dict(type='token_count',info=dict(
            last_token_usage=usage(),total_token_usage=usage())))
        total={k:2*v for k,v in usage().items()}
        missing=line('event_msg',dict(type='token_count',info=dict(
            last_token_usage=None,total_token_usage=total)))
        self.write('a.jsonl',session()+[first,missing])
        warnings=[]
        records=self.m['read_usage'](self.home,warnings)
        self.assertEqual(sum(r['usage']['total_tokens'] for r in records),130)
        self.assertIn('invalid token counters skipped',warnings)

    def test_even_integer_medians_preserve_huge_counts_and_ties(self):
        for base in (10**400,10**20+1):
            with self.subTest(base=base):
                rows=[modern(str(i),base+i%2) for i in range(6)]
                missing=modern('gap'); missing['payload']['usage']=None
                missing['payload']['thread_token_usage']=None
                self.write('a.jsonl',session()+rows+[missing])
                warnings=[]
                records=self.m['read_usage'](self.home,warnings)
                gap=next(r for r in records if r['response']=='gap')
                expected=base if base%2==0 else base+1
                self.assertEqual(gap['usage'],usage(expected))
                self.assertEqual(gap['status'],'estimated')
                self.assertEqual(len(records),7)
                self.assertEqual(warnings,[])


class AuthorDesignRegressionTests(unittest.TestCase):
    setUp = UsageTests.setUp
    write = UsageTests.write
    broken = RecoveryTests.broken
    cumulative_rows = RecoveryTests.cumulative_rows

    def test_state_threads_preserve_nested_recorded_parent(self):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.home/'state_5.sqlite')) as db:
            db.execute('create table threads (id text, parent_thread_id text)')
            db.execute("insert into threads values ('child','cli')")
            db.commit()
        self.write('cli.jsonl',session(ident='cli'))
        self.write('editor.jsonl',session('vscode','editor'))
        source={'subagent':{'thread_spawn':{'parent_thread_id':'editor'}}}
        self.write('child.jsonl',session(source,'child')+[modern()])
        warnings=[]
        self.assertEqual(self.m['read_usage'](self.home,warnings),[])
        self.assertEqual(warnings,[])

    def test_reconstructed_snapshot_deduplicates_legacy_twin(self):
        self.write('a.jsonl',session()+self.cumulative_rows())
        twin=line('event_msg',dict(type='token_count',info=dict(
            last_token_usage=usage(),total_token_usage={k:2*v for k,v in usage().items()})))
        self.write('copy.jsonl',session()+[twin])
        warnings=[]
        records=self.m['read_usage'](self.home,warnings)
        self.assertEqual(sum(r['usage']['total_tokens'] for r in records),390)
        self.assertEqual(len(records),3)
        gap=next(r for r in records if r['response']=='gap')
        self.assertEqual(gap['snapshot'],tuple(2*usage().get(k,0) for k in self.m['TOKEN_KEYS']))
        self.assertEqual(warnings,[])

    def test_wal_ancestry_read_preserves_source_files_and_committed_rows(self):
        import subprocess,sys
        path=self.home/'state_5.sqlite'
        code="""import sqlite3,sys,os
connection=sqlite3.connect(sys.argv[1])
connection.execute('pragma journal_mode=wal')
connection.execute('create table threads (id text, source text)')
connection.execute("insert into threads values ('root','cli')")
connection.commit()
os._exit(0)
"""
        subprocess.run([sys.executable,'-c',code,str(path)],check=True)
        Path(str(path)+'-shm').unlink()
        before={p.name:p.read_bytes() for p in self.home.iterdir() if p.is_file()}
        metadata,warnings={},[]
        self.m['state_metadata'](self.home,metadata,warnings)
        self.assertEqual(metadata['root']['source'],'cli')
        self.assertEqual(warnings,[])
        after={p.name:p.read_bytes() for p in self.home.iterdir() if p.is_file()}
        self.assertEqual(after,before)


class FinalRecoveryTests(unittest.TestCase):
    setUp = UsageTests.setUp
    write = UsageTests.write
    broken = RecoveryTests.broken
    def advancing_samples(self):
        rows=[modern(str(i)) for i in range(5)]
        for i,row in enumerate(rows,1):
            row['payload']['thread_token_usage']={k:v*i for k,v in usage().items()}
        return rows

    def test_contradictory_cumulative_evidence_blocks_median(self):
        for mode in ('reset','snapshot'):
            with self.subTest(mode=mode):
                rows=self.advancing_samples()
                gap=self.broken()
                gap['payload']['thread_token_usage']=None if mode=='reset' else usage(900)
                right=modern('right')
                right['payload']['thread_token_usage']={k:v*(1 if mode=='reset' else 7) for k,v in usage().items()}
                self.write('a.jsonl',session()+rows+[gap,right])
                warnings=[]; records=self.m['read_usage'](self.home,warnings)
                self.assertFalse(any(r['response']=='gap' for r in records))
                self.assertEqual(sum(r['usage']['total_tokens'] for r in records),780)
                self.assertIn('contradictory recovery evidence skipped',warnings)
                self.assertIn('2026-11: 1 unresolved usage gaps',warnings)

    def test_inferred_usage_cannot_replace_valid_different_usage(self):
        rows=self.advancing_samples()
        gap=modern('gap',900)
        gap['payload']['thread_token_usage']=None
        right=modern('right')
        right['payload']['thread_token_usage']={k:v*7 for k,v in usage().items()}
        self.write('a.jsonl',session()+rows+[gap,right])
        warnings=[]; records=self.m['read_usage'](self.home,warnings)
        self.assertFalse(any(r['response']=='gap' for r in records))
        self.assertEqual(sum(r['usage']['total_tokens'] for r in records),780)
        self.assertIn('contradictory recovery evidence skipped',warnings)
        self.assertIn('2026-11: 1 unresolved usage gaps',warnings)

    def test_exact_legacy_twin_precedes_median_estimate(self):
        total={k:5*v+usage(900)[k] for k,v in usage().items()}
        gap=self.broken()
        gap['payload']['thread_token_usage']=total
        self.write('a.jsonl',session()+self.advancing_samples()+[gap])
        twin=line('event_msg',dict(type='token_count',info=dict(
            last_token_usage=usage(900),total_token_usage=total)))
        self.write('b.jsonl',session()+[twin])
        warnings=[]; records=self.m['read_usage'](self.home,warnings)
        self.assertEqual(sum(r['usage']['total_tokens'] for r in records),1580)
        self.assertEqual(len(records),6)
        self.assertFalse(any(r['status']=='estimated' for r in records))
        self.assertEqual(warnings,[])

class CanonicalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.m=runpy.run_path(str(SCRIPT))

    def test_valid_usage_cannot_be_replaced_by_median(self):
        record=dict(usage=usage(),provider='openai',model='test',response='r',isolated=True)
        self.assertIsNone(self.m['estimate'](record,[dict(usage=usage())]*5))

    def test_conflicting_modern_copies_are_not_arbitrarily_counted(self):
        rows=session()+[modern('same',100),modern('same',200)]
        records=list(self.m['candidates'](rows,rows[0]['payload'],[],None,True))
        warnings=[]
        result=self.m['recover_records']([('root',r) for r in records],warnings)
        self.assertEqual(result,[])
        self.assertTrue(any('conflicting' in w for w in warnings))

    def test_conflicting_legacy_twins_survive_until_reconciliation(self):
        total=usage(500)
        rows=session()+[line('event_msg',dict(type='token_count',info=dict(last_token_usage=usage(n),total_token_usage=total))) for n in (100,200)]
        records=list(self.m['candidates'](rows,rows[0]['payload'],[],None,True))
        self.assertEqual(len(records),2)

    def test_duplicate_neighbors_block_median_fallback(self):
        rows=session()+[modern('left'),modern('gap'),modern('right')]
        rows[3]['payload']['usage']={}
        records=list(self.m['candidates'](rows,rows[0]['payload'],[],None,True))
        samples=list(self.m['candidates'](session()+[modern(str(n)) for n in range(5)],rows[0]['payload'],[],None,True))
        warnings=[]
        result=self.m['recover_records']([('root',r) for r in records+[records[0]]+samples],warnings)
        self.assertFalse(any(r['response']=='gap' for _,r in result))

class FinalRecoveryEvidenceTests(unittest.TestCase):
    setUp=UsageTests.setUp
    write=UsageTests.write
    read=UsageTests.read
    def test_malformed_legacy_model_does_not_crash(self):
        old=line('event_msg',dict(type='token_count',info=dict(last_token_usage=usage(),total_token_usage=usage())))
        self.write('a.jsonl',session()+[line('turn_context',dict(model=[])),old])
        self.assertEqual(len(self.read()),1)

    def test_legacy_snapshot_cannot_recover_two_responses(self):
        old=line('event_msg',dict(type='token_count',info=dict(last_token_usage=usage(),total_token_usage=usage())))
        gap=modern('gap');gap['payload']['usage']=None
        for first in (modern('exact'),dict(gap,payload=dict(gap['payload'],response_id='gap2'))):
            with self.subTest(first=first['payload']['response_id']):
                self.write('a.jsonl',session()+[first,gap,old])
                warnings=[]
                records=self.m['read_usage'](self.home,warnings)
                self.assertEqual(sum(r['usage']['total_tokens'] for r in records),130)
                self.assertTrue(warnings)

class MalformedRecoveryOwnerTests(unittest.TestCase):
    setUp=UsageTests.setUp
    write=UsageTests.write
    read=UsageTests.read

    def test_unhashable_response_cannot_crash_legacy_recovery(self):
        gap=modern('gap');gap['payload']['usage']=None
        bad=modern([])
        old=line('event_msg',dict(type='token_count',info=dict(last_token_usage=usage(),total_token_usage=usage())))
        self.write('a.jsonl',session()+[gap,bad,old])
        warnings=[]
        records=self.m['read_usage'](self.home,warnings)
        self.assertEqual(sum(r['usage']['total_tokens'] for r in records),130)
        self.assertTrue(warnings)

class PartialEvidenceTests(unittest.TestCase):
    setUp=UsageTests.setUp
    write=UsageTests.write
    read=UsageTests.read

    def test_reset_at_end_blocks_estimation(self):
        rows=[modern(str(i),100*(i+1)) for i in range(5)]
        gap=modern('gap');gap['payload']['usage']=None
        self.write('a.jsonl',session()+rows+[gap])
        warnings=[]
        self.assertFalse(any(r['response']=='gap' for r in self.m['read_usage'](self.home,warnings)))
        self.assertTrue(warnings)

    def test_partial_counter_contradiction_blocks_all_fallbacks(self):
        rows=[modern('left'),modern('gap'),modern('right')]
        rows[1]['payload']['usage']=dict(input_tokens=900,output_tokens=30,total_tokens=929)
        rows[1]['payload']['thread_token_usage']=None
        rows[2]['payload']['thread_token_usage']={k:3*v for k,v in usage().items()}
        self.write('a.jsonl',session()+rows)
        warnings=[]
        self.assertFalse(any(r['response']=='gap' for r in self.m['read_usage'](self.home,warnings)))
        self.assertTrue(warnings)

class CumulativeEvidencePriorityTests(unittest.TestCase):
    setUp=UsageTests.setUp
    write=UsageTests.write
    read=UsageTests.read

    def test_known_eof_delta_is_recovered_not_estimated(self):
        left=modern('left')
        gap=modern('gap');gap['payload']['usage']=None
        total=usage();total['input_tokens']+=10;total['total_tokens']+=10
        gap['payload']['thread_token_usage']=total
        self.write('a.jsonl',session()+[left,gap])
        found=next(r for r in self.read() if r['response']=='gap')
        self.assertEqual(found['usage']['total_tokens'],10)
        self.assertEqual(found['status'],'recovered')

    def test_contradictory_alternate_is_not_silently_accepted(self):
        bad=modern('gap');bad['payload']['usage']=dict(input_tokens=900,output_tokens=30,total_tokens=929)
        self.write('a.jsonl',session()+[bad])
        self.write('a.jsonl.zst',session()+[modern('gap')],True)
        warnings=[]
        self.assertEqual(self.m['read_usage'](self.home,warnings),[])
        self.assertTrue(warnings)

class SampleEligibilityRegressionTests(unittest.TestCase):
    setUp=UsageTests.setUp
    write=UsageTests.write
    read=UsageTests.read

    def test_conflicted_response_cannot_train_estimate(self):
        rows=[modern(str(i)) for i in range(5)]
        conflict=modern('0'); conflict['payload']['usage']=dict(input_tokens=900,output_tokens=30,total_tokens=929)
        gap=modern('gap'); gap['payload']['usage']=None; gap['payload']['thread_token_usage']=None
        self.write('a.jsonl',session()+rows+[conflict,gap])
        warnings=[]
        records=self.m['read_usage'](self.home,warnings)
        self.assertFalse(any(r['response']=='gap' for r in records))
        self.assertTrue(warnings)
