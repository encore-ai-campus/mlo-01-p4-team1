"""수령 03/05/06의 완성 회차·NULL·역순·충돌·조회 만료 계약."""
import copy
import os
import unittest
from datetime import datetime, timedelta
from importlib.util import find_spec
from itertools import permutations
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from pipeline.weather import es_latest
from pipeline.weather.common import OBS, FORECAST, KST, digest
from pipeline.weather.projection import project_grid
from pipeline.weather.latest_state import merge_rows
from pipeline.weather.sky import current_sky

NOW=datetime(2026,10,16,12,10,tzinfo=KST)


def row(category,source='odam',at=NOW.replace(minute=0),issue=None,value=0):
    issue=issue or at
    return dict(_id=f'{source}-{issue.isoformat()}-{at.isoformat()}-{category}',
        nx=60,ny=127,source=source,category=category,issued_at=issue.isoformat(),target_at=at.isoformat(),
        received_at=(issue+timedelta(minutes=1)).isoformat(),source_hash='hash-'+issue.isoformat(),
        rule_version='normalize_v1',measurement={'status':'valid','value':value,
        'unit':'code' if category in ('PTY','SKY') else 'degC' if category=='T1H' else 'unknown'})


def observation(at=NOW.replace(minute=0)):
    return [row(category,at=at,value=0) for category in OBS]


def forecast(issue):
    return [
        row(code, 'vsrt', issue.replace(minute=0) + timedelta(hours=hour), issue,
            value=1 if code == 'SKY' else 0)
        for hour in range(1, 7)
        for code in FORECAST
    ]


