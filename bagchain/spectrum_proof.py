"""Vibration spectrum, aliasing proof and physically feasible maintenance bands.

Every function here is pure: the same 200 captured points always yield the same
spectrum, and no amount of post-processing can recover a frequency the sampler
folded away.  Only the standard library is used.
"""
import cmath
import math

RMS_INSPECT_THRESHOLD = 0.4
# Bearing rolling-element defects live well above the shaft frequency and its
# low-order harmonics.  The band is motor-agnostic; shaft harmonics are derived
# from each motor's rated/measured shaft frequency.
DEFAULT_BEARING_BAND_HZ = (60.0, 500.0)
SHAFT_HARMONIC_ORDER = 4
BAND_TOLERANCE_HZ = 2.0
PEAK_MIN_AMPLITUDE = 1e-9


def rms(samples):
    """Root-mean-square acceleration in g, computed from the stored points."""
    if not samples:
        raise ValueError('empty_samples')
    total = 0.0
    for x in samples:
        if not isinstance(x, (int, float)) or isinstance(x, bool) or math.isnan(x) or math.isinf(x):
            raise ValueError('non_finite_sample')
        total += float(x) * float(x)
    return math.sqrt(total / len(samples))


def dft(x):
    """Exact O(n^2) discrete Fourier transform for short records."""
    n = len(x)
    if n == 0:
        raise ValueError('empty_samples')
    out = []
    for k in range(n):
        acc = 0j
        for j, v in enumerate(x):
            acc += complex(v) * cmath.exp(-2j * math.pi * k * j / n)
        out.append(acc)
    return out


def fft(x):
    """Radix-2 iterative FFT; input is zero-padded by the caller when needed."""
    n = len(x)
    if n == 0:
        raise ValueError('empty_samples')
    if n & (n - 1):
        raise ValueError('fft_requires_power_of_two')
    a = [complex(v) for v in x]
    bits = n.bit_length() - 1
    for i in range(n):
        j = int(bin(i)[2:].zfill(bits)[::-1], 2)
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    while length <= n:
        half = length // 2
        w = cmath.exp(-2j * math.pi / length)
        for start in range(0, n, length):
            wk = 1j
            for k in range(half):
                even = a[start + k]
                odd = a[start + k + half] * wk
                a[start + k] = even + odd
                a[start + k + half] = even - odd
                wk *= w
        length *= 2
    return a


def _next_power_of_two(n):
    return 1 << (n - 1).bit_length()


def one_sided_spectrum(samples, sample_hz, pad_to_power_of_two=False):
    """Magnitude spectrum with only physically reachable bins (0..Nyquist).

    Bins above Nyquist are not reported: a real sampler cannot distinguish them
    from their folded aliases, so emitting a peak there would manufacture
    evidence the points do not contain.  By default the exact N-point DFT is
    used so every reported bin is a real sampled bin; padding is display-only.
    """
    if sample_hz <= 0:
        raise ValueError('invalid_sample_hz')
    n = len(samples)
    if n == 0:
        raise ValueError('empty_samples')
    if pad_to_power_of_two:
        size = _next_power_of_two(n)
        bins = fft(list(samples) + [0.0] * (size - n)) if size != n else fft(samples)
    else:
        size = n
        bins = dft(samples)
    half = size // 2 + 1
    freqs = [k * sample_hz / size for k in range(half)]
    amps = [abs(bins[k]) * (2.0 / n) for k in range(half)]
    amps[0] = abs(bins[0]) / n
    if size % 2 == 0:
        amps[-1] = abs(bins[half - 1]) / n
    return [{'hz': freqs[k], 'amplitude_g': amps[k], 'bin': k} for k in range(half)]


def alias_candidates(frequency_hz, sample_hz):
    """All nonnegative frequencies that fold to the same observed frequency."""
    if sample_hz <= 0 or frequency_hz < 0:
        raise ValueError('invalid_alias_input')
    folded = frequency_hz % sample_hz
    observed = min(folded, sample_hz - folded) if folded else 0.0
    # Every true frequency k*fs +/- folded (k >= 0) lands on the same bin.
    candidates = []
    for k in range(3):
        base = k * sample_hz
        for cand in (base + folded, base - folded if folded else None,
                     base + sample_hz - folded if folded else None):
            if cand is not None and cand >= 0 and cand not in candidates:
                candidates.append(cand)
    return sorted(candidates), observed


