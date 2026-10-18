"""Parse a Finale-exported vector PDF (Maestro font) into note events per part."""
import sys, json, collections, itertools
import pymupdf

PDF = sys.argv[1]
OUT = sys.argv[2]

LETTERS = 'CDEFGAB'
SEMI = [0, 2, 4, 5, 7, 9, 11]
SHARP_ORDER = 'FCGDAEB'
FLAT_ORDER = 'BEADGCF'
CLEF_BOTTOM = {'&': 4 * 7 + 2, 'V': 3 * 7 + 2, '?': 2 * 7 + 4}  # diatonic index of bottom line
HEADS = {'œ': 1.0, '˙': 2.0, 'w': 4.0}
RESTS = {'Œ': 1.0, '‰': 0.5, '∑': None, 'Ó': 2.0, '≈': 0.25}
ACC = {'b': -1, '#': 1, 'n': 0}
DYN = {'p': 55, 'P': 70, 'F': 85, 'f': 100, 'ƒ': 112}

def label_to_part(t):
    t = t.strip()
    if t.startswith('D'): return 'Descant'
    return {'S': 'Soprano', 'A': 'Alto', 'T': 'Tenor', 'B': 'Bass'}.get(t[:1])

def near(a, b, tol):
    return abs(a - b) <= tol

doc = pymupdf.open(PDF)
systems = []  # list of dicts in reading order

for pno, page in enumerate(doc):
    glyphs, texts = [], []
    for b in page.get_text('rawdict')['blocks']:
        for l in b.get('lines', []):
            for s in l['spans']:
                if s['font'] == 'Maestro':
                    for c in s['chars']:
                        if c['c'].strip():
                            x0, y0, x1, y1 = c['bbox']
                            glyphs.append(dict(c=c['c'], x=c['origin'][0], y=c['origin'][1], x1=x1))
                else:
                    txt = ''.join(c['c'] for c in s['chars'])
                    if txt.strip():
                        texts.append(dict(t=txt, x=s['bbox'][0], y=s['bbox'][3], font=s['font']))
    hlines, vlines, fills = [], [], []
    for d in page.get_drawings():
        kinds = ''.join(i[0] for i in d['items'])
        r = d['rect']
        if d['type'] == 's' and kinds == 'l':
            p1, p2 = d['items'][0][1], d['items'][0][2]
            if abs(p1.y - p2.y) < 0.05:
                hlines.append((min(p1.x, p2.x), max(p1.x, p2.x), p1.y))
            elif abs(p1.x - p2.x) < 0.05:
                vlines.append((p1.x, min(p1.y, p2.y), max(p1.y, p2.y)))
        elif d['type'] == 'f':
            fills.append(dict(kind=kinds, x0=r.x0, y0=r.y0, x1=r.x1, y1=r.y1))
    # staves: groups of 5 long horizontal lines
    long = sorted([h for h in hlines if h[1] - h[0] > 150], key=lambda h: h[2])
    staves, i = [], 0
    while i + 4 < len(long):
        grp = long[i:i + 5]
        gaps = [grp[k + 1][2] - grp[k][2] for k in range(4)]
        if max(gaps) - min(gaps) < 0.2 and all(abs(g[0] - grp[0][0]) < 1 for g in grp):
            staves.append(dict(top=grp[0][2], bot=grp[4][2], sp=sum(gaps) / 4, x0=grp[0][0], x1=grp[0][1]))
            i += 5
        else:
            i += 1
    # systems: left system line spanning several staves
    sys_lines = [v for v in vlines if v[2] - v[1] > 60]
    page_systems = []
    for v in sys_lines:
        members = [s for s in staves if s['top'] >= v[1] - 0.5 and s['bot'] <= v[2] + 0.5 and near(s['x0'], v[0], 2)]
        if len(members) >= 2 and not any(set(id(m) for m in members) == set(id(m) for m in ps['staves']) for ps in page_systems):
            page_systems.append(dict(staves=members))
    page_systems.sort(key=lambda s: s['staves'][0]['top'])
    for ps in page_systems:
        sts = ps['staves']
        # labels
        for st in sts:
            mid = (st['top'] + st['bot']) / 2
            lab = [t for t in texts if t['x'] < st['x0'] and abs(t['y'] - mid) < st['sp'] * 2 and 'Times' in t['font']]
            st['part'] = label_to_part(lab[0]['t']) if lab else None
        order = ['Soprano', 'Alto', 'Tenor', 'Bass'] if len(sts) == 4 else ['Descant', 'Soprano', 'Alto', 'Tenor', 'Bass']
        for st, nm in zip(sts, order):
            if st['part'] not in (None, nm): print('label mismatch', pno, st['part'], nm)
            st['part'] = nm
        # barlines: vertical lines spanning a staff exactly, present in all staves
        xs = collections.defaultdict(int)
        for v in vlines:
            for st in sts:
                if near(v[1], st['top'], 0.4) and near(v[2], st['bot'], 0.4):
                    xs[round(v[0], 0)] += 1
        bars = sorted(x for x, c in xs.items() if c >= len(sts))
        dedup = []
        for x in bars:
            if not dedup or x - dedup[-1] > 3: dedup.append(x)
            else: dedup[-1] = x
        ps['bars'] = dedup
        ps['page'] = pno
        sy0 = sts[0]['top'] - 10 * sts[0]['sp']; sy1 = sts[-1]['bot'] + 10 * sts[-1]['sp']
        # assign glyphs to staves
        def staff_of(y):
            best, bd = None, 1e9
            for k, st in enumerate(sts):
                dist = 0 if st['top'] <= y <= st['bot'] else min(abs(y - st['top']), abs(y - st['bot']))
                if dist < bd: best, bd = k, dist
            return best, bd
        for st in sts: st['g'] = []
        def page_dist(y, st):
            return 0 if st['top'] <= y <= st['bot'] else min(abs(y - st['top']), abs(y - st['bot']))
        for g in glyphs:
            if not (sy0 <= g['y'] <= sy1): continue
            k, dist = staff_of(g['y'])
            if any(page_dist(g['y'], o) < dist for o in staves if not any(o is s2 for s2 in sts)):
                continue
            if g['c'] in DYN:  # dynamics sit above their staff
                cand = [kk for kk, st in enumerate(sts) if st['top'] >= g['y'] - 1]
                k = cand[0] if cand else k
            sts[k]['g'].append(g)
        ps['vlines'] = [v for v in vlines if sy0 <= v[1] <= sy1]
        ps['fills'] = [f for f in fills if sy0 <= f['y0'] <= sy1]
        ps['texts'] = [t for t in texts if sy0 - 30 <= t['y'] <= sy1]
        systems.append(ps)

