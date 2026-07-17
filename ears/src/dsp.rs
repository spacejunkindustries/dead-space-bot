//! 48 kHz stereo → 16 kHz mono conversion (GDD §4 Ears table, §5).
//!
//! Stereo interleaved i16 is folded to mono (per-pair average), lowpassed with
//! a 61-tap Hamming-windowed sinc FIR cut off at 7.5 kHz, then decimated 3:1.
//! The FIR keeps its delay line and decimation phase across calls so per-user
//! streams stay continuous over 20 ms tick boundaries.
//!
//! Everything here operates on in-memory slices only — audio never touches
//! disk (hard constraint 5).

/// Number of FIR taps.
pub const TAPS: usize = 61;
/// Decimation factor: 48 kHz → 16 kHz.
pub const DECIMATION: usize = 3;
/// Lowpass cutoff in Hz (below the 8 kHz output Nyquist to leave a
/// transition band for the 61-tap window).
pub const CUTOFF_HZ: f32 = 7_500.0;
/// Input sample rate in Hz.
pub const INPUT_RATE_HZ: f32 = 48_000.0;

/// Fold interleaved stereo (`L, R, L, R, …`) into mono by averaging each pair.
///
/// A trailing unpaired sample (which never occurs for well-formed 20 ms
/// Discord ticks) is ignored.
#[must_use]
pub fn stereo_to_mono(interleaved: &[i16]) -> Vec<i16> {
    interleaved
        .chunks_exact(2)
        .map(|pair| {
            let sum = i32::from(pair[0]) + i32::from(pair[1]);
            (sum / 2) as i16
        })
        .collect()
}

/// Compute the 61-tap Hamming-windowed sinc lowpass, normalised to unity DC
/// gain.
#[must_use]
fn design_taps() -> [f32; TAPS] {
    let m = (TAPS - 1) as f32; // filter order (60)
    let fc = CUTOFF_HZ / INPUT_RATE_HZ; // normalised cutoff (cycles/sample)
    let mut taps = [0.0f32; TAPS];
    for (n, tap) in taps.iter_mut().enumerate() {
        let x = n as f32 - m / 2.0;
        // sinc(2 * fc * x), with the removable singularity at x == 0.
        let sinc = if x == 0.0 {
            1.0
        } else {
            let arg = std::f32::consts::PI * 2.0 * fc * x;
            arg.sin() / arg
        };
        let hamming =
            0.54 - 0.46 * (2.0 * std::f32::consts::PI * n as f32 / m).cos();
        *tap = 2.0 * fc * sinc * hamming;
    }
    let sum: f32 = taps.iter().sum();
    for tap in &mut taps {
        *tap /= sum;
    }
    taps
}

/// Stateful 3:1 decimator: 61-tap FIR history plus decimation phase, carried
/// across calls. One instance per SSRC.
#[derive(Debug, Clone)]
pub struct Decimator {
    taps: [f32; TAPS],
    /// Last `TAPS - 1` input samples, oldest first.
    history: [f32; TAPS - 1],
    /// Input samples consumed since the last emitted output, modulo 3.
    phase: usize,
}

impl Default for Decimator {
    fn default() -> Self {
        Self::new()
    }
}

impl Decimator {
    #[must_use]
    pub fn new() -> Self {
        Self {
            taps: design_taps(),
            history: [0.0; TAPS - 1],
            phase: 0,
        }
    }

    /// Lowpass + decimate a block of mono 48 kHz samples, returning mono
    /// 16 kHz samples. Block boundaries are seamless.
    #[must_use]
    pub fn process(&mut self, mono48k: &[i16]) -> Vec<i16> {
        if mono48k.is_empty() {
            return Vec::new();
        }
        // Extended buffer: [history | new samples]; sample at extended index
        // p has full FIR context once p >= TAPS - 1, which holds for every
        // new-sample position.
        let hist_len = self.history.len();
        let mut ext = Vec::with_capacity(hist_len + mono48k.len());
        ext.extend_from_slice(&self.history);
        ext.extend(mono48k.iter().map(|&s| f32::from(s)));

        let mut out = Vec::with_capacity(mono48k.len() / DECIMATION + 1);
        for i in 0..mono48k.len() {
            if self.phase == 0 {
                let p = hist_len + i;
                let mut acc = 0.0f32;
                for (k, &tap) in self.taps.iter().enumerate() {
                    acc += tap * ext[p - k];
                }
                out.push(acc.round().clamp(f32::from(i16::MIN), f32::from(i16::MAX)) as i16);
            }
            self.phase = (self.phase + 1) % DECIMATION;
        }

        // Carry the last TAPS-1 input samples forward.
        let tail = &ext[ext.len() - hist_len..];
        self.history.copy_from_slice(tail);
        out
    }
}

