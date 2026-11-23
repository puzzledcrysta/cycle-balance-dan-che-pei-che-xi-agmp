import json
from datetime import datetime, timezone
from app.errors import DataError
from app.store import connection
from app.modules.runs.repository import lookup
from app.pipeline import stages
from .planner import plan, split, finish

PENDING, SIGNED, RETURNED = '待签核', '已签核', '已退回'


def stamp():
    return datetime.now(timezone.utc).isoformat()


def pack(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def materialize(db, run_id):
    if db.execute('SELECT 1 FROM sheets WHERE run_id=? LIMIT 1', (run_id,)).fetchone():
        return
    run = lookup(db, run_id)
    ready = stages(run['source'])['quality']['ready']
    sheets, capacity = plan(ready)
    moment = stamp()
    for label, cap in sorted(capacity.items()):
        db.execute('INSERT OR IGNORE INTO segments VALUES (?,?,?,0)', (run_id, label, cap))
    for sheet in sheets:
        db.execute('INSERT OR IGNORE INTO sheets VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (run_id, sheet['district'], sheet['shortage'], pack(sheet['requested']),
                    pack(sheet['allocated']), pack(sheet['proof']), sheet['unmet'], PENDING, '', moment))


def board_of(db, run_id):
    segments = []
    for row in db.execute('SELECT segment,capacity,committed FROM segments WHERE run_id=? ORDER BY segment', (run_id,)):
        segments.append({'segment': row['segment'], 'capacity': row['capacity'],
                         'committed': row['committed'], 'remaining': row['capacity'] - row['committed']})
    sheets = []
    for row in db.execute('SELECT * FROM sheets WHERE run_id=? ORDER BY district', (run_id,)):
        sheets.append({'district': row['district'], 'shortage': row['shortage'],
                       'requested': json.loads(row['requested']), 'allocated': json.loads(row['allocated']),
                       'proof': json.loads(row['proof']), 'unmet': row['unmet'],
                       'status': row['status'], 'note': row['note'], 'changed': row['changed']})
    return {'run_id': run_id, 'segments': segments, 'sheets': sheets}


def board(run_id):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        lookup(db, run_id)
        materialize(db, run_id)
        return board_of(db, run_id)


def sheet_of(db, run_id, district):
    row = db.execute('SELECT * FROM sheets WHERE run_id=? AND district=?', (run_id, district)).fetchone()
    if row is None:
        raise DataError('SHEET_UNKNOWN', '没有此配车单', 404)
    return row


def sign(run_id, district):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        lookup(db, run_id)
        materialize(db, run_id)
        row = sheet_of(db, run_id, district)
        if row['status'] == SIGNED:
            return board_of(db, run_id)  # 重试幂等：运力只占用一次
        if row['status'] != PENDING:
            raise DataError('SHEET_STATE', '已退回的配车单不能再次签核', 409)
        allocated = json.loads(row['allocated'])
        for label, amount in allocated.items():
            ledger = db.execute('SELECT capacity,committed FROM segments WHERE run_id=? AND segment=?',
                                (run_id, label)).fetchone()
            if ledger and ledger['committed'] + amount > ledger['capacity']:
                raise DataError('SEGMENT_EXHAUSTED', f'路段 {label} 剩余容量不足', 409)
        for label, amount in allocated.items():
            db.execute('UPDATE segments SET committed=committed+? WHERE run_id=? AND segment=?',
                       (amount, run_id, label))
        db.execute('UPDATE sheets SET status=?,changed=? WHERE run_id=? AND district=?',
                   (SIGNED, stamp(), run_id, district))
        return board_of(db, run_id)


def hand_back(run_id, district, note=''):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        run = lookup(db, run_id)
        materialize(db, run_id)
        row = sheet_of(db, run_id, district)
        if row['status'] == RETURNED:
            return board_of(db, run_id)  # 重试幂等：运力只释放一次
        if row['status'] != SIGNED:
            raise DataError('SHEET_STATE', '配车单尚未签核，没有可退运力', 409)
        for label, amount in json.loads(row['allocated']).items():
            db.execute('UPDATE segments SET committed=MAX(committed-?,0) WHERE run_id=? AND segment=?',
                       (amount, run_id, label))
        db.execute('UPDATE sheets SET status=?,note=?,changed=? WHERE run_id=? AND district=?',
                   (RETURNED, note.strip(), stamp(), run_id, district))
        resplit(db, run_id, run['source'])
        return board_of(db, run_id)


def resplit(db, run_id, source):
    """把未占用运力按缺车数重新划给尚未签核的配车单。"""
    pending = {}
    for row in db.execute('SELECT district,shortage,requested,allocated FROM sheets WHERE run_id=? AND status=?',
                          (run_id, PENDING)):
        pending[row['district']] = {'shortage': row['shortage'], 'requested': json.loads(row['requested']),
                                    'allocated': json.loads(row['allocated'])}
    if not pending:
        return
    ledgers = list(db.execute('SELECT segment,capacity,committed FROM segments WHERE run_id=?', (run_id,)))
    changed = set()
    for ledger in ledgers:
        label, free = ledger['segment'], ledger['capacity'] - ledger['committed']
        holders = {key: sheet['requested'][label] for key, sheet in pending.items()
                   if sheet['requested'].get(label)}
        if not holders:
            continue
        share = split(free, {key: pending[key]['shortage'] for key in holders}, holders)
        for key, amount in share.items():
            if pending[key]['allocated'].get(label) != amount:
                pending[key]['allocated'][label] = amount
                changed.add(key)
    if not changed:
        return
    fields = {record['key']: record['fields'] for record in stages(source)['quality']['ready']}
    shared = {ledger['segment'] for ledger in ledgers}
    moment = stamp()
    for key in changed:
        sheet = pending[key]
        proof, unmet = finish(fields[key], shared, sheet['allocated'])
        db.execute('UPDATE sheets SET allocated=?,proof=?,unmet=?,changed=? WHERE run_id=? AND district=? AND status=?',
                   (pack(sheet['allocated']), pack(proof), unmet, moment, run_id, key, PENDING))