class WeatherEsTests(unittest.TestCase):
    def test_partial_new_round_preserves_old_and_completed_missing_stays_null(self):
        old=observation(NOW.replace(minute=0)-timedelta(minutes=10))
        new=observation()
        pending=project_grid(merge_rows(old,new[:1]))
        self.assertEqual(pending['observation']['target_at'],old[0]['target_at'])
        new[0]['measurement']={'status':'missing','value':None,'unit':'code'}
        complete=project_grid(merge_rows(old,new))
        self.assertEqual(complete['observation']['target_at'],new[0]['target_at'])
        self.assertIsNone(complete['observation']['values'][new[0]['category']]['value'])

    def test_forecast_incomplete_issue_keeps_complete_six_targets(self):
        issue=NOW.replace(minute=0)-timedelta(minutes=30)
        rows=[row(code,'vsrt',NOW.replace(minute=0)+timedelta(hours=hour),issue,
                  value=1 if code=='SKY' else 0) for hour in range(1,7) for code in FORECAST]
        partial=row('PTY','vsrt',NOW.replace(minute=0)+timedelta(hours=2),issue+timedelta(minutes=20))
        projected=project_grid(merge_rows(rows,[partial]))
        self.assertEqual(len(projected['forecast']),6)
        self.assertTrue(all(slot['issued_at']==issue.isoformat() for slot in projected['forecast']))

    def test_delayed_forecast_completes_after_newer_partial_round(self):
        issue = NOW.replace(hour=11, minute=30)
        old = forecast(issue - timedelta(hours=1))
        delayed = forecast(issue)
        newest = forecast(issue + timedelta(hours=1))[:1]

        saved = merge_rows(old, delayed[:1])
        saved = merge_rows(saved, newest)
        self.assertEqual(project_grid(saved)['forecast'][0]['issued_at'], old[0]['issued_at'])

        saved = merge_rows(saved, delayed[1:])
        forecast_view = project_grid(saved)['forecast']
        self.assertEqual(forecast_view, project_grid(merge_rows(old, delayed + newest))['forecast'])
        self.assertEqual({slot['issued_at'] for slot in forecast_view}, {issue.isoformat()})
        self.assertEqual(len(forecast_view), 6)

    def test_forecast_retention_includes_six_hour_boundary_across_midnight(self):
        latest = NOW.replace(hour=2, minute=30) + timedelta(days=1)
        cutoff = latest - timedelta(hours=6)
        older = cutoff - timedelta(microseconds=1)
        complete = forecast(cutoff - timedelta(hours=1))
        incoming = [
            row('T1H', 'vsrt', issue + timedelta(hours=1), issue)
            for issue in (older, cutoff, latest)
        ]
        saved = merge_rows(complete, incoming)

        temperature_issues = {
            item['issued_at'] for item in saved
            if item['source'] == 'vsrt' and item['category'] == 'T1H'
        }
        self.assertEqual(temperature_issues, {complete[0]['issued_at'], cutoff.isoformat(), latest.isoformat()})
        self.assertEqual(project_grid(saved)['forecast'][0]['issued_at'], complete[0]['issued_at'])

    def test_delayed_observation_completes_after_newer_partial_round(self):
        issue = NOW.replace(minute=0)
        old = observation(issue - timedelta(minutes=10))
        delayed = observation(issue)
        newest = observation(issue + timedelta(minutes=10))[:1]
        saved = merge_rows(merge_rows(old, delayed[:1]), newest)

        result = project_grid(merge_rows(saved, delayed[1:]))['observation']
        self.assertEqual(result['target_at'], issue.isoformat())
        self.assertTrue(result['complete'])

    def test_same_receipt_conflict_is_rejected_even_with_later_correction(self):
        original = row('T1H', value=21)
        conflicting = row('T1H', value=22)
        conflicting['source_hash'] = 'conflicting'
        later = row('T1H', value=23)
        later.update(source_hash='later', received_at=(NOW + timedelta(minutes=1)).isoformat())

        for incoming in permutations((original, conflicting, later)):
            with self.subTest(values=[item['measurement']['value'] for item in incoming]):
                with self.assertRaisesRegex(ValueError, '^ambiguous_same_time_correction$'):
                    merge_rows([], list(incoming))
        self.assertEqual(merge_rows([original], [later]), [later])

    def test_same_receipt_accepts_equivalent_value_and_new_rule_for_same_original(self):
        original = row('T1H', value=21)
        original['measurement']['raw'] = '21'
        equivalent = copy.deepcopy(original)
        equivalent['source_hash'] = 'z-equivalent'
        equivalent['measurement']['raw'] = '21.0'
        self.assertEqual(merge_rows([original], [equivalent]), [equivalent])

        renormalized = copy.deepcopy(original)
        renormalized['rule_version'] = 'normalize_v2'
        renormalized['measurement']['value'] = 22
        self.assertEqual(merge_rows([original], [renormalized]), [renormalized])
        self.assertEqual(merge_rows([renormalized], [original]), [renormalized])

    def test_es_rechecks_value_conflict_after_concurrent_create(self):
        rows = [row('T1H', value=21)]
        saved = {'schema_version': 1, 'revision': 1, 'rows': rows, 'state_hash': digest(rows)}
        conflict = row('T1H', value=22)
        conflict['source_hash'] = 'conflicting'
        es_document = {'_source': saved, '_seq_no': 1, '_primary_term': 1}
        concurrent_write = HTTPError('http://test', 409, 'conflict', None, None)
        with patch.object(es_latest, 'configuration', return_value=('http://test', 'weather-test')), \
                patch.object(es_latest, 'load', side_effect=[None, es_document]), \
                patch.object(es_latest, 'request', side_effect=concurrent_write) as request:
            with self.assertRaisesRegex(ValueError, '^ambiguous_same_time_correction$'):
                es_latest.write_grid(60, 127, [conflict])
        request.assert_called_once()
        self.assertIn('op_type=create', request.call_args.args[2])
        self.assertEqual(saved['revision'], 1)

    @unittest.skipUnless(find_spec('pymongo'), 'requires weather MongoDB dependencies')
    def test_mongo_rejects_same_receipt_conflict_before_writing(self):
        from pipeline.weather import mongo_current

        rows = [row('T1H', value=21)]
        saved = {'schema_version': 1, 'revision': 1, 'rows': rows, 'state_hash': digest(rows)}
        store = Mock()
        store.find_one.return_value = saved
        conflict = row('T1H', value=22)
        conflict['source_hash'] = 'conflicting'
        with self.assertRaisesRegex(ValueError, '^ambiguous_same_time_correction$'):
            mongo_current.write_grid(store, 60, 127, [conflict])
        store.replace_one.assert_not_called()
        store.insert_one.assert_not_called()
        self.assertEqual(saved['rows'][0]['measurement']['value'], 21)
        self.assertEqual(saved['revision'], 1)

    def test_current_sky_uses_current_kst_hour_not_next_forecast(self):
        current=NOW.replace(minute=0)
        old=row('SKY','vsrt',current,current-timedelta(minutes=30),1)
        following=row('SKY','vsrt',current+timedelta(hours=1),current-timedelta(minutes=30),4)
        document=project_grid([old,following])
        state=current_sky(document,NOW,{'forecast_max_age_minutes':120})
        self.assertEqual(state['sky_code'],1)
        expired=current_sky(document,NOW+timedelta(hours=3),{'forecast_max_age_minutes':120})
        self.assertIsNone(expired['sky_code'])

    def test_es_conflict_reloads_and_identical_replay_does_not_write(self):
        saved=None;sequence=0;conflict=True
        def fake_request(base,method,path,body=None):
            nonlocal saved,sequence,conflict
            if method=='GET':
                return copy.deepcopy({'_source':saved,'_seq_no':sequence,'_primary_term':1}) if saved else None
            if conflict:
                conflict=False
                raise HTTPError('http://test',409,'conflict',None,None)
            if saved:self.assertIn(f'if_seq_no={sequence}',path)
            else:self.assertIn('op_type=create',path)
            saved=copy.deepcopy(body);sequence+=1
            return {'result':'updated','_shards':{'failed':0}}
        with patch.dict(os.environ,{'ES_URL':'http://test','WEATHER_INDEX':'weather-test'}),patch.object(es_latest,'request',side_effect=fake_request):
            self.assertTrue(es_latest.write_grid(60,127,observation()))
            self.assertFalse(es_latest.write_grid(60,127,observation()))
            self.assertIn('guidance',saved['view']);self.assertIn('music_weather',saved['view'])
            newer=observation(NOW.replace(minute=10))
            self.assertTrue(es_latest.write_grid(60,127,newer))
            self.assertFalse(es_latest.write_grid(60,127,observation()))
            self.assertEqual(saved['revision'],2)

    def test_bad_grid_and_absent_config_fail_before_network(self):
        with self.assertRaises(ValueError):es_latest.grid_id(True,127)
        with patch.dict(os.environ,{'ES_URL':'','WEATHER_INDEX':''}),patch.object(es_latest,'request') as request:
            with self.assertRaises(RuntimeError):es_latest.read_grid(60,127)
            request.assert_not_called()

    def test_es_read_recalculates_features_instead_of_returning_stored_flags(self):
        with patch.object(es_latest,'configuration',return_value=('test','weather')),patch.object(es_latest,'load',return_value={
            '_source':{'schema_version':1,'view':{'nx':60,'ny':127,'observation':None,'forecast':[],'guidance':{'stale':False}}}}):
            result=es_latest.read_grid(60,127)
            self.assertIn('evaluated_at',result)
            self.assertNotEqual(result['guidance'],{'stale':False})


if __name__=='__main__':unittest.main()