def shaft_frequency_hz(rated_rpm):
    if rated_rpm <= 0:
        raise ValueError('invalid_rated_rpm')
    return rated_rpm / 60.0


def classify_peak(hz, shaft_hz, bearing_band=DEFAULT_BEARING_BAND_HZ,
                  tolerance=BAND_TOLERANCE_HZ, harmonic_order=SHAFT_HARMONIC_ORDER):
    """Separate shaft frequency/low harmonics from rolling-element defect band."""
    for order in range(1, harmonic_order + 1):
        if abs(hz - order * shaft_hz) <= tolerance:
            return 'shaft', order
    low, high = bearing_band
    if low - tolerance <= hz <= high + tolerance:
        return 'bearing', None
    return 'other', None


def dominant_peaks(spectrum, limit=5):
    ranked = sorted((p for p in spectrum if p['amplitude_g'] >= PEAK_MIN_AMPLITUDE),
                    key=lambda p: p['amplitude_g'], reverse=True)
    return ranked[:limit]


def _band_energy(spectrum, low_hz, high_hz):
    return sum(p['amplitude_g'] ** 2 for p in spectrum if low_hz <= p['hz'] <= high_hz)


def tonal_frequency(samples, sample_hz, search_band=None):
    """Locate a tone robustly against DFT leakage.

    Off-bin tones smear across neighbouring bins, so the peak bin alone can
    mislabel the band.  The dominant DFT bin's phase gives the sub-bin frequency
    (Jacobsen estimator); the result is only accepted when the neighbouring
    bins carry the expected leakage energy, i.e. the signal really is tonal.
    """
    n = len(samples)
    spectrum = one_sided_spectrum(samples, sample_hz)
    if search_band is not None:
        low, high = search_band
        candidates = [p for p in spectrum if low <= p['hz'] <= high]
    else:
        candidates = spectrum[1:]
    if not candidates:
        return None
    peak = max(candidates, key=lambda p: p['amplitude_g'])
    k = peak['bin']
    full = dft(samples)
    if 0 < k < n // 2:
        xm, x0, xp = full[k - 1], full[k], full[k + 1]
        denom = 2 * abs(x0) - abs(xm) - abs(xp)
        if denom > 0:
            delta = (abs(xm) - abs(xp)) / denom
        else:
            delta = 0.0
        est = (k + delta) * sample_hz / n
    else:
        est = peak['hz']
    neighbour_energy = (abs(full[k - 1]) ** 2 + abs(full[k + 1]) ** 2) if 0 < k < n // 2 else 0.0
    peak_energy = abs(full[k]) ** 2
    tonal = peak_energy <= 0 or (neighbour_energy / peak_energy) < 2.0
    return {'hz': est, 'amplitude_g': peak['amplitude_g'], 'bin_hz': peak['hz'],
            'tonal': tonal}


def detect_saturation(samples, sensor_limit_g=None):
    """Clipped/rail-stuck samples mean the channel saturated."""
    if not samples:
        raise ValueError('empty_samples')
    if sensor_limit_g is not None:
        clipped = sum(1 for x in samples if abs(x) >= sensor_limit_g)
        if clipped:
            return True, clipped
    # Repeated equal extreme runs are the classic saturation fingerprint.
    longest = run = 1
    for a, b in zip(samples, samples[1:]):
        if a == b and a != 0:
            run += 1
            longest = max(longest, run)
        else:
            run = 1
    return longest >= 4, longest if longest >= 4 else 0


