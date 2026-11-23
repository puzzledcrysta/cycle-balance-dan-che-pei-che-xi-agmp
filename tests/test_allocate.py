from concurrent.futures import ThreadPoolExecutor
import pytest
from app.modules.allocate.planner import split
from app.modules.allocate.service import sign

CSV = '\n'.join([
    '调车区,修订号,提交时间,站点余缺,可行车路,原始备注',
    'D01,1,2026-11-18T09:30:00+08:00,甲:6;乙:-6,甲>乙:3:1,缺车最多',
    'D02,1,2026-11-18T09:30:00+08:00,甲:3;乙:-3,甲>乙:3:1,缺车次之',
    'D03,1,2026-11-18T09:30:00+08:00,甲:2;乙:-2,甲>乙:3:1,缺车最少',
    'D04,1,2026-11-18T09:30:00+08:00,丙:2;丁:-2,丙>丁:2:1,独享路段',
    '',
])


@pytest.fixture
def anchor(client):
    return client.post('/api/runs', json={'data': CSV}).json()['manifest']['run_id']


def sheet(board, district):
    return next(item for item in board['sheets'] if item['district'] == district)


def test_split_proportional_to_shortage():
    assert split(3, {'A': 6, 'B': 3}, {'A': 3, 'B': 3}) == {'A': 2, 'B': 1}


def test_split_respects_caps_and_redistributes():
    assert split(5, {'A': 100, 'B': 10}, {'A': 1, 'B': 10}) == {'A': 1, 'B': 4}


def test_split_tie_breaks_by_district_key():
    assert split(1, {'B': 2, 'A': 2}, {'B': 1, 'A': 1}) == {'A': 1, 'B': 0}


def test_split_covers_all_requests_when_capacity_suffices():
    assert split(5, {'A': 1, 'B': 100}, {'A': 2, 'B': 2}) == {'A': 2, 'B': 2}


def test_board_lists_signable_sheets_and_shortage(client, anchor):
    board = client.get(f'/api/allocate/{anchor}').json()
    assert [segment['segment'] for segment in board['segments']] == ['甲>乙']
    segment = board['segments'][0]
    assert (segment['capacity'], segment['committed'], segment['remaining']) == (3, 0, 3)
    sheets = {item['district']: item for item in board['sheets']}
    assert {key: item['shortage'] for key, item in sheets.items()} == {'D01': 6, 'D02': 3, 'D03': 2, 'D04': 2}
    assert sheets['D01']['requested'] == {'甲>乙': 3}
    # 缺车多的区分得更多运力，且不拼提交先后
    assert sheets['D01']['allocated'] == {'甲>乙': 2}
    assert sheets['D02']['allocated'] == {'甲>乙': 1}
    assert sheets['D03']['allocated'] == {'甲>乙': 0}
    assert sheets['D04']['allocated'] == {'丙>丁': 2}
    assert sheets['D01']['unmet'] == 4 and sheets['D03']['unmet'] == 2
    assert all(item['status'] == '待签核' for item in board['sheets'])


def test_board_is_reproducible(client, anchor):
    first = client.get(f'/api/allocate/{anchor}')
    second = client.get(f'/api/allocate/{anchor}')
    assert first.status_code == 200 and first.json() == second.json()


def test_sign_commits_allocated_once(client, anchor):
    first = client.post(f'/api/allocate/{anchor}/D01/sign')
    assert first.status_code == 200
    again = client.post(f'/api/allocate/{anchor}/D01/sign')
    assert again.status_code == 200  # 重试幂等
    segment = again.json()['segments'][0]
    assert segment['committed'] == 2  # 没有重复占用
    assert sheet(again.json(), 'D01')['status'] == '已签核'


def test_retry_storm_counts_once(client, anchor):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: sign(anchor, 'D01'), range(8)))
    board = client.get(f'/api/allocate/{anchor}').json()
    assert board['segments'][0]['committed'] == 2


