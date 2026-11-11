from backend.accounts.models import MemberProfile
from backend.recommendation.models import Catalog, Family, Job, Playlist
from backend.venues.tests.test_consumer_bridge import ApprovedVenueLinkTests
from backend.workspaces.models import ServiceMembership


class ApprovedPlaylistBindingTests(ApprovedVenueLinkTests):
    def test_only_owner_approved_playlist_is_visible_and_revocation_hides_it(self):
        workspace = self.approved_workspace(self.owner, self.fixture_id)
        profile = MemberProfile.objects.get(user_id=self.owner.session['_auth_user_id'])
        other = MemberProfile.objects.get(user_id=self.other_owner.session['_auth_user_id'])
        catalog = Catalog.objects.create(version='binding-test', tracks=[], sha256='x', source_ref='test', source_sha256='x')

        def create_playlist(owner, approved):
            family = Family.objects.create(owner_id=str(owner.owner_id), options={}, policy_version='test', mapping_version='test')
            job = Job.objects.create(family=family, catalog=catalog, request_id='test', status='SUCCEEDED', input={})
            playlist = Playlist.objects.create(family=family, job=job, snapshot={'summary': '승인 선곡표'})
            if approved:
                family.approved_playlist_id = playlist.pk
                family.save(update_fields=['approved_playlist_id'])
            return family, playlist

        family, playlist = create_playlist(profile, True)
        _, unapproved = create_playlist(profile, False)
        _, foreign = create_playlist(other, True)
        for candidate in (unapproved, foreign):
            result = self.apply_playlist(self.owner, workspace, candidate.pk)
            self.assertEqual(result.status_code, 404)
        self.data(self.apply_playlist(self.owner, workspace, playlist.pk))
        self.assertEqual(self.detail()['applied_playlist'], {'id': playlist.pk, 'name': '승인 선곡표'})
        membership = ServiceMembership.objects.get(user=profile.user, kind='BUSINESS')
        membership.status = 'SUSPENDED'
        membership.save(update_fields=['status'])
        self.assertIsNone(self.detail()['applied_playlist'])
        membership.status = 'ACTIVE'
        membership.save(update_fields=['status'])
        self.assertIsNotNone(self.detail()['applied_playlist'])
        family.approved_playlist_id = None
        family.save(update_fields=['approved_playlist_id'])
        self.assertIsNone(self.detail()['applied_playlist'])
