"""The Culling: create full-length stem arrangements using original audio only.

Every source sample keeps its original timeline position. Audio is streamed in
bounded chunks; edits are documented gain masks, without deletion or stretching.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import uuid

import numpy as np
import soundfile as sf


STEMS = ('instruments', 'kick', '808', 'drums')
FILENAMES = dict(zip(STEMS, ('Instruments.wav', 'Kick.wav', '808.wav', 'Drums.wav')))
INTENSITIES = ('Subtle', 'Balanced', 'Bold')
CHUNK = 65536


class CullingError(ValueError):
    """An actionable input or export error suitable for displaying in the app."""


def _notify(progress, message):
    if progress is not None:
        progress(message)


def _tempo(value):
    try:
        tempo = float(value)
    except (TypeError, ValueError):
        raise CullingError('Enter the actual song tempo between 20 and 400 BPM.') from None
    if isinstance(value, bool) or not math.isfinite(tempo) or not 20 <= tempo <= 400:
        raise CullingError('Enter the actual song tempo between 20 and 400 BPM.')
    return tempo


def inspect_stems(paths: dict[str, str], bpm: float) -> dict:
    """Validate four mono/stereo WAV/FLAC files exported from the same start."""
    tempo = _tempo(bpm)
    if not isinstance(paths, dict) or any(not paths.get(key) for key in STEMS):
        raise CullingError('Choose all four stems: instruments, kick, 808, and drums.')
    result = {}
    for key in STEMS:
        path = Path(paths[key]).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in ('.wav', '.flac'):
            raise CullingError(f'{key}: choose an existing WAV or FLAC file.')
        try:
            info = sf.info(str(path))
        except (OSError, RuntimeError) as error:
            raise CullingError(f'Cannot read {key}: {error}') from error
        if info.channels not in (1, 2):
            raise CullingError(f'{key} has {info.channels} channels; use a mono or stereo stem.')
        if info.frames <= 0 or info.samplerate <= 0:
            raise CullingError(f'{key} contains no readable audio.')
        result[key] = {'path': str(path), 'frames': info.frames,
                       'sample_rate': info.samplerate, 'channels': info.channels,
                       'subtype': info.subtype, 'format': info.format}
    if len({item['path'].casefold() for item in result.values()}) != len(STEMS):
        raise CullingError('Choose four different stem files.')
    rates = {item['sample_rate'] for item in result.values()}
    if len(rates) != 1:
        raise CullingError('All stems must use the same sample rate. Export them together without resampling.')
    lengths = [item['frames'] for item in result.values()]
    if max(lengths) - min(lengths) > 1:
        raise CullingError('The stems have different lengths. Export all four from bar 1 to the same end, including silence.')
    rate, frames = rates.pop(), max(lengths)
    frames_per_bar = rate * 240.0 / tempo
    estimate = frames / frames_per_bar
    nearest = round(estimate)
    full_bars = nearest if abs(frames - round(nearest * frames_per_bar)) <= 1 else math.floor(estimate)
    if full_bars < 1:
        raise CullingError('The stems must contain at least one complete 4/4 bar at the supplied BPM.')
    tail_frames = max(0, frames - round(full_bars * frames_per_bar))
    return {'bpm': tempo, 'sample_rate': rate, 'frames': frames,
            'seconds': frames / rate, 'full_bars': full_bars,
            'tail_frames': tail_frames, 'frames_per_bar': frames_per_bar,
            'stems': result,
            'alignment_assumption': 'All four stems start at song bar 1 in 4/4; equal lengths do not prove alignment.'}


def _frame(bar, info):
    return min(info['frames'], max(0, round(bar * info['frames_per_bar'])))


def _read(audio, start, count):
    """Pad the permitted one-frame export-rounding difference with silence."""
    audio.seek(min(start, len(audio)))
    data = audio.read(min(count, max(0, len(audio) - start)), dtype='float64', always_2d=True)
    if len(data) < count:
        data = np.pad(data, ((0, count - len(data)), (0, 0)))
    return data


def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _analyse(info, progress):
    """Per-bar RMS describes activity and repeated phrase contours, not notes."""
    energies = {}
    for key in STEMS:
        _notify(progress, f'Listening for activity in {key}…')
        rows = []
        with sf.SoundFile(info['stems'][key]['path']) as audio:
            for bar in range(info['full_bars']):
                start, end = _frame(bar, info), _frame(bar + 1, info)
                square_sum = 0.0
                for cursor in range(start, end, CHUNK):
                    data = _read(audio, cursor, min(CHUNK, end - cursor))
                    if not np.isfinite(data).all():
                        raise CullingError(f'{key} contains invalid audio samples. Re-export that stem.')
                    square_sum += float(np.square(data).sum())
                rows.append(math.sqrt(square_sum / max(1, (end - start) * audio.channels)))
                if bar and bar % 64 == 0:
                    _notify(progress, f'Analysing {key}: bar {bar + 1} of {info["full_bars"]}…')
        energies[key] = rows
        info['stems'][key]['sha256'] = _hash_file(info['stems'][key]['path'])
    return energies


def _choice(seed, *parts):
    payload = '|'.join(map(str, (seed, *parts))).encode('utf-8')
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], 'big')


def _phrases(info, energies):
    phrases = []
    for start in range(0, info['full_bars'], 4):
        end = min(start + 4, info['full_bars'])
        phrases.append({'index': len(phrases), 'start_bar': start, 'end_bar': end,
                        'source_start': _frame(start, info), 'source_end': _frame(end, info)})
    # Preserve the complete exported ending, including any partial bar or tail.
    phrases[-1]['source_end'] = info['frames']
    return phrases


def _cut_map(phrases, selected):
    cursor, result = 0, []
    for index in selected:
        phrase = phrases[index]
        frames = phrase['source_end'] - phrase['source_start']
        result.append({'phrase_index': index,
                       'source_start_bar': phrase['start_bar'] + 1,
                       'source_end_bar_exclusive': phrase['end_bar'] + 1,
                       'source_start_frame': phrase['source_start'],
                       'source_end_frame': phrase['source_end'],
                       'output_start_frame': cursor, 'output_end_frame': cursor + frames})
        cursor += frames
    return result


def _runs(mask):
    """Half-open ranges of true values in a short planning mask."""
    changes = np.diff(np.r_[False, mask, False].astype(np.int8))
    return list(zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)))


def _beat_activity(info, handles, progress):
    """Measure whole beats so ordinary spaces between drum hits remain usable.

    An active beat needs RMS above -60 dB relative to that stem's 95th percentile
    active-beat RMS, with a 1e-6 absolute floor. This is activity, not note or
    section recognition. The partial ending is measured but never edited.
    """
    bounds = [_frame(beat / 4, info) for beat in range(info['full_bars'] * 4 + 1)]
    if bounds[-1] < info['frames']:
        bounds.append(info['frames'])
    bounds = np.asarray(bounds, dtype=np.int64)
    rms = np.zeros((len(STEMS), len(bounds) - 1))
    thresholds = {}
    for row, key in enumerate(STEMS):
        _notify(progress, f'Finding beat-length space for {key}…')
        for beat, (start, end) in enumerate(zip(bounds[:-1], bounds[1:])):
            square_sum = 0.0
            for cursor in range(int(start), int(end), CHUNK):
                data = _read(handles[key], cursor, min(CHUNK, int(end) - cursor))
                if not np.isfinite(data).all():
                    raise CullingError(f'{key} contains invalid audio samples. Re-export that stem.')
                square_sum += float(np.square(data).sum())
            rms[row, beat] = math.sqrt(square_sum / max(1, (end - start) * handles[key].channels))
        positive = rms[row][rms[row] > 1e-6]
        thresholds[key] = max(1e-6, float(np.percentile(positive, 95)) * .001) if len(positive) else 1e-6
    active = rms > np.asarray([thresholds[key] for key in STEMS])[:, None]
    return bounds, active, thresholds


def _movement(info, cuts, intensity, seed, handles, keep_percent=70, progress=None):
    """Choose audible, separated dropouts on an unchanged source timeline.

    Safety is evaluated against all already-selected mutes. For every beat that
    originally has active audio, at least one original active stem is retained.
    Silence between that backing's rhythmic hits is normal and is not stretched
    or filled. This is a beat-level arrangement safeguard, not sample synthesis.
    """
    bounds, active, thresholds = _beat_activity(info, handles, progress)
    lengths = np.diff(bounds)
    muted = np.zeros_like(active)
    full_beats = info['full_bars'] * 4
    protected = np.zeros(len(lengths), dtype=bool)
    protected[:1] = True
    protected[max(0, full_beats - 4):] = True
    fade_frames = max(1, round(info['sample_rate'] * .003))
    opening_end = int(bounds[min(1, len(bounds) - 1)])
    groups = ((0,), (1, 2), (3,))
    names = ('Instrument dropout and return', 'Kick and 808 dropout and return', 'Drum dropout and return')
    source_time = float((active * lengths).sum())
    group_time = [float((active[list(group)] * lengths).sum()) for group in groups]
    target_fraction = 1 - keep_percent / 100
    desired_removed = source_time * target_fraction
    removed = 0.0
    removed_by_group = [0.0, 0.0, 0.0]
    phrase_use = np.zeros(max(1, math.ceil(full_beats / 16)), dtype=np.int32)
    durations, step, minimum = {
        'Subtle': ((1, 2), 2, 1),
        'Balanced': ((4, 8), 4, 2),
        'Bold': ((8, 16), 4, 4),
    }[intensity]
    candidates = []
    if keep_percent < 100 and source_time:
        for group_index, group in enumerate(groups):
            if not group_time[group_index]:
                continue
            for start in range(0, full_beats, step):
                for duration in durations:
                    end = min(full_beats, start + duration)
                    if end - start >= minimum:
                        candidates.append((group_index, start, end,
                                           _choice(seed, 'dropout', group_index, start, duration)))
    while candidates and removed < desired_removed:
        best = None
        best_key = None
        for candidate_index, (group_index, start, end, tie) in enumerate(candidates):
            group = groups[group_index]
            # Leave a complete beat between a group's dropouts, so returns are
            # real and adjacent windows cannot silently become a whole-song mute.
            if muted[list(group), max(0, start - 1):min(len(lengths), end + 1)].any():
                continue
            others = [row for row in range(len(STEMS)) if row not in group]
            target_active = active[list(group), start:end].any(axis=0)
            backing = (active[others, start:end] & ~muted[others, start:end]).any(axis=0)
            safe = ~(target_active & ~backing) & ~protected[start:end]
            eligible = np.zeros(end - start, dtype=bool)
            for left, right in _runs(safe):
                # Fade before the musical boundary so its removed attack does
                # not leak through. Never borrow that fade from the opening.
                if right - left >= minimum and int(bounds[start + left]) - fade_frames >= opening_end:
                    eligible[left:right] = True
            gain = float((active[list(group), start:end] * eligible * lengths[start:end]).sum())
            improvement = abs(desired_removed - removed) - abs(desired_removed - removed - gain)
            if gain <= 0 or improvement <= 0:
                continue
            first_phrase, last_phrase = start // 16, (end - 1) // 16
            key = (round(target_fraction - removed_by_group[group_index] / group_time[group_index], 8),
                   -int(phrase_use[first_phrase:last_phrase + 1].sum()), improvement, tie)
            if best_key is None or key > best_key:
                best_key = key
                best = (candidate_index, group_index, start, end, eligible, gain)
        if best is None:
            break
        candidate_index, group_index, start, end, eligible, gain = best
        for row in groups[group_index]:
            muted[row, start:end] |= eligible
        removed += gain
        removed_by_group[group_index] += gain
        phrase_use[start // 16:(end - 1) // 16 + 1] += 1
        candidates.pop(candidate_index)
    if np.any(active.any(axis=0) & ~(active & ~muted).any(axis=0)):
        raise CullingError('The arrangement could not preserve active backing. No completed export was created.')
    moves = []
    for group_index, group in enumerate(groups):
        for start, end in _runs(muted[group[0]]):
            mute_frame, end_frame = int(bounds[start]), int(bounds[end])
            begin_frame = mute_frame - fade_frames
            moves.append({'name': names[group_index], 'stems': [STEMS[row] for row in group],
                          'gain': 0.0, 'source_start_frame': begin_frame, 'source_end_frame': end_frame,
                          'output_start_frame': begin_frame, 'output_end_frame': end_frame,
                          'mute_start_frame': mute_frame,
                          'duration_bars': (end_frame - mute_frame) / info['frames_per_bar']})
    moves.sort(key=lambda move: (move['source_start_frame'], move['name']))
    removed = float((active * muted * lengths).sum())
    actual_keep = 100 * (1 - removed / source_time) if source_time else 100.0
    per_stem = {}
    for row, key in enumerate(STEMS):
        total = float((active[row] * lengths).sum())
        removed_stem = float((active[row] * muted[row] * lengths).sum())
        per_stem[key] = {'source_active_seconds': total / info['sample_rate'],
                         'retained_active_seconds': (total - removed_stem) / info['sample_rate'],
                         'actual_keep_percent': 100 * (1 - removed_stem / total) if total else 100.0}
    metric = {
        'method': 'Beat-window active stem-time from mute_start_frame to source_end_frame; the 3 ms pre-fade is excluded and the short return ramp is not subtracted.',
        'requested_keep_percent': keep_percent, 'actual_keep_percent': actual_keep,
        'source_active_stem_seconds': source_time / info['sample_rate'],
        'retained_active_stem_seconds': (source_time - removed) / info['sample_rate'],
        'removed_active_stem_seconds': removed / info['sample_rate'],
        'per_stem': per_stem, 'activity_rms_thresholds': thresholds,
        'backing_safety': 'At least one originally active stem stays active in each active beat, accounting for every selected dropout.',
        'protected_opening_end_frame': int(bounds[min(1, len(bounds) - 1)]),
        'protected_ending_start_frame': int(bounds[max(0, full_beats - 4)]),
    }
    notes = []
    if keep_percent == 100:
        notes.append('100% parts retained: the stem samples are unchanged, apart from any permitted one-frame padding.')
    elif not source_time:
        notes.append('No activity above the analysis floor was found; the original full-length stems were preserved.')
    elif not moves:
        notes.append('The source is too short or sparse for supported dropouts. The opening, ending and only active backing were preserved.')
    elif actual_keep > keep_percent + 5:
        notes.append('Safety, phrase lengths or sparse backing limited the requested reduction; more active part-time was retained to protect the arrangement.')
    return moves, metric, notes


def _gain(start, count, key, moves, edges, rate):
    gain = np.ones(count)
    positions = np.arange(start, start + count)
    fade = max(1, round(rate * .003))
    for move in moves:
        if key not in move['stems']:
            continue
        begin, end = move['output_start_frame'], move['output_end_frame']
        if end <= start or begin >= start + count:
            continue
        width = min(fade, max(1, (end - begin) // 2))
        inside = (positions >= begin) & (positions < end)
        mute_start = move.get('mute_start_frame', begin + width)
        fade_down = np.clip((positions[inside] - begin) / max(1, mute_start - begin), 0, 1)
        fade_up = np.clip((end - 1 - positions[inside]) / width, 0, 1)
        weight = np.minimum(fade_down, fade_up)
        gain[inside] = np.minimum(gain[inside], 1 - (1 - move['gain']) * weight)
    for edge in edges:
        begin, end = edge['start'], edge['end']
        inside = (positions >= begin) & (positions < end)
        if not inside.any():
            continue
        weight = (positions[inside] - begin) / max(1, end - begin - 1)
        if edge['direction'] == 'out':
            weight = 1 - weight
        gain[inside] = np.minimum(gain[inside], weight)
    return gain


def _write_json(path, data):
    with open(path, 'x', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write('\n')


def process_stems(paths: dict[str, str], bpm: float, keep_percent: int = 70,
                  intensity: str = 'Balanced', seed: int = 2026,
                  output_root: str | Path | None = None, progress=None) -> Path:
    """Export full-length aligned stems with musical dropouts and an edit map.

    progress, if supplied, receives one human-readable status string per call.
    keep_percent targets retained active part-time, never song duration. An
    explicit output root is required. Input files are never modified.
    """
    info = inspect_stems(paths, bpm)
    if type(keep_percent) is not int or not 40 <= keep_percent <= 100:
        raise CullingError('Parts to keep must be a whole number from 40 to 100.')
    intensity = str(intensity).strip().title()
    if intensity not in INTENSITIES:
        raise CullingError('Choose Subtle, Balanced, or Bold movement.')
    if type(seed) is not int:
        raise CullingError('Use a whole number for the variation seed.')
    if output_root is None or not str(output_root).strip():
        raise CullingError('Choose an output folder before exporting.')
    root = Path(output_root).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise CullingError('Choose a folder for the output.')
    energies = _analyse(info, progress)
    phrases = _phrases(info, energies)
    cuts = _cut_map(phrases, list(range(len(phrases))))
    if any(cut['source_start_frame'] != cut['output_start_frame']
           or cut['source_end_frame'] != cut['output_end_frame'] for cut in cuts):
        raise CullingError('The source timeline could not be preserved. No export was created.')
    root.mkdir(parents=True, exist_ok=True)
    target = root / f'The Culling {datetime.now():%Y-%m-%d %H-%M-%S} {uuid.uuid4().hex[:8]}'
    target.mkdir(exist_ok=False)
    marker = target / 'INCOMPLETE.txt'
    marker.write_text('Export in progress. These files are not ready to use.\n', encoding='utf-8')
    try:
        with ExitStack() as stack:
            handles = {key: stack.enter_context(sf.SoundFile(info['stems'][key]['path'])) for key in STEMS}
            _notify(progress, 'Planning full-length dropouts and returns…')
            moves, part_time, notes = _movement(info, cuts, intensity, seed, handles, keep_percent, progress)
            edges = {key: [] for key in STEMS}
            subtypes = {key: info['stems'][key]['subtype'] if sf.check_format('WAV', info['stems'][key]['subtype'])
                        else 'FLOAT' for key in STEMS}
            writers = {key: stack.enter_context(sf.SoundFile(str(target / FILENAMES[key]), mode='w',
                       samplerate=info['sample_rate'], channels=info['stems'][key]['channels'],
                       subtype=subtypes[key], format='WAV')) for key in STEMS}
            preview_channels = max(item['channels'] for item in info['stems'].values())
            temporary_preview = target / 'Preview.unscaled.wav'
            mix_writer = stack.enter_context(sf.SoundFile(str(temporary_preview), mode='w',
                         samplerate=info['sample_rate'], channels=preview_channels, subtype='FLOAT', format='WAV'))
            peak = 0.0
            for index, cut in enumerate(cuts):
                _notify(progress, f'Writing phrase {index + 1} of {len(cuts)}…')
                begin, end = cut['source_start_frame'], cut['source_end_frame']
                for cursor in range(begin, end, CHUNK):
                    count = min(CHUNK, end - cursor)
                    output_start = cut['output_start_frame'] + cursor - begin
                    mix = np.zeros((count, preview_channels))
                    for key in STEMS:
                        data = _read(handles[key], cursor, count)
                        if not np.isfinite(data).all():
                            raise CullingError(f'{key} contains invalid audio samples. Re-export that stem.')
                        data *= _gain(output_start, count, key, moves, edges[key], info['sample_rate'])[:, None]
                        writers[key].write(data)
                        mix += data if data.shape[1] == preview_channels else np.repeat(data, preview_channels, axis=1)
                    peak = max(peak, float(np.max(np.abs(mix))))
                    mix_writer.write(mix)
        _notify(progress, 'Balancing the listening preview…')
        preview_gain = .95 / peak if peak > 0 else 1.0
        with sf.SoundFile(str(temporary_preview)) as reader, sf.SoundFile(
                str(target / 'Preview.wav'), mode='w', samplerate=info['sample_rate'],
                channels=preview_channels, subtype='PCM_24', format='WAV') as writer:
            while len(data := reader.read(CHUNK, dtype='float64', always_2d=True)):
                writer.write(data * preview_gain)
        temporary_preview.unlink()
        output_frames = info['frames']
        # Verify the files after every writer has closed. A manifest alone must
        # never claim matching duration if an output is truncated or incomplete.
        for name in (*FILENAMES.values(), 'Preview.wav'):
            exported = sf.info(str(target / name))
            if exported.frames != output_frames or exported.samplerate != info['sample_rate']:
                raise CullingError(f'{name} failed the original-length verification. These files are incomplete.')
        if any(item['frames'] != info['frames'] for item in info['stems'].values()):
            notes.append('A one-frame export-rounding difference was padded with silence at the end of the shorter stem.')
        manifest = {
            'application': 'The Culling', 'schema_version': 2,
            'settings': {'bpm': info['bpm'], 'meter': '4/4', 'keep_percent': keep_percent,
                         'keep_percent_meaning': 'Approximate active part-time retained, not song duration.',
                         'intensity': intensity, 'seed': seed},
            'source': info, 'source_bar_rms': energies,
            'selection': 'Every source range retained at its original position; the cut map is an identity timeline.',
            'cuts': cuts, 'movement': moves, 'declick_fades': edges,
            'part_time': part_time,
            'output': {'sample_rate': info['sample_rate'], 'frames': output_frames,
                       'seconds': output_frames / info['sample_rate'],
                       'actual_keep_percent': 100.0,
                       'actual_parts_keep_percent': part_time['actual_keep_percent'],
                       'files': FILENAMES, 'subtypes': subtypes,
                       'preview': 'Preview.wav', 'preview_constant_gain': preview_gain},
            'notes': notes,
            'invariants': {'new_audio_added': False, 'repeated_phrases': False,
                           'reordered_phrases': False, 'time_stretching': False,
                            'pitch_shifting': False, 'input_files_changed': False,
                            'all_four_stems_share_exact_cut_boundaries': True,
                            'full_source_duration_preserved': True,
                            'source_samples_relocated': False,
                            'source_timeline_frames_deleted': False,
                            'original_ending_preserved': True,
                            'all_output_frame_counts_verified': True},
        }
        _write_json(target / 'Edit map.json', manifest)
        readme = (
            'THE CULLING\n\n'
            f'Original and edited duration: {info["seconds"]:.2f} seconds ({output_frames} frames).\n'
            f'Parts to keep: {keep_percent}%. Estimated active part-time retained: {part_time["actual_keep_percent"]:.1f}%.\n'
            f'Tempo: {info["bpm"]:g} BPM in 4/4. Movement: {intensity}. Seed: {seed}.\n\n'
            'Import Instruments.wav, Kick.wav, 808.wav, and Drums.wav together at the same start.\n'
            'They have the original full length and source timing. Their channel counts,\n'
            'sample rate, and gain are preserved except inside the documented dropouts\n'
            'and their short declick ramps. No audio was moved, shortened or added.\n\n'
            'Preview.wav is a combined listening reference with one constant normalization gain.\n'
            'Use the separate stems for mixing. Keep your original files as the source.\n\n'
            'The first beat, final full bar and complete ending tail are preserved.\n'
            'Parts to keep describes approximate active stem-time, not song duration.\n'
            'Activity is measured per beat; natural spaces between backing hits remain.\n'
            'At least one active backing stem is retained in every originally active beat.\n'
            'All files must originally have been exported from bar 1 at the same BPM and start.\n'
            'The tool checks format and length, but cannot prove that your exports are aligned.\n\n'
            'Edit map.json records the unchanged timeline, dropouts, density estimate and source hashes.\n'
            + ('\nNotes:\n' + '\n'.join('- ' + note for note in notes) + '\n' if notes else ''))
        with open(target / 'Read Me.txt', 'x', encoding='utf-8') as handle:
            handle.write(readme)
        marker.unlink()
        _notify(progress, f'Complete: original {output_frames / info["sample_rate"]:.1f}-second timeline, {len(moves)} dropouts, {part_time["actual_keep_percent"]:.1f}% active part-time retained.')
        return target
    except Exception as error:
        marker.write_text(f'EXPORT FAILED — these files are incomplete and should not be used.\n\n{error}\n', encoding='utf-8')
        raise