def test_concurrent_signs_never_overcommit(client, anchor):
    districts = ['D01', 'D02', 'D03', 'D04']
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda district: sign(anchor, district), districts * 4))
    assert all(item['run_id'] == anchor for item in results)
    board = client.get(f'/api/allocate/{anchor}').json()
    for segment in board['segments']:
        assert segment['committed'] <= segment['capacity']
    assert board['segments'][0]['committed'] == 3
    assert [item['status'] for item in board['sheets']] == ['已签核'] * 4


def test_return_reallocates_freed_capacity_to_pending(client, anchor):
    assert client.post(f'/api/allocate/{anchor}/D01/sign').status_code == 200
    assert client.post(f'/api/allocate/{anchor}/D02/sign').status_code == 200
    back = client.post(f'/api/allocate/{anchor}/D01/return', json={'note': '车路临时管制'})
    assert back.status_code == 200
    board = back.json()
    assert board['segments'][0]['committed'] == 1
    assert sheet(board, 'D01')['status'] == '已退回'
    assert sheet(board, 'D01')['note'] == '车路临时管制'
    # 腾出的运力回补给尚未签核的 D03
    assert sheet(board, 'D03')['allocated'] == {'甲>乙': 2}
    assert sheet(board, 'D03')['unmet'] == 0
    assert sheet(board, 'D04')['allocated'] == {'丙>丁': 2}
    signed = client.post(f'/api/allocate/{anchor}/D03/sign')
    assert signed.json()['segments'][0]['committed'] == 3


def test_return_is_idempotent_and_terminal(client, anchor):
    assert client.post(f'/api/allocate/{anchor}/D01/sign').status_code == 200
    assert client.post(f'/api/allocate/{anchor}/D01/return', json={'note': '管制'}).status_code == 200
    again = client.post(f'/api/allocate/{anchor}/D01/return', json={'note': '管制'})
    assert again.status_code == 200  # 重试幂等
    assert again.json()['segments'][0]['committed'] == 0
    resign = client.post(f'/api/allocate/{anchor}/D01/sign')
    assert (resign.status_code, resign.json()['code']) == (409, 'SHEET_STATE')


def test_return_unsigned_sheet_conflicts(client, anchor):
    response = client.post(f'/api/allocate/{anchor}/D03/return', json={'note': '多余'})
    assert (response.status_code, response.json()['code']) == (409, 'SHEET_STATE')


def test_unknown_run_and_sheet(client, anchor):
    missing = client.get('/api/allocate/not-a-run')
    assert (missing.status_code, missing.json()['code']) == (404, 'RUN_UNKNOWN')
    for verb in ('sign', 'return'):
        response = client.post(f'/api/allocate/{anchor}/D99/{verb}', json={})
        assert (response.status_code, response.json()['code']) == (404, 'SHEET_UNKNOWN')


def test_shared_capacity_uses_min_declared(client):
    csv = '\n'.join([
        '调车区,修订号,提交时间,站点余缺,可行车路,原始备注',
        'E01,1,2026-11-18T09:30:00+08:00,甲:5;乙:-5,甲>乙:5:1,',
        'E02,1,2026-11-18T09:30:00+08:00,甲:4;乙:-4,甲>乙:2:1,',
        '',
    ])
    run_id = client.post('/api/runs', json={'data': csv}).json()['manifest']['run_id']
    board = client.get(f'/api/allocate/{run_id}').json()
    segment = board['segments'][0]
    assert segment['capacity'] == 2
    used = sum(item['allocated']['甲>乙'] for item in board['sheets'])
    assert used <= segment['capacity']


def test_quarantined_rows_have_no_sheet(client):
    csv = '\n'.join([
        '调车区,修订号,提交时间,站点余缺,可行车路,原始备注',
        'F01,1,2026-11-18T09:30:00+08:00,甲:2;乙:-2,甲>乙:2:1,',
        'F02,1,2026-11-18T09:30:00+08:00,甲:1;乙:-1,甲>丙:1:1,未知站点',
        '',
    ])
    run_id = client.post('/api/runs', json={'data': csv}).json()['manifest']['run_id']
    board = client.get(f'/api/allocate/{run_id}').json()
    assert [item['district'] for item in board['sheets']] == ['F01']
    assert board['segments'] == []
