"""Preparation safety independent of Docker startup and external databases."""

from unittest.mock import MagicMock

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from domains.ads.management.commands.prepare_ads_runtime import prepare


class AdsRuntimeInitializationTests(SimpleTestCase):
    def definitions(self):
        return {
            "ads": {
                "validator": {"kind": "ads"},
                "indexes": [{"name": "ad-id", "keys": [["_id", 1]], "unique": True}],
            },
            "events": {"validator": {"kind": "events"}, "indexes": []},
            "missing": {"validator": {"kind": "new"}, "indexes": []},
        }

    def test_all_existing_validators_are_checked_before_any_missing_creation(self):
        database = MagicMock()
        database.list_collections.return_value = [
            {"name": "ads", "options": {"validator": {"kind": "ads"}}},
            {"name": "events", "options": {"validator": {"kind": "wrong"}}},
        ]
        with self.assertRaises(CommandError):
            prepare(database, self.definitions())
        database.create_collection.assert_not_called()
        database.__getitem__.assert_not_called()
        database.drop_collection.assert_not_called()
        database.command.assert_not_called()

    def test_compatible_existing_data_is_preserved_and_only_missing_collections_created(self):
        database = MagicMock()
        database.list_collections.return_value = [
            {"name": name, "options": {"validator": definition["validator"]}}
            for name, definition in self.definitions().items()
            if name != "missing"
        ]
        prepare(database, self.definitions())
        database.create_collection.assert_called_once_with(
            "missing", validator={"kind": "new"}, validationLevel="strict", validationAction="error"
        )
        database["ads"].create_index.assert_called_once_with(
            [("_id", 1)], name="ad-id", unique=True
        )
        database.drop_collection.assert_not_called()
        database.command.assert_not_called()
        database["ads"].delete_many.assert_not_called()

    def test_permissive_existing_validator_settings_fail_closed(self):
        database = MagicMock()
        database.list_collections.return_value = [
            {"name": "ads", "options": {"validator": {"kind": "ads"}, "validationAction": "warn"}}
        ]
        with self.assertRaises(CommandError):
            prepare(database, self.definitions())
        database.create_collection.assert_not_called()