print('systems', len(systems), [len(s['staves']) for s in systems])
print('parts', [[st['part'] for st in s['staves']] for s in systems][:3])

def note_debug(m, part):
    return False

def diat_to_midi(d, alter):
    return (d // 7 + 1) * 12 + SEMI[d % 7] + alter

state = {}  # per part: key (count, sign), clef, time sig
measure_no = 0
events = collections.defaultdict(list)  # part -> list of note dicts
dynamics = collections.defaultdict(list)
measures = []  # (measure_no, length_quarters)
problems = []
cur_time = 0.0
ts = (3, 4)
open_ties = {}
keys_out = {}  # part -> list of notes awaiting tie continuation across system

for si, ps in enumerate(systems):
    sts = ps['staves']
    sp = sts[0]['sp']
    bars = ps['bars']
    # measure boundaries: from system start to each barline
    nmeas = len(bars)
    x_start = sts[0]['x0']
    bounds = [x_start] + bars
    # --- per staff glyph classification
    per_staff = []
    for st in sts:
        part = st['part']
        g = sorted(st['g'], key=lambda q: q['x'])
        heads = [dict(q) for q in g if q['c'] in HEADS]
        for h in heads:
            h['w'] = h['x1'] - h['x']
        accs = [q for q in g if q['c'] in ACC]
        # key-signature chains: accidentals right after a clef or a barline
        anchors_x = [q['x1'] for q in g if q['c'] in CLEF_BOTTOM] + [b + 0.5 for b in bars]
        chain_ids = set()
        acc_sorted = sorted(accs, key=lambda a: a['x'])
        for ax0 in anchors_x:
            start = [a for a in acc_sorted if ax0 < a['x'] < ax0 + 2.2 * sp]
            if not start: continue
            chain = [start[0]]
            for a in acc_sorted:
                if a['x'] > chain[-1]['x'] and a['x'] - chain[-1]['x'] < 1.6 * sp:
                    chain.append(a)
            # a single accidental directly before a note at the same height is a note accidental, not a key
            if len(chain) == 1 and any(near(h['y'], chain[0]['y'], 0.3 * sp) and 0 < h['x'] - chain[0]['x'] < 2.5 * sp for h in heads):
                continue
            chain_ids.update(id(a) for a in chain)
        attached = set()
        for h in heads:
            for a in accs:
                if id(a) in chain_ids: continue
                if near(a['y'], h['y'], 0.3 * sp) and h['x'] - 4.5 * sp <= a['x'] < h['x'] - 0.3:
                    # nearest accidental to the left at same height
                    if 'acc' not in h or a['x'] > h['acc_x']:
                        h['acc'] = ACC[a['c']]; h['acc_x'] = a['x']
            if 'acc' in h:
                attached.add(id([a for a in accs if a['x'] == h['acc_x'] and near(a['y'], h['y'], 0.3 * sp)][0]))
        keysig = [a for a in accs if id(a) in chain_ids]
        stray = [a for a in accs if id(a) not in attached and id(a) not in chain_ids]
        for a in stray:
            problems.append(('stray_acc', si, part, a['c'], round(a['x']), round(a['y'])))
        st['heads'], st['keysig'] = heads, keysig
        st['rests'] = [q for q in g if q['c'] in RESTS]
        st['dots'] = [q for q in g if q['c'] == '.']
        st['flags'] = [q for q in g if q['c'] in 'jJ']
        st['clefs'] = [q for q in g if q['c'] in CLEF_BOTTOM]
        st['digits'] = [q for q in g if q['c'].isdigit()]
        st['dyn'] = [q for q in g if q['c'] in DYN]
    # stems
    stems = [v for v in ps['vlines'] if not any(near(v[0], b, 1.0) for b in bars) and v[2] - v[1] > 1.8 * sp]
    beams = [f for f in ps['fills'] if f['kind'] == 'lll']
    curves = [f for f in ps['fills'] if f['kind'] != 'lll']
    for st in sts:
        for h in st['heads']:
            h['dur'] = HEADS[h['c']]
            if h['c'] == 'w':
                h['colx'] = h['x']; continue
            best = None
            for v in stems:
                if v[1] - 1.5 <= h['y'] <= v[2] + 1.5:
                    if near(v[0], h['x'] + 0.2, 0.7):
                        best = (v, 'down')
                    elif near(v[0], h['x1'] - 0.2, 0.7):
                        best = best or (v, 'up')
            h['stem'] = best
            if best is None:
                problems.append(('nostem', si, st['part'], round(h['x']), round(h['y'])))
                h['colx'] = h['x']; continue
            v, _ = best
            # free end: the end farther from this head
            free_y = v[1] if abs(h['y'] - v[1]) > abs(h['y'] - v[2]) else v[2]
            direction = 'up' if free_y < h['y'] else 'down'
            h['dir'] = direction
            h['colx'] = v[0] - (h['w'] - 0.2) if direction == 'up' else v[0] - 0.2
            if h['c'] == 'œ':
                nb = 0
                for b in beams:
                    if b['x0'] - 0.5 <= v[0] <= b['x1'] + 0.5 and b['y1'] >= v[1] - 0.5 and b['y0'] <= v[2] + 0.5:
                        nb += 1
                fl = [f for f in st['flags'] if near(f['x'], v[0], 1.5) and near(f['y'], free_y, 3 * sp)]
                if nb or fl:
                    h['dur'] = 0.5 if (nb <= 1 and len(fl) <= 1) else 0.25
        for h in st['heads']:
            for d in st['dots']:
                if h['x1'] + 0.2 < d['x'] < h['x1'] + 2.5 * sp and abs(d['y'] - h['y']) <= 0.6 * sp:
                    h['dur'] *= 1.5
                    break
        for r in st['rests']:
            r['dur'] = RESTS[r['c']]
            r['colx'] = r['x']
            for d in st['dots']:
                if r['x1'] + 0.2 < d['x'] < r['x1'] + 2.5 * sp and abs(d['y'] - r['y']) <= 1.5 * sp:
                    r['dur'] *= 1.5 if r['dur'] else 1
                    break
    # --- measures
    for mi in range(nmeas):
        xa, xb = bounds[mi], bounds[mi + 1]
        measure_no += 1
        # time signature / key signature changes in this measure region (incl. courtesy at previous end)
        for st in sts:
            part = st['part']
            ps_state = state.setdefault(part, dict(key=0, clef='&'))
            for c in st['clefs']:
                if xa - 2 <= c['x'] < xb - 2: ps_state['clef'] = c['c']
            ks = [a for a in st['keysig'] if xa - 2 <= a['x'] < xb - 2]
            if ks:
                if True:
                    nf = sum(1 for a in ks if a['c'] == 'b'); ns = sum(1 for a in ks if a['c'] == '#')
                    ps_state['key'] = ns - nf
        votes = collections.Counter(state[st['part']]['key'] for st in sts)
        maj = votes.most_common(1)[0][0]
        for st in sts:
            state[st['part']]['key'] = maj
        for st in sts:
            keys_out.setdefault(st['part'], {})[measure_no] = state[st['part']]['key']
        digs = [d for st in sts[:1] for d in st['digits'] if xa - 2 <= d['x'] < xb - 2]
        if len(digs) >= 2:
            dd = sorted(digs, key=lambda d: d['y'])
            ts = (int(dd[0]['c']), int(dd[1]['c']))
        mlen = ts[0] * 4.0 / ts[1]
        # collect events per staff in this measure
        staff_ev = []
        for st in sts:
            evs = []
            for h in st['heads']:
                if xa <= h['colx'] + 0.5 < xb: evs.append(h)
            for r in st['rests']:
                if xa <= r['x'] < xb:
                    if r['dur'] is None: r['dur'] = mlen
                    evs.append(r)
            staff_ev.append(evs)
        # anchors from single-voice staves
        anchors = {xa: 0.0}
        def columns(evs):
            cols = []
            for e in sorted(evs, key=lambda e: e['colx']):
                if cols and abs(e['colx'] - cols[-1][0]) < 1.2:
                    cols[-1][1].append(e)
                else:
                    cols.append([e['colx'], [e]])
            return cols
        for evs in staff_ev:
            cols = columns(evs)
            if not cols: continue
            durs = [set(e['dur'] for e in c[1]) for c in cols]
            if all(len(d) == 1 for d in durs) and abs(sum(min(d) for d in durs) - mlen) < 1e-6:
                t = 0.0
                for c, d in zip(cols, durs):
                    anchors.setdefault(round(c[0], 1), t)
                    t += min(d)
        anchors[round(xb, 1)] = mlen
        ax = sorted(anchors.items())
        # per-staff exact times: single voice accumulation, else two-voice split by stem direction
        exact_t = {}
        for st, evs in zip(sts, staff_ev):
            cols = columns(evs)
            if not cols: continue
            durs = [set(e['dur'] for e in c[1]) for c in cols]
            if all(len(d) == 1 for d in durs) and abs(sum(min(d) for d in durs) - mlen) < 1e-6:
                t = 0.0
                for c, d in zip(cols, durs):
                    for e in c[1]: exact_t[id(e)] = t
                    t += min(d)
                continue
            mid = (st['top'] + st['bot']) / 2
            base1, base2, free = [], [], []
            for e in evs:
                if e['c'] in RESTS:
                    if e['y'] < mid - 1.2 * sp: base1.append(e)
                    elif e['y'] > mid + 1.2 * sp: base2.append(e)
                    else: base1.append(e); base2.append(e)
                elif e.get('dir') == 'up': base1.append(e)
                elif e.get('dir') == 'down': base2.append(e)
                else: free.append(e)
            for combo in itertools.product((0, 1, 2), repeat=len(free)):
              v1 = base1 + [e for e, c in zip(free, combo) if c in (0, 2)]
              v2 = base2 + [e for e, c in zip(free, combo) if c in (1, 2)]
              ok, tmp = True, {}
              for v in (v1, v2):
                vc = columns(v)
                vd = [set(e['dur'] for e in c[1]) for c in vc]
                if not all(len(d) == 1 for d in vd) or sum(min(d) for d in vd) > mlen + 1e-6:
                    ok = False; break
                t = 0.0
                for c, d in zip(vc, vd):
                    for e in c[1]:
                        if id(e) in tmp and abs(tmp[id(e)] - t) > 1e-6: ok = False
                        tmp[id(e)] = t
                    t += min(d)
              if ok:
                exact_t.update(tmp)
                problems.append(('2voice', measure_no, st['part']))
                break
        def time_at(x):
            best = min(ax, key=lambda a: abs(a[0] - x))
            if abs(best[0] - x) < 1.5:
                return best[1], True
            # linear interpolation between neighbours
            lo = [a for a in ax if a[0] <= x]; hi = [a for a in ax if a[0] > x]
            if lo and hi:
                (x0, t0), (x1, t1) = lo[-1], hi[0]
                t = t0 + (t1 - t0) * (x - x0) / (x1 - x0)
            else:
                t = lo[-1][1] if lo else 0.0
            return round(t * 2) / 2, False
        # build voices for verification and output
        for st, evs in zip(sts, staff_ev):
            part = st['part']
            pst = state[part]
            keyalt = {}
            k = pst['key']
            for i in range(abs(k)):
                keyalt[(SHARP_ORDER if k > 0 else FLAT_ORDER)[i]] = 1 if k > 0 else -1
            measure_acc = {}
            for e in sorted(evs, key=lambda e: e['colx']):
                if id(e) in exact_t:
                    t, exact = exact_t[id(e)], True
                else:
                    t, exact = time_at(e['colx'])
                if not exact:
                    problems.append(('interp', measure_no, part, round(t, 2), e['c'], round(e['colx'],1), round(e['y'],1), e['dur'], e.get('dir')))
                    for e2 in sorted(evs, key=lambda q: q['colx']):
                        problems.append(('  ctx', e2['c'], round(e2['colx'],1), round(e2['y'],1), e2['dur'], e2.get('dir'), exact_t.get(id(e2))))
                if e['c'] in RESTS:
                    continue
                step = round((st['bot'] - e['y']) / (sp / 2))
                d = CLEF_BOTTOM[pst['clef']] + step
                if 'acc' in e:
                    measure_acc[d] = e['acc']
                alter = measure_acc.get(d, keyalt.get(LETTERS[d % 7], 0))
                if note_debug(measure_no, part): print('DBG', measure_no, part, e['c'], round(e['x'],1), round(e['y'],1), 'bot', st['bot'], 'clef', pst['clef'], 'd', d)
                note = dict(m=measure_no, t=cur_time + t, dur=e['dur'], midi=diat_to_midi(d, alter),
                            x=e['x'], x1=e['x1'], y=e['y'], sys=si, staff_bot=st['bot'])
                events[part].append(note)
            for dy in st['dyn']:
                if xa <= dy['x'] < xb:
                    t, _ = time_at(dy['x'] + 4)
                    dynamics[part].append((cur_time + t, DYN[dy['c']]))
        # sanity: events must fit in measure and cover it
        for st, evs in zip(sts, staff_ev):
            cover = []
            for e in evs:
                t = exact_t[id(e)] if id(e) in exact_t else time_at(e['colx'])[0]
                if t + e['dur'] > mlen + 1e-6:
                    problems.append(('overflow', measure_no, st['part'], e['c'], t, e['dur']))
                cover.append((t, t + e['dur']))
            grid = [any(a <= k * 0.5 < b for a, b in cover) for k in range(int(mlen * 2))]
            if not all(grid):
                problems.append(('gap', measure_no, st['part'], [k * 0.5 for k, g in enumerate(grid) if not g]))
        # measure-sum check per staff (single voice estimate)
        measures.append((measure_no, cur_time, mlen, ts))
        cur_time += mlen
    # ties within this system and across systems
    for st in sts:
        part = st['part']
        notes = [n for n in events[part] if n['sys'] == si]
        for cv in curves:
            if cv['y1'] - cv['y0'] > 3 * sp: continue
            # starting note: head whose right part is near curve left end
            starts = [n for n in notes if n['x'] - 1 <= cv['x0'] <= n['x1'] + 2 * sp and abs(n['y'] - (cv['y0'] + cv['y1']) / 2) < 3 * sp]
            for n in starts:
                ends = [m for m in notes if m['midi'] == n['midi'] and abs(m['t'] - (n['t'] + n['dur'])) < 1e-6
                        and m['x'] - 2 * sp <= cv['x1'] <= m['x1'] + 1]
                if ends:
                    ends[0]['tied_from'] = True
                elif cv['x1'] > bounds[-1] - 6 and abs(n['t'] + n['dur'] - cur_time) < 1e-6:
                    n['tie_out'] = True
        # incoming cross-system ties
        for n in notes:
            prev = [p for p in events[part] if p.get('tie_out') and p['sys'] < si and p['midi'] == n['midi'] and abs(p['t'] + p['dur'] - n['t']) < 1e-6]
            if prev:
                n['tied_from'] = True

# merge ties
for part, ns in events.items():
    ns.sort(key=lambda n: (n['t'], n['midi']))
    out = []
    for n in ns:
        if n.get('tied_from'):
            prevs = [p for p in out if p['midi'] == n['midi'] and abs(p['t'] + p['dur'] - n['t']) < 1e-6]
            if prevs:
                prevs[-1]['dur'] += n['dur']
                continue
        out.append(n)
    events[part] = out

# verify: per part per measure, total covered time by note/rest union not exceeding
print('measures', measure_no, 'total quarters', cur_time)
cnt = collections.Counter(p[0] for p in problems)
print('problems', cnt)
for p in problems:
    if p[0] not in ('2voice',): print(p)
json.dump(dict(measures=measures, events={k: [dict(t=n['t'], dur=n['dur'], midi=n['midi'], m=n['m']) for n in v] for k, v in events.items()},
               dynamics=dynamics, keys=keys_out), open(OUT, 'w'), ensure_ascii=False, indent=0)
for part, v in events.items():
    print(part, len(v), min(n['midi'] for n in v), max(n['midi'] for n in v))