/// Convenience: fold stereo 48 kHz to mono and decimate to 16 kHz.
#[must_use]
pub fn stereo48k_to_mono16k(decimator: &mut Decimator, interleaved48k: &[i16]) -> Vec<i16> {
    let mono = stereo_to_mono(interleaved48k);
    decimator.process(&mono)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sine(freq_hz: f32, rate_hz: f32, amplitude: f32, samples: usize) -> Vec<i16> {
        (0..samples)
            .map(|n| {
                let t = n as f32 / rate_hz;
                (amplitude * (2.0 * std::f32::consts::PI * freq_hz * t).sin()) as i16
            })
            .collect()
    }

    fn rms(samples: &[i16]) -> f64 {
        if samples.is_empty() {
            return 0.0;
        }
        let sum: f64 = samples.iter().map(|&s| f64::from(s) * f64::from(s)).sum();
        (sum / samples.len() as f64).sqrt()
    }

    fn db(ratio: f64) -> f64 {
        20.0 * ratio.log10()
    }

    #[test]
    fn stereo_fold_averages_pairs() {
        assert_eq!(stereo_to_mono(&[100, 200, -50, -150]), vec![150, -100]);
    }

    #[test]
    fn stereo_fold_cancels_antiphase() {
        assert_eq!(stereo_to_mono(&[1000, -1000, 32767, -32767]), vec![0, 0]);
    }

    #[test]
    fn stereo_fold_ignores_trailing_odd_sample() {
        assert_eq!(stereo_to_mono(&[10, 20, 99]), vec![15]);
    }

    #[test]
    fn dc_passes_at_unity_gain() {
        let mut d = Decimator::new();
        let input = vec![1000i16; 48_000]; // 1s of DC
        let out = d.process(&input);
        assert_eq!(out.len(), 16_000);
        // Skip the filter warm-up (TAPS samples at 48k ≈ 21 outputs at 16k).
        let steady = &out[100..];
        let mean: f64 =
            steady.iter().map(|&s| f64::from(s)).sum::<f64>() / steady.len() as f64;
        assert!(
            (mean - 1000.0).abs() < 10.0,
            "DC gain off: mean = {mean}"
        );
    }

    #[test]
    fn tone_3khz_survives_within_1db() {
        let mut d = Decimator::new();
        let amplitude = 10_000.0;
        let input = sine(3_000.0, 48_000.0, amplitude, 48_000);
        let out = d.process(&input);
        let steady = &out[200..];
        let expected_rms = f64::from(amplitude) / std::f64::consts::SQRT_2;
        let loss_db = db(rms(steady) / expected_rms).abs();
        assert!(
            loss_db < 1.0,
            "3 kHz tone attenuated by {loss_db:.2} dB (limit 1 dB)"
        );
    }

    #[test]
    fn tone_20khz_attenuated_over_40db() {
        // 20 kHz is above the 8 kHz output Nyquist: without the lowpass it
        // would alias into band. Post-decimation energy must be >40 dB down.
        let mut d = Decimator::new();
        let amplitude = 10_000.0;
        let input = sine(20_000.0, 48_000.0, amplitude, 48_000);
        let out = d.process(&input);
        let steady = &out[200..];
        let input_rms = f64::from(amplitude) / std::f64::consts::SQRT_2;
        let atten_db = -db(rms(steady) / input_rms);
        assert!(
            atten_db > 40.0,
            "20 kHz alias only {atten_db:.1} dB down (need >40 dB)"
        );
    }

    #[test]
    fn block_boundaries_are_seamless() {
        // Processing one long block must equal processing the same samples in
        // 20 ms (960-sample) chunks — the state carries across calls.
        let input = sine(1_000.0, 48_000.0, 8_000.0, 9_600);
        let mut whole = Decimator::new();
        let expected = whole.process(&input);

        let mut chunked = Decimator::new();
        let mut got = Vec::new();
        for chunk in input.chunks(960) {
            got.extend(chunked.process(chunk));
        }
        assert_eq!(expected, got);
    }

    #[test]
    fn output_rate_is_one_third() {
        let mut d = Decimator::new();
        // 960 mono samples per 20ms tick at 48k → 320 at 16k.
        let out = d.process(&vec![0i16; 960]);
        assert_eq!(out.len(), 320);
        // Phase carries: 961 total inputs would give 321 outputs overall.
        let out2 = d.process(&[0i16; 1]);
        assert_eq!(out2.len(), 1);
    }

    #[test]
    fn empty_input_yields_empty_output() {
        let mut d = Decimator::new();
        assert!(d.process(&[]).is_empty());
    }
}
