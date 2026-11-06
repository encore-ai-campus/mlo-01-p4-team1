"""Image VOLUME paths must have stable run-owned names across recreation."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mlo_runtime.lab import Lab


class LabVolumeTests(unittest.TestCase):
    def prepare(self, service, image_volumes):
        lab = Lab('volume-unit')
        lab.configure(service)
        with patch.object(lab, 'docker', return_value=SimpleNamespace(stdout=json.dumps(image_volumes).encode())):
            lab._own_image_volumes()
        return lab, json.loads(lab.compose_path.read_text())

    def test_unmounted_image_paths_get_project_volumes_and_recreation_reuses_names(self):
        lab, config = self.prepare({'image': 'mongo-fixture', 'volumes': ['data:/data/db']},
                                   {'/data/db': {}, '/data/configdb': {}})
        service = config['services'][lab.service]
        self.assertIn('data:/data/db', service['volumes'])
        extra = [v for v in service['volumes'] if v.endswith(':/data/configdb')]
        self.assertEqual(len(extra), 1)
        self.assertEqual(config['volumes'][extra[0].split(':')[0]]['labels']['mlo.run'], lab.project)
        before = lab.compose_path.read_bytes()
        with patch.object(lab, 'docker') as docker:
            lab._own_image_volumes()
            docker.assert_not_called()
        self.assertEqual(lab.compose_path.read_bytes(), before)

    def test_parent_bind_covers_child_image_volume_but_child_does_not_cover_parent(self):
        for mount, expected in [('data:/data', 1), ('data:/data/db', 2)]:
            with self.subTest(mount=mount):
                lab, config = self.prepare({'image': 'fixture', 'volumes': [mount]}, {'/data': {}, '/data/db': {}})
                self.assertEqual(len(config['services'][lab.service]['volumes']), expected)

    def test_anonymous_short_and_long_mounts_become_owned_named_volumes(self):
        lab, config = self.prepare({'image': 'fixture', 'volumes': ['/one', {'type': 'volume', 'target': '/two'}]}, {})
        mounts = config['services'][lab.service]['volumes']
        self.assertTrue(mounts[0].startswith('image_'))
        self.assertIn(mounts[1]['source'], config['volumes'])
