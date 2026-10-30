"""An accepted Twitch identity cannot return to the frozen GroupID queue."""
from copy import deepcopy
from datetime import timedelta

from radar_backend.domain.official_queue import make_queue
from scripts.twitch_official_queue import is_twitch_queue_candidate, sync_twitch_queue
from tests.test_twitch_steam_admission import NOW, row


def test_completed_twitch_admission_survives_frozen_rebuild_and_tracking_expiry():
    record = row()
    record.update(followers=None, follower_checked_at=None, follower_source=None)
    old = {'appid': 123, 'release_date': record['release_start'],
           'group_id64': None, 'queue_source': 'normal',
           'group_resolution': {'status': 'not_found'}}
    checkpoint = {'pending_candidates': {'123': deepcopy(old)}, 'official_results': {},
                  'attempt_events': [], 'cursor': 47}
    batch = {'records': [record], 'follower_candidates': [], 'active_twitch_appids': [123],
             'state_updates': {'123': {'status': 'accepted'}}}
    recovered = sync_twitch_queue(checkpoint, batch, NOW)
    assert checkpoint['pending_candidates']['123'] == old
    assert '123' not in recovered['pending_candidates']
    frozen = [{'appid': aid, 'release_date': record['release_start']}
              for aid in [123, *range(10000, 11316)]]
    groups = [{'appid': aid, 'group_short_id': None} for aid in range(10000, 11358)]
    legacy = {'cohort': 'steam_fresh_20260922_post_adult_1317_near_release',
              'official_results': {str(aid): {} for aid in range(10000, 11316)}}
    for clock in (NOW, NOW + timedelta(days=45)):
        queue, _ = make_queue(recovered, frozen, legacy, groups, {'games': []}, {},
            {'games': {}}, {'verified': {}}, now=clock,
            is_twitch_queue_candidate=is_twitch_queue_candidate,
            cached_follower=lambda *args: None)
        assert not queue
        assert '123' not in recovered['pending_candidates']
        assert recovered['official_results'] == {}
        assert recovered['cursor'] == 47

