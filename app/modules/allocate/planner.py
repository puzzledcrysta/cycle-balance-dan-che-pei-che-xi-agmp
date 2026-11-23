from fractions import Fraction
from app.errors import DataError
from app.modules.domain.rule import evaluate
from app.modules.domain.values import parts, whole


def assess(fields):
    """独立复算一个调车区：逐路段申请流量与缺车数（站点缺口合计）。"""
    _, proof = evaluate(fields)
    flows, moved, unmet = {}, 0, 0
    for label, value, _ in proof:
        if '>' in label:
            flows[label] = value
        elif label == '调配完成':
            moved = value
        else:
            unmet = value
    return flows, moved + unmet


def declared(fields):
    """该区申报的各路容量。"""
    capacity = {}
    for item in parts(fields['可行车路']):
        label, raw, _ = item.split(':')
        capacity[label] = whole(raw, 1, 1000)
    return capacity


def split(capacity, weights, caps):
    """按缺车权重水注式切分容量：比例配额、最大余量法取整、业务键升序定序，不超出申请量。"""
    alloc = {key: 0 for key in caps}
    active = {key for key, cap in caps.items() if cap > 0}
    remaining = capacity
    while remaining > 0 and active:
        total = sum(weights.get(key, 0) for key in active)
        if total <= 0:
            weights = {**weights, **{key: 1 for key in active}}
            total = len(active)
        quotas = {key: Fraction(remaining * weights[key], total) for key in active}
        floors = {key: quota.numerator // quota.denominator for key, quota in quotas.items()}
        give = {key: min(floors[key], caps[key] - alloc[key]) for key in active}
        leftover = remaining - sum(give.values())
        order = sorted(active, key=lambda key: (-(quotas[key] - floors[key]), key))
        for key in order:
            if not leftover:
                break
            if give[key] < caps[key] - alloc[key]:
                give[key] += 1
                leftover -= 1
        granted = sum(give.values())
        for key, amount in give.items():
            alloc[key] += amount
        remaining -= granted
        active = {key for key in active if alloc[key] < caps[key]}
        if not granted:
            break
    return alloc


def finish(fields, shared, allocated):
    """共享路段按核定额截断后复算，得到可执行车路明细与缺口。"""
    items = []
    for item in parts(fields['可行车路']):
        label, _, distance = item.split(':')
        if label in shared:
            amount = allocated.get(label, 0)
            if amount:
                items.append(f'{label}:{amount}:{distance}')
        else:
            items.append(item)
    capped = {**fields, '可行车路': ';'.join(items) if items else '无'}
    _, proof = evaluate(capped)
    rows, unmet = [], 0
    for label, value, unit in proof:
        if label == '未满足':
            unmet = value
        rows.append({'metric': label, 'value': str(value), 'unit': unit})
    return rows, unmet


def plan(ready):
    """从可采信记录出配车单：识别共享路段并按缺车数核定，返回 (配车单列表, {路段: 容量})。"""
    base = {}
    for record in ready:
        try:
            requested, shortage = assess(record['fields'])
        except DataError:
            continue  # 域内已隔离的记录不出单
        base[record['key']] = {'fields': record['fields'], 'requested': requested, 'shortage': shortage}
    users = {}
    for key, sheet in base.items():
        for label, flow in sheet['requested'].items():
            users.setdefault(label, {})[key] = flow
    shared = {label: holders for label, holders in users.items() if len(holders) > 1}
    capacity = {label: min(declared(base[key]['fields'])[label] for key in holders)
                for label, holders in shared.items()}
    shares = {label: split(cap, {key: base[key]['shortage'] for key in shared[label]}, shared[label])
              for label, cap in capacity.items()}
    sheets = []
    for key in sorted(base):
        sheet = base[key]
        allocated = {label: shares[label][key] if label in shared else flow
                     for label, flow in sheet['requested'].items()}
        proof, unmet = finish(sheet['fields'], shared, allocated)
        sheets.append({'district': key, 'shortage': sheet['shortage'], 'requested': sheet['requested'],
                       'allocated': allocated, 'proof': proof, 'unmet': unmet})
    return sheets, capacity