def analyze_capture(samples, sample_hz, rated_rpm, bearing_band=DEFAULT_BEARING_BAND_HZ,
                    sensor_limit_g=None, aa_filter=None):
    """Physically feasible reading of one acceleration segment.

    Returns the reachable spectrum plus an explicit ``decidable`` flag.  When
    the dominant peak has out-of-band aliases, ``alias_ambiguous`` lists the
    indistinguishable true frequencies and no bearing-vs-belt verdict is made.
    """
    shaft_hz = shaft_frequency_hz(rated_rpm)
    measured_rms = rms(samples)
    saturated, saturated_count = detect_saturation(samples, sensor_limit_g)
    spectrum = one_sided_spectrum(samples, sample_hz)
    peaks = dominant_peaks(spectrum)
    labels = []
    for p in peaks:
        band, order = classify_peak(p['hz'], shaft_hz, bearing_band)
        labels.append({'hz': p['hz'], 'amplitude_g': p['amplitude_g'],
                       'band': band, 'shaft_order': order})
    alias_ambiguous = []
    filter_note = None
    if peaks:
        top = peaks[0]['hz']
        candidates, observed = alias_candidates(top, sample_hz)
        reachable = [c for c in candidates if c <= sample_hz / 2.0]
        hidden = [c for c in candidates if c > sample_hz / 2.0]
        # An analog anti-alias filter with a finite cutoff can rule out hidden
        # candidates above its attenuation band; anything merely "configured"
        # but not proven still leaves the ambiguity open.
        if aa_filter and aa_filter.get('cutoff_hz') is not None:
            cutoff = float(aa_filter['cutoff_hz'])
            survivors = [c for c in hidden if c <= cutoff]
            filter_note = {'cutoff_hz': cutoff,
                           'attenuated_candidates': [c for c in hidden if c > cutoff]}
            hidden = survivors
        if hidden:
            alias_ambiguous = [{'observed_hz': observed, 'reachable_hz': reachable,
                                'hidden_true_hz': hidden}]
    decidable = not alias_ambiguous and not saturated
    if decidable and labels:
        top_band = labels[0]['band']
        verdict = 'tension_only_candidate' if top_band == 'shaft' else (
            'bearing_replace_candidate' if top_band == 'bearing' else 'unclassified')
    else:
        verdict = 'indeterminate'
    return {'sample_hz': sample_hz, 'n_points': len(samples), 'shaft_hz': shaft_hz,
            'rms_g': measured_rms, 'spectrum': spectrum, 'peaks': labels,
            'saturated': saturated, 'saturated_samples': saturated_count,
            'alias_ambiguous': alias_ambiguous, 'filter_note': filter_note,
            'decidable': decidable, 'verdict': verdict}


def validate_time_base_segments(segments):
    """One recorder power-on session shares one time base.

    Segments may not individually re-rate ``sample_hz`` to manufacture a
    fault.  Overlapping/duplicate sequence numbers and out-of-order segments
    reject the batch; dropped frames are reported, not silently filled.
    """
    if not segments:
        raise ValueError('no_segments')
    groups = {}
    for seg in segments:
        groups.setdefault(seg['time_base_id'], []).append(seg)
    report = {}
    for tb_id, segs in groups.items():
        rates = {int(s['sample_hz']) for s in segs}
        if len(rates) > 1:
            raise ValueError('sample_rate_conflict_within_time_base')
        ordered = sorted(segs, key=lambda s: s['seq_index'])
        seqs = [int(s['seq_index']) for s in ordered]
        if len(set(seqs)) != len(seqs):
            raise ValueError('duplicate_seq_index')
        gaps = [b - a for a, b in zip(seqs, seqs[1:])]
        if any(g <= 0 for g in gaps):
            raise ValueError('segment_seq_out_of_order')
        dropped = sum(max(0, g - 1) for g in gaps)
        saturated = any(detect_saturation(s['samples'], s.get('sensor_limit_g'))[0]
                        for s in ordered)
        report[tb_id] = {'sample_hz': rates.pop(), 'segment_count': len(ordered),
                         'dropped_frames': dropped, 'saturated': saturated,
                         'seq_indexes': seqs}
    return report


