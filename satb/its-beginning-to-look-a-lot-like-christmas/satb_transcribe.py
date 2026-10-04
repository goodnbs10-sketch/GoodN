import itertools, numpy as np, librosa
from basic_pitch.inference import predict
import music21 as m21
import pretty_midi

AUDIO = 'audio.wav'
y, sr = librosa.load(AUDIO, sr=22050)
_, beats = librosa.beat.beat_track(y=y, sr=sr, units='time')
beats = np.asarray(beats)
bp = np.median(np.diff(beats))
# extend beat grid to cover the whole clip
pre = np.arange(beats[0] - bp, -bp, -bp)[::-1]
post = np.arange(beats[-1] + bp, len(y) / sr + bp, bp)
grid_beats = np.concatenate([pre, beats, post])
def t2b(t):  # seconds -> beat position
    return np.interp(t, grid_beats, np.arange(len(grid_beats)))

_, _, notes = predict(AUDIO, onset_threshold=0.5, frame_threshold=0.3,
                      minimum_note_length=100, minimum_frequency=60, maximum_frequency=1100)

SUB = 2  # 8th-note slots
nslots = int(np.ceil(t2b(len(y) / sr) * SUB)) + 1
act = [dict() for _ in range(nslots)]      # pitch -> weight
ons = [set() for _ in range(nslots)]
for s, e, p, a, _ in notes:
    bs, be = t2b(s) * SUB, t2b(e) * SUB
    k0 = int(round(bs))
    ons[min(k0, nslots - 1)].add(p)
    for k in range(int(np.floor(bs)), int(np.ceil(be))):
        ov = min(be, k + 1) - max(bs, k)
        if ov >= 0.5 and 0 <= k < nslots:
            act[k][p] = act[k].get(p, 0) + a * ov

VOICES = ['Soprano', 'Alto', 'Tenor', 'Bass']
RANGE = {'Soprano': (60, 81), 'Alto': (55, 74), 'Tenor': (48, 69), 'Bass': (38, 62)}
CENTER = {'Soprano': 69, 'Alto': 63, 'Tenor': 58, 'Bass': 48}

def cost(v, p, prev):
    lo, hi = RANGE[v]
    c = 3.0 * max(0, lo - p, p - hi) + 0.05 * abs(p - CENTER[v])
    if prev is not None:
        c += 0.25 * min(abs(p - prev), 12)
    return c

prev = {v: None for v in VOICES}
assign = []  # per slot: voice -> pitch or None
for k in range(nslots):
    cand = sorted(act[k].items(), key=lambda x: -x[1])[:7]
    pitches = sorted(p for p, w in cand if w > 0.15)
    best, bestc = None, 1e9
    # choose an ordered (non-crossing) subset of pitches for S>=A>=T>=B
    for r in range(0, min(4, len(pitches)) + 1):
        for sub in itertools.combinations(pitches[::-1], r):  # descending
            for vs in itertools.combinations(VOICES, r):
                c = sum(cost(v, p, prev[v]) for v, p in zip(vs, sub))
                c += 4.0 * (4 - r)  # penalty for silent voices
                c += 1.0 * (len(pitches) - r)  # dropped notes
                if c < bestc:
                    bestc, best = c, dict(zip(vs, sub))
    a = {v: best.get(v) for v in VOICES}
    for v in VOICES:
        if a[v] is not None:
            prev[v] = a[v]
    assign.append(a)

# smooth: fill single-slot gaps, drop single-slot blips
for v in VOICES:
    seq = [a[v] for a in assign]
    for k in range(1, nslots - 1):
        if seq[k] is None and seq[k-1] is not None and seq[k-1] == seq[k+1]:
            seq[k] = seq[k-1]
    for k in range(1, nslots - 1):
        if seq[k] is not None and seq[k-1] != seq[k] and seq[k+1] != seq[k] and seq[k-1] is not None and seq[k-1] == seq[k+1]:
            seq[k] = seq[k-1]
    for k, a in enumerate(assign):
        a[v] = seq[k]

# build events per voice: (start_slot, len_slots, pitch)
events = {}
for v in VOICES:
    ev, k = [], 0
    while k < nslots:
        p = assign[k][v]
        j = k + 1
        while j < nslots and assign[j][v] == p and (p is None or p not in ons[j]):
            j += 1
        ev.append((k, j - k, p))
        k = j
    events[v] = ev

tempo_bpm = 60.0 / bp
print('tempo', round(tempo_bpm, 1), 'slots', nslots)

# --- MusicXML score
score = m21.stream.Score()
score.metadata = m21.metadata.Metadata(movementName='', title="It's Beginning to Look a Lot Like Christmas (SATB 자동채보 초안)",
                                       composer='Meredith Willson / 편곡: VOICEWING (자동 채보)')
clefs = {'Soprano': m21.clef.TrebleClef(), 'Alto': m21.clef.TrebleClef(),
         'Tenor': m21.clef.Treble8vbClef(), 'Bass': m21.clef.BassClef()}
for v in VOICES:
    part = m21.stream.Part(id=v)
    part.partName = v
    inst = m21.instrument.Vocalist(); inst.partName = v
    part.insert(0, inst)
    part.insert(0, clefs[v])
    part.insert(0, m21.key.Key('B'))
    part.insert(0, m21.meter.TimeSignature('4/4'))
    if v == 'Soprano':
        part.insert(0, m21.tempo.MetronomeMark(number=round(tempo_bpm)))
    for s, l, p in events[v]:
        ql = l / SUB
        n = m21.note.Rest(quarterLength=ql) if p is None else m21.note.Note(pretty_midi.note_number_to_name(int(p)), quarterLength=ql)
        part.insert(s / SUB, n)
    part.makeMeasures(inPlace=True)
    part.makeTies(inPlace=True)
    score.insert(0, part)
score.write('musicxml', fp='out/SATB_score.musicxml')

# --- MIDI per part and full
def make_midi(gains, path, program=52):
    pm = pretty_midi.PrettyMIDI(initial_tempo=tempo_bpm)
    spb = 60.0 / tempo_bpm
    for v in VOICES:
        inst = pretty_midi.Instrument(program=program, name=v)
        for s, l, p in events[v]:
            if p is None or gains[v] == 0:
                continue
            inst.notes.append(pretty_midi.Note(velocity=int(100 * gains[v]), pitch=int(p),
                                               start=s / SUB * spb, end=(s + l) / SUB * spb - 0.02))
        pm.instruments.append(inst)
    pm.write(path)

make_midi({v: 1 for v in VOICES}, 'out/SATB_all.mid')
for v in VOICES:
    make_midi({w: (1 if w == v else 0) for w in VOICES}, f'out/part_{v}.mid')
    make_midi({w: (1 if w == v else 0.35) for w in VOICES}, f'out/practice_{v}.mid')
for v in VOICES:
    ps = [p for _, _, p in events[v] if p is not None]
    print(v, 'notes', len(ps), 'range', pretty_midi.note_number_to_name(min(ps)), '-', pretty_midi.note_number_to_name(max(ps)))
