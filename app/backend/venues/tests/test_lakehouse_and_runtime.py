"""Tests for DB catalog mirroring, Track 2 raw/delta lakehouse export, and docker secret keys."""
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.management import call_command
from django.test import Client, TestCase, override_settings

from backend.venues.models.catalog import VenueCatalogRecord
from backend.venues.models.outbox import VenueOutbox
from backend.venues.models.status_overlay import VenueStatusOverlay
from backend.venues.repositories.csv_lazy_repo import CsvLazyRepository, get_venue_repo
from backend.venues.services.lakehouse_sync import (
    export_lakehouse_snapshot,
    sync_catalog_to_db,
)
from backend.venues.tests.test_admin_closure_and_monitoring import ROLES
from backend.workspaces.models import BusinessEntity, Workspace
from backend.workspaces.tests.test_platform import Helpers, TEST_CONFIG
from pipeline.venues.catalog_normalize import (
    deduplicate_latest_silver,
    normalize_venue_record,
    project_gold_records,
)
from pipeline.venues.submit_spark import build_package


class LakehouseAndRuntimeTests(TestCase):
    def test_app_runtime_secret_keys_allow_venue_platform_and_kibana_keys(self):
        runtime_dir = Path(__file__).resolve().parents[3] / 'docker' / 'runtime'
        if str(runtime_dir) not in sys.path:
            sys.path.insert(0, str(runtime_dir))
        import app_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            secret_file = root / 'app-env.json'
            secret_file.write_text(
                json.dumps({
                    'DJANGO_SECRET_KEY': 'test-key',
                    'PLATFORM_LOCAL_MVP': '1',
                    'VENUE_DATA_DIR': '/data/venues',
                    'VENUE_KAFKA_BOOTSTRAP': 'kafka:9092',
                    'VENUE_KAFKA_TOPIC': 'venue.events.v1',
                    'VENUE_OUTBOX_KAFKA_BOOTSTRAP': 'kafka:9092',
                    'VENUE_OUTBOX_KAFKA_TOPIC': 'venue.outbox.v1',
                    'VENUE_OBSERVATION_SPOOL_PATH': '/var/log/venues.ndjson',
                    'VENUE_RAW_S3_BUCKET': 'example-only-raw',
                    'VENUE_LAKE_S3_BUCKET': 'example-only-lake',
                    'VENUE_DATASET_VERSION': 'v1',
                    'ADMIN_OPERATIONS_DASHBOARD_URL': 'https://ops.example.org/ops/app/dashboards',
                    'ADMIN_LOG_DASHBOARD_URL': 'https://ops.example.org/ops/app/dashboards',
                    'ADMIN_KIBANA_ALLOWED_HOSTS': 'ops.example.org',
                    'GEMINI_ENABLED': '0',
                }),
                encoding='utf-8',
            )
            fake_stat = MagicMock(st_mode=stat.S_IFREG | 0o400, st_size=secret_file.stat().st_size)
            with patch.object(Path, 'stat', return_value=fake_stat):
                parsed = app_runtime.secret_environment(secret_file, root=root)
            self.assertEqual(parsed['VENUE_RAW_S3_BUCKET'], 'example-only-raw')
            self.assertEqual(parsed['PLATFORM_LOCAL_MVP'], '1')

            bad_file = root / 'bad-env.json'
            bad_file.write_text(json.dumps({'UNSUPPORTED_CUSTOM_KEY': 'nope'}), encoding='utf-8')
            fake_bad_stat = MagicMock(st_mode=stat.S_IFREG | 0o400, st_size=bad_file.stat().st_size)
            with patch.object(Path, 'stat', return_value=fake_bad_stat):
                with self.assertRaisesRegex(ValueError, 'unsupported secret environment keys'):
                    app_runtime.secret_environment(bad_file, root=root)

    def test_sync_catalog_to_db_idempotent_and_fallback_reads_without_csv(self):
        first = sync_catalog_to_db(gus=['강남구'], dataset_version='v1', limit=5)
        self.assertEqual(first['venues_scanned'], 5)
        self.assertEqual(first['created'], 5)
        self.assertEqual(first['updated'], 0)
        self.assertEqual(VenueCatalogRecord.objects.count(), 5)

        second = sync_catalog_to_db(gus=['강남구'], dataset_version='v1', limit=5)
        self.assertEqual(second['created'], 0)
        self.assertEqual(second['updated'], 5)

        # Stripped container without region CSVs or sample CSVs falls back to VenueCatalogRecord DB mirror
        sample_record = VenueCatalogRecord.objects.order_by('venue_id').first()
        repo = CsvLazyRepository()
        with (
            patch.object(repo, '_find_region_csv', return_value=None),
            patch.object(repo, '_load_fallback_venues_for_gu', return_value=[]),
        ):
            repo.refresh_source_data()
            venues = repo.get_venues_by_gu('강남구')
            self.assertEqual(len(venues), 5)
            found = repo.get_venue_by_id(sample_record.venue_id)
            self.assertIsNotNone(found)
            self.assertEqual(found['venue_id'], sample_record.venue_id)
            menus = repo.get_menus_by_venue(sample_record.venue_id)
            self.assertEqual(len(menus), len(sample_record.menus_payload))

    def test_export_lakehouse_snapshot_writes_manifest_and_uploads_to_s3(self):
        sync_catalog_to_db(gus=['강남구'], dataset_version='v1', limit=3)
        target_vid = VenueCatalogRecord.objects.order_by('venue_id').first().venue_id
        VenueStatusOverlay.objects.create(venue_id=target_vid, status='CLOSED_HIDDEN', version=3)
        VenueOutbox.objects.create(
            event_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            event_type='CLOSURE_RESOLVED_CLOSED_HIDDEN',
            venue_id=target_vid,
            payload={'status': 'CLOSED_HIDDEN'},
        )

        mock_s3 = MagicMock()
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch('boto3.client', return_value=mock_s3),
        ):
            res = export_lakehouse_snapshot(
                gus=['강남구'],
                dataset_version='v1',
                output_dir=tmpdir,
                s3_bucket='example-only-raw',
            )
            self.assertEqual(res['raw_prefix'], 'raw/venues/v1/')
            self.assertEqual(res['lake_prefix'], 'lake/venues/v1/v1/')
            self.assertGreaterEqual(res['venues_exported'], 3)
            self.assertEqual(res['outbox_exported'], 1)
            self.assertEqual(len(res['s3_uploaded']), 3)
            self.assertEqual(mock_s3.upload_file.call_count, 3)

            manifest_path = Path(res['files']['manifest.json'])
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            self.assertEqual(manifest['query'], 'venues-catalog')
            catalog_bytes = Path(res['files']['venues_catalog.ndjson']).read_bytes()
            self.assertEqual(manifest['files']['venues_catalog.ndjson'], hashlib.sha256(catalog_bytes).hexdigest())

            rows = [
                json.loads(line)
                for line in Path(res['files']['venues_catalog.ndjson']).read_text(encoding='utf-8').splitlines()
                if line.strip()
            ]
            # Normalize -> deduplicate Silver -> Gold projection
            silver = deduplicate_latest_silver(
                rows + [normalize_venue_record({'venue_id': target_vid, 'gu_name': '강남구', 'status_code': 'OPEN', 'revision': 1})],
                dataset_version='v1',
            )
            closed_silver = next(r for r in silver if r['venue_id'] == target_vid)
            self.assertEqual(closed_silver['status_code'], 'CLOSED_HIDDEN')
            self.assertEqual(closed_silver['revision'], 3)
            self.assertFalse(closed_silver['is_searchable'])

            gold = project_gold_records(silver)
            self.assertFalse(any(v['venue_id'] == target_vid for v in gold['venues']))
            gangnam_gold = next(g for g in gold['gu_summaries'] if g['gu_name'] == '강남구')
            self.assertEqual(gangnam_gold['hidden_venues'], 1)

    def test_management_commands_and_spark_package(self):
        out = MagicMock()
        with tempfile.TemporaryDirectory() as tmpdir:
            call_command(
                'sync_venues_catalog',
                target='all',
                gu=['강남구'],
                limit=2,
                output_dir=tmpdir,
                stdout=out,
            )
            payload = json.loads(out.write.call_args[0][0])
            self.assertEqual(payload['db']['venues_scanned'], 2)
            self.assertTrue(Path(payload['lakehouse']['files']['manifest.json']).exists())

            zip_path = build_package(Path(tmpdir) / 'venues-code.zip')
            self.assertTrue(zip_path.is_file())

    @override_settings(ADMIN_PASSKEY_REQUIRED=False, LOCAL_DEMO=True)
    def test_metrics_api_includes_lakehouse_status(self):
        sync_catalog_to_db(gus=['강남구'], dataset_version='v1', limit=2)
        from django.contrib.auth import get_user_model
        from backend.common.admin_roles import assign_role

        admin_user = get_user_model().objects.create_user('lake_admin', password='Test-Password-123!')
        assign_role(admin_user, 'OPS_ADMIN')
        client = Client()
        client.force_login(admin_user)
        with patch.dict(os.environ, {'VENUE_RAW_S3_BUCKET': 'example-only-raw', 'VENUE_LAKE_S3_BUCKET': 'example-only-lake'}, clear=False):
            resp = client.get('/api/admin/venues/metrics')
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['lakehouse']['db_synced_venues_count'], 2)
        self.assertTrue(body['lakehouse']['s3_raw_bucket_configured'])
        self.assertTrue(body['lakehouse']['s3_lake_bucket_configured'])
        self.assertEqual(body['lakehouse']['spark_job_module'], 'pipeline.venues.spark_job')