def high_rate_buffer_resolution(buffer_samples, buffer_hz, rated_rpm,
                                bearing_band=DEFAULT_BEARING_BAND_HZ):
    """Re-analyse a synchronously stored high-rate oscilloscope buffer.

    A higher Nyquist that actually covers the bearing band can separate belt
    imbalance (shaft/low harmonics) from rolling-element defects using band
    energy plus a sub-bin tonal estimate, so DFT leakage cannot swap the bands.
    Buffers that still cannot reach the bearing band remain indeterminate.
    """
    if buffer_hz <= 0:
        raise ValueError('invalid_buffer_hz')
    shaft_hz = shaft_frequency_hz(rated_rpm)
    low, high = bearing_band
    covers_bearing_band = buffer_hz / 2.0 >= high
    saturated, saturated_count = detect_saturation(buffer_samples)
    spectrum = one_sided_spectrum(buffer_samples, buffer_hz)
    shaft_cut = SHAFT_HARMONIC_ORDER * shaft_hz + BAND_TOLERANCE_HZ
    shaft_energy = _band_energy(spectrum, 0.0, shaft_cut)
    bearing_energy = _band_energy(spectrum, low, high)
    tone = tonal_frequency(buffer_samples, buffer_hz)
    tone_band = None
    if tone is not None:
        tone_band, _ = classify_peak(tone['hz'], shaft_hz, bearing_band)
    if not covers_bearing_band or saturated:
        verdict, decidable = 'indeterminate', False
    elif bearing_energy > shaft_energy and tone_band == 'bearing':
        verdict, decidable = 'bearing_replace_candidate', True
    elif shaft_energy >= bearing_energy and tone_band == 'shaft':
        verdict, decidable = 'tension_only_candidate', True
    else:
        verdict, decidable = 'indeterminate', False
    return {'buffer_hz': buffer_hz, 'n_points': len(buffer_samples),
            'shaft_hz': shaft_hz, 'rms_g': rms(buffer_samples),
            'covers_bearing_band': covers_bearing_band, 'saturated': saturated,
            'saturated_samples': saturated_count, 'spectrum': spectrum,
            'shaft_band_energy': shaft_energy, 'bearing_band_energy': bearing_energy,
            'tonal_frequency_hz': None if tone is None else tone['hz'],
            'tonal_band': tone_band, 'decidable': decidable, 'verdict': verdict}


def alias_equivalence(rms_g=0.5, shaft_hz=20.0, bearing_hz=180.0,
                      sample_hz=200.0, n_points=200, phase=math.pi):
    """The strict same-samples counter-proof: A (belt) and B (bearing).

    A: a(t) = sqrt(2)*rms*sin(2*pi*shaft*t)  -- adjustable belt imbalance
    B: a(t) = sqrt(2)*rms*sin(2*pi*bearing*t + pi) -- rolling-element damage

    At t_n = n/sample_hz the two are equal point-by-point because the bearing
    frequency folds onto the shaft frequency, so every stored point, the RMS
    and the original ``inspect`` advisory are identical while the maintenance
    outcomes differ.  No transform of these points can recover B.
    """
    amp = math.sqrt(2.0) * rms_g
    process_a = [amp * math.sin(2 * math.pi * shaft_hz * n / sample_hz)
                 for n in range(n_points)]
    process_b = [amp * math.sin(2 * math.pi * bearing_hz * n / sample_hz + phase)
                 for n in range(n_points)]
    max_abs_diff = max(abs(a - b) for a, b in zip(process_a, process_b))
    folded = bearing_hz % sample_hz
    observed = min(folded, sample_hz - folded)
    candidates, _ = alias_candidates(bearing_hz, sample_hz)
    spectrum = one_sided_spectrum(process_a, sample_hz, pad_to_power_of_two=False)
    top = dominant_peaks(spectrum, limit=1)[0]
    return {
        'motor_rated_rpm': 1200,
        'shaft_hz': shaft_hz,
        'sample_hz': sample_hz,
        'n_points': n_points,
        'rms_g': rms(samples=process_a),
        'rms_advisory': 'inspect' if rms_g >= RMS_INSPECT_THRESHOLD else 'run',
        'process_a': {'cause': 'adjustable_belt_imbalance', 'true_frequency_hz': shaft_hz,
                      'maintenance': 'tension_only', 'samples': process_a},
        'process_b': {'cause': 'bearing_rolling_element_damage', 'true_frequency_hz': bearing_hz,
                      'maintenance': 'replace_bearing', 'samples': process_b},
        'folded_frequency_hz': observed,
        'alias_candidates_hz': candidates,
        'max_pointwise_abs_diff': max_abs_diff,
        'pointwise_equal': max_abs_diff < 1e-12,
        'observed_peak_hz': top['hz'],
        'observed_peak_band': classify_peak(top['hz'], shaft_hz)[0],
        'fft_cannot_distinguish': True,
        'review_paths': [
            {'kind': 'high_rate_buffer', 'requires': '1kHz raw oscilloscope buffer stored before shutdown',
             'settles': 'historical A/B band and bearing warranty claim'},
            {'kind': 'teardown_microscopy', 'requires': 'open bearing raceway now, keep micrographs',
             'settles': 'current replace-vs-tension only; not the historical spectrum or fault onset'},
        ],
    }
