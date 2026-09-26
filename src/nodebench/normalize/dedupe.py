from __future__ import annotations


def _ordered_union(first, second):
    merged = list(first)
    existing = set(merged)
    for value in second:
        if value not in existing:
            merged.append(value)
            existing.add(value)
    return merged


def _merge_group(group):
    base = group[0].model_copy()
    for other in group[1:]:
        base.source_ids = _ordered_union(base.source_ids, other.source_ids)
        base.raw_refs = _ordered_union(base.raw_refs, other.raw_refs)
        if not base.remarks and other.remarks:
            base.remarks = other.remarks
    return base


def _dedupe_kind(items):
    groups = {}
    for item in items:
        fingerprint = item.fingerprint
        if fingerprint in groups:
            groups[fingerprint].append(item)
        else:
            groups[fingerprint] = [item]
    return [_merge_group(groups[key]) for key in sorted(groups)]


def dedupe(nodes, endpoints):
    proxies = _dedupe_kind(nodes)
    edges = _dedupe_kind(endpoints)
    return proxies, edges