class OptionAVenueExposureTests(Helpers, TestCase):
    """Option A 정책 검증: 공공 카탈로그 기본 전체 노출 + 점주 가입/인증/신규등록 시 파트너 승격."""

    def setUp(self):
        get_venue_repo().clear_cache()

    def tearDown(self):
        get_venue_repo().clear_cache()

    def _post_json(self, client: Client, path: str, payload: dict):
        csrf = client.cookies.get('csrftoken')
        if not csrf:
            client.get('/api/auth/me')
            csrf = client.cookies.get('csrftoken')
        headers = {'HTTP_X_CSRFTOKEN': csrf.value} if csrf else {}
        return client.post(path, data=json.dumps(payload), content_type='application/json', **headers)

    @override_settings(**TEST_CONFIG)
    def test_direct_registered_venue_appears_immediately_with_partner_and_event_permissions(self):
        business = self.user('opt-a-owner', 'BUSINESS')
        biz_client = self.client_for(business)
        consumer = self.user('opt-a-consumer', 'CONSUMER')
        consumer_client = self.client_for(consumer)

        initial_list = consumer_client.get('/api/venues?sigungu=강남구').json()
        self.assertTrue(initial_list['success'])
        self.assertGreater(initial_list['total_count'], 0)

        reg_res = self._post_json(
            biz_client,
            '/api/owner/venues/register',
            {
                'venue_name': '옵션A직접등록카페',
                'gu_name': '강남구',
                'dong_name': '역삼동',
                'road_address': '서울 강남구 테헤란로 123',
                'category': 'CAFE',
                'avg_price': 8500,
                'business_number': '211-88-12345',
            },
        )
        self.assertEqual(reg_res.status_code, 200, reg_res.content.decode('utf-8', 'replace'))
        reg_data = reg_res.json()
        self.assertTrue(reg_data['success'])
        new_vid = reg_data['venue_id']

        search_data = consumer_client.get('/api/venues?sigungu=강남구&q=옵션A직접등록카페').json()
        matched = [v for v in search_data['venues'] if v['venue_id'] == new_vid]
        self.assertEqual(len(matched), 1)
        self.assertTrue(matched[0]['is_partner'])

        detail_res = consumer_client.get(f'/api/venues/{new_vid}')
        self.assertEqual(detail_res.status_code, 200)
        detail_venue = detail_res.json()['venue']
        self.assertEqual(detail_venue['venue_id'], new_vid)
        self.assertTrue(detail_venue['is_partner'])

        event_res = self._post_json(
            biz_client,
            f'/api/owner/venues/{new_vid}/events',
            {'title': '신규 오픈 아메리카노 1+1', 'total_quantity': 15, 'duration_hours': 3},
        )
        self.assertEqual(event_res.status_code, 200)
        self.assertTrue(event_res.json()['success'])

    @override_settings(**TEST_CONFIG)
    def test_claimed_bridge_and_active_workspace_promote_catalog_venue_to_partner(self):
        business = self.user('opt-a-claim-owner', 'BUSINESS')
        biz_client = self.client_for(business)
        consumer = self.user('opt-a-claim-consumer', 'CONSUMER')
        consumer_client = self.client_for(consumer)

        default_venues = consumer_client.get('/api/venues?sigungu=강남구').json()['venues']
        plain_candidates = [v for v in default_venues if not v.get('is_partner')]
        self.assertGreaterEqual(len(plain_candidates), 2)
        claim_target = plain_candidates[0]
        ws_target = plain_candidates[1]

        claim_res = self._post_json(
            biz_client,
            '/api/owner/venues/claim',
            {'venue_id': claim_target['venue_id'], 'business_number': '123-45-67890'},
        )
        self.assertEqual(claim_res.status_code, 200, claim_res.content.decode('utf-8', 'replace'))
        claimed_vid = claim_res.json()['venue_id']

        claim_detail_res = consumer_client.get(f'/api/venues/{claimed_vid}')
        self.assertEqual(claim_detail_res.status_code, 200)
        claim_detail = claim_detail_res.json()['venue']
        self.assertTrue(claim_detail['is_partner'])
        partner_list = consumer_client.get('/api/venues?sigungu=강남구&partner_exact=1').json()['venues']
        self.assertIn(claimed_vid, [v['venue_id'] for v in partner_list])

        ws_vid = ws_target['venue_id']
        entity = BusinessEntity.objects.create(
            registration_lookup='opt-a-ws-entity',
            registration_ciphertext='enc',
            display_name=ws_target['venue_name'],
        )
        Workspace.objects.create(
            business=entity,
            display_name=ws_target['venue_name'],
            address_ciphertext='enc',
            location_key=f'catalog:{ws_vid}',
            status='ACTIVE',
        )

        self.assertEqual(Client().get(f'/api/venues/{ws_vid}').status_code, 401)

        mock_playlist = {'id': 'pl-opt-a', 'name': '비 오는 날 공식 선곡'}
        with patch('backend.workspaces.music.public_applied_playlist', return_value=mock_playlist), \
             patch('backend.venues.views.detail_view.public_applied_playlist', return_value=mock_playlist):
            ws_detail = consumer_client.get(f'/api/venues/{ws_vid}').json()['venue']
            self.assertTrue(ws_detail['is_partner'])
            self.assertEqual(ws_detail['applied_playlist'], mock_playlist)
            self.assertEqual(ws_detail['music_banner']['banner_type'], 'APPROVED_PLAYLIST')
            self.assertEqual(ws_detail['music_banner']['playlist_id'], 'pl-opt-a')

            ws_list = consumer_client.get(f"/api/venues?sigungu=강남구&q={ws_target['venue_name']}").json()['venues']
            ws_match = next(v for v in ws_list if v['venue_id'] == ws_vid)
            self.assertTrue(ws_match['is_partner'])
            self.assertEqual(ws_match['music_banner']['playlist_id'], 'pl-opt-a')

        with override_settings(PLATFORM_ENABLED=False):
            anon_client = Client()
            with patch('backend.workspaces.music.public_applied_playlist', return_value=None), \
                 patch('backend.venues.views.detail_view.public_applied_playlist', return_value=None):
                public_detail = anon_client.get(f'/api/venues/{ws_vid}').json()['venue']
                self.assertTrue(public_detail['is_partner'])
                self.assertIsNone(public_detail['applied_playlist'])
                self.assertNotEqual((public_detail.get('music_banner') or {}).get('banner_type'), 'APPROVED_PLAYLIST')

            with patch('backend.workspaces.music.public_applied_playlist', return_value=mock_playlist), \
                 patch('backend.venues.views.detail_view.public_applied_playlist', return_value=mock_playlist):
                public_playlist_detail = anon_client.get(f'/api/venues/{ws_vid}').json()['venue']
                self.assertEqual(public_playlist_detail['applied_playlist'], mock_playlist)
                self.assertEqual(public_playlist_detail['music_banner']['banner_type'], 'APPROVED_PLAYLIST')
