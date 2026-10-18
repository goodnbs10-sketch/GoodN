"""Render parsed score events to MIDI (per part / all / practice mixes) and MusicXML."""
import json, sys, bisect
import pretty_midi
import music21 as m21

SRC, OUTDIR = sys.argv[1], sys.argv[2]
j = json.load(open(SRC))
measures = j['measures']  # [m, start_q, len_q, ts]
events = j['events']
dyn = j['dynamics']
PARTS = ['Descant', 'Soprano', 'Alto', 'Tenor', 'Bass']
KO = {'Descant': '데스캔트', 'Soprano': '소프라노', 'Alto': '알토', 'Tenor': '테너', 'Bass': '베이스'}
mstart = {m[0]: m[1] for m in measures}
mlen = {m[0]: m[2] for m in measures}
total_q = measures[-1][1] + measures[-1][2]

# ---- tempo map (quarter-note BPM as function of score position)
def bpm_at(q):
    def m_of(q):
        i = bisect.bisect_right([m[1] for m in measures], q) - 1
        return measures[i][0]
    m = m_of(q)
    if m < 60: return 72.0
    if m < 62:  # accel. 72 -> 116 across m60-61
        a, b = mstart[60], mstart[62]
        return 72 + (116 - 72) * (q - a) / (b - a)
    if m < 119: return 116.0
    if m < 121:  # molto rall. 116 -> 60
        a, b = mstart[119], mstart[121]
        return 116 + (60 - 116) * (q - a) / (b - a)
    return 88.0

STEP = 1 / 48
qgrid, tgrid = [0.0], [0.0]
q, t = 0.0, 0.0
while q < total_q + 8:
    t += STEP * 60.0 / bpm_at(q)
    q += STEP
    if abs(q - mstart[121]) < STEP / 2:
        t += 0.6  # breath mark before Maestoso
    qgrid.append(q); tgrid.append(t)

def sec(q):
    i = min(int(round(q / STEP)), len(tgrid) - 1)
    return tgrid[i]

def vel_at(part, q):
    v = 70
    for tq, vv in sorted(dyn.get(part, [])):
        if tq <= q + 1e-6: v = vv
    return v

def build_midi(gains, path, program=0):
    pm = pretty_midi.PrettyMIDI(initial_tempo=72)
    for part in PARTS:
        g = gains.get(part, 0)
        if g == 0: continue
        # layer notes so identical pitches never overlap within one track
        layers = []
        for n in sorted(events.get(part, []), key=lambda n: (n['t'], n['midi'])):
            for L in layers:
                if all(not (o['midi'] == n['midi'] and o['t'] < n['t'] + n['dur'] and n['t'] < o['t'] + o['dur']) for o in L):
                    L.append(n); break
            else:
                layers.append([n])
        for li, L in enumerate(layers):
            inst = pretty_midi.Instrument(program=program, name=f'{part}' + (f' {li+1}' if li else ''))
            for n in L:
                s, e = sec(n['t']), sec(n['t'] + n['dur'])
                inst.notes.append(pretty_midi.Note(velocity=max(1, min(127, int(vel_at(part, n['t']) * g))),
                                                   pitch=n['midi'], start=s, end=max(s + 0.05, e - 0.03)))
            pm.instruments.append(inst)
    pm.write(path)

build_midi({p: 1.0 for p in PARTS}, f'{OUTDIR}/all.mid')
for p in PARTS:
    build_midi({p: 1.0}, f'{OUTDIR}/solo_{p}.mid')
    build_midi({q: (1.15 if q == p else 0.38) for q in PARTS}, f'{OUTDIR}/practice_{p}.mid')
print('duration sec', round(sec(total_q), 1))

# ---- MusicXML (for checking / editing in MuseScore)
score = m21.stream.Score()
score.metadata = m21.metadata.Metadata(title='아카펠라 성탄 찬양 메들리', composer='Hanna Cho 편곡 (PDF에서 자동 변환)')
score.metadata.movementName = ''
keys = j.get('keys', {})
for part in PARTS:
    evs = events.get(part, [])
    P = m21.stream.Part(id=part); P.partName = KO[part]
    first_clef = m21.clef.BassClef() if part == 'Bass' else (m21.clef.Treble8vbClef() if part == 'Tenor' else m21.clef.TrebleClef())
    prev_ts, prev_key = None, None
    for mno, st, ln, ts in measures:
        M = m21.stream.Measure(number=mno)
        if mno == 1:
            M.insert(0, first_clef)
        if tuple(ts) != prev_ts:
            M.insert(0, m21.meter.TimeSignature(f'{ts[0]}/{ts[1]}')); prev_ts = tuple(ts)
        k = keys.get(part, {}).get(str(mno), keys.get('Soprano', {}).get(str(mno)))
        if k is not None and k != prev_key:
            M.insert(0, m21.key.KeySignature(k)); prev_key = k
        # notes overlapping this measure, split at barlines
        segs = []
        for n in evs:
            a, b = max(n['t'], st), min(n['t'] + n['dur'], st + ln)
            if a < b - 1e-6:
                segs.append((round(a - st, 4), round(b - a, 4), n['midi'], a > n['t'] + 1e-6, b < n['t'] + n['dur'] - 1e-6))
        # group into chords, then pack into non-overlapping voices
        chords = {}
        for a, d, p, tin, tout in segs:
            chords.setdefault((a, d), []).append((p, tin, tout))
        voices = []
        for (a, d) in sorted(chords, key=lambda x: (x[0], -x[1])):
            for V in voices:
                if V[-1][0] + V[-1][1] <= a + 1e-6:
                    V.append((a, d, chords[(a, d)])); break
            else:
                voices.append([(a, d, chords[(a, d)])])
        if not voices:
            M.append(m21.note.Rest(quarterLength=ln))
        target = M
        for vi, V in enumerate(voices[:2] if len(voices) > 1 else voices):
            container = m21.stream.Voice(id=str(vi + 1)) if len(voices) > 1 else M
            pos = 0.0
            for a, d, notes in V:
                if a > pos + 1e-6:
                    r = m21.note.Rest(quarterLength=a - pos); r.style.hideObjectOnPrint = vi > 0
                    container.insert(pos, r)
                if len(notes) == 1:
                    el = m21.note.Note(notes[0][0], quarterLength=d)
                    tin, tout = notes[0][1], notes[0][2]
                else:
                    el = m21.chord.Chord([x[0] for x in notes], quarterLength=d)
                    tin, tout = notes[0][1], notes[0][2]
                if tin and tout: el.tie = m21.tie.Tie('continue')
                elif tin: el.tie = m21.tie.Tie('stop')
                elif tout: el.tie = m21.tie.Tie('start')
                container.insert(a, el)
                pos = a + d
            if pos < ln - 1e-6:
                r = m21.note.Rest(quarterLength=ln - pos); r.style.hideObjectOnPrint = vi > 0
                container.insert(pos, r)
            if container is not M:
                M.insert(0, container)
        if len(voices) > 2:
            # extra overlapping material: merge into voice 2 as chords where possible (rare)
            print('more than 2 voices', part, mno, len(voices))
        P.append(M)
    score.insert(0, P)
score.write('musicxml', fp=f'{OUTDIR}/score.musicxml')
print('musicxml ok')
