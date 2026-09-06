import pytest

from cr.store import BudgetError, Store


@pytest.fixture
def ledger(tmp_path):
    store = Store(tmp_path / 'ledger.sqlite')
    keys = store.bootstrap(1000)
    yield store, keys
    store.close()


def usage(total):
    return {'input_tokens': total, 'output_tokens': 0}


def test_checkpoint_spends_reservation_once_and_can_use_its_own_hold(ledger):
    store, _ = ledger
    store.reserve('a', 'user1', 'm', 900)
    store.running('a')
    assert store.checkpoint('a', usage(100))
    assert store.checkpoint('a', usage(100))
    current = store.usage('user1')
    assert (current['used'], current['held'], current['remaining']) == (100, 800, 100)
    assert store.recent('user1')[0]['reserved'] == 800
    assert store.db.execute("SELECT reserved FROM requests WHERE id='a'").fetchone()[0] == 900
    assert store.token_counter() == 100
    # The other reservation is protected even though this request can use its own.
    store.reserve('b', 'user1', 'm', 100)
    assert store.checkpoint('a', usage(900))
    assert not store.checkpoint('a', usage(901))
    with pytest.raises(BudgetError):
        store.reserve('c', 'user1', 'm', 1)


def test_exact_budget_can_return_its_completed_answer(ledger):
    store, _ = ledger
    store.reserve('r', 'user1', 'm', 1000)
    store.running('r')
    assert store.checkpoint('r', usage(0))
    assert store.checkpoint('r', usage(1000))
    store.finish('r', 'completed', usage(1000))
    assert store.usage('user1')['remaining'] == 0
    assert store.recent('user1')[0]['status'] == 'completed'


@pytest.mark.parametrize('end', ['disconnect', 'restart'])
def test_unknown_retains_known_charge_and_only_the_unspent_hold(ledger, end):
    store, _ = ledger
    store.reserve('r', 'user1', 'm', 900)
    store.running('r')
    store.checkpoint('r', usage(100))
    if end == 'restart':
        store.recover()
    else:
        store.finish('r', 'cancelled', error='client_disconnected')
    current = store.usage('user1')
    assert (current['used'], current['held'], current['remaining'], current['unresolved']) == (100, 800, 100, 1)
    assert store.token_counter() == 100
    assert store.reconcile('r', 150)
    assert store.usage('user1')['remaining'] == 850


def test_reset_during_generation_only_counts_later_consumption(ledger):
    store, _ = ledger
    store.reserve('r', 'user1', 'm', 900)
    store.running('r')
    store.checkpoint('r', usage(100))
    store.reset_all_usage()
    assert store.usage('user1')['held'] == 800
    assert store.checkpoint('r', usage(150))
    current = store.usage('user1')
    assert (current['used'], current['held'], current['remaining']) == (50, 750, 200)
    store.finish('r', 'completed', usage(150))
    assert store.usage('user1')['used'] == 50
    assert store.token_counter() == 150
    assert store.statistics(0, 10**12)['users'][0]['total_tokens'] == 150


def test_delayed_tool_usage_merges_once_after_reset(ledger):
    store, _ = ledger
    store.reserve('tool', 'user1', 'm', 200)
    store.running('tool')
    store.finish('tool', 'completed')
    store.reset_all_usage()
    store.reserve('continuation', 'user1', 'm', 300)
    store.running('continuation')
    for _ in range(2):
        assert store.checkpoint('continuation', usage(240), ['tool'])
    current = store.usage('user1')
    assert (current['used'], current['held'], current['unresolved']) == (240, 60, 0)
    store.finish('continuation', 'completed', usage(260))
    assert store.token_counter() == 260
    assert store.usage('user1')['remaining'] == 740
    assert store.statistics(0, 10**12)['users'][0]['total_tokens'] == 260


def test_checkpoint_cannot_settle_another_user_or_mutate_finished_request(ledger):
    store, _ = ledger
    store.reserve('foreign', 'user2', 'm', 100)
    store.running('foreign')
    store.finish('foreign', 'completed')
    store.reserve('r', 'user1', 'm', 200)
    store.running('r')
    assert store.checkpoint('r', usage(50), ['foreign'])
    assert store.usage('user2')['held'] == 100
    store.finish('r', 'completed', usage(50))
    assert not store.checkpoint('r', usage(500))
    assert store.token_counter() == 50
    assert store.usage('user1')['used'] == 50


def test_user_concurrency_defaults_and_changes_preserve_budget_and_keys(ledger):
    store, keys = ledger
    assert [user['concurrent_limit'] for user in store.users()] == [1] * 4
    assert store.authenticate(keys['user1'])['concurrent_limit'] == 1
    updated = store.update_user('user1', {'concurrent_limit': 6})
    assert updated['concurrent_limit'] == 6 and updated['budget'] == 1000
    assert store.authenticate(keys['user1'])['concurrent_limit'] == 6
    assert store.usage('user2')['concurrent_limit'] == 1


@pytest.mark.parametrize('value', [True, False, 0, 7, -1, 1.5, 2.0, '2', None])
def test_invalid_concurrency_does_not_partially_update_user(ledger, value):
    store, _ = ledger
    with pytest.raises(ValueError):
        store.update_user('user1', {'name': 'changed', 'concurrent_limit': value})
    user = store.usage('user1')
    assert user['concurrent_limit'] == 1 and user['name'] == '用户1'


def test_concurrency_migration_is_idempotent_and_keeps_old_reservations(tmp_path):
    path = tmp_path / 'legacy.sqlite'
    store = Store(path)
    keys = store.bootstrap(1000)
    store.reserve('r', 'user1', 'm', 900)
    store.running('r')
    store.checkpoint('r', usage(100))
    store.finish('r', 'failed')
    with store.db:
        store.db.execute('ALTER TABLE users DROP COLUMN concurrent_limit')
    store.close()
    for limit in (1, 4):
        store = Store(path)
        current = store.usage('user1')
        assert (current['used'], current['held'], current['remaining']) == (100, 800, 100)
        assert store.authenticate(keys['user1'])['concurrent_limit'] == limit
        assert store.db.execute("SELECT reserved FROM requests WHERE id='r'").fetchone()[0] == 900
        assert store.token_counter() == 100
        store.update_user('user1', {'concurrent_limit': 4})
        store.close()
