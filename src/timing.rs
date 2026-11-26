//! Assigning a host timestamp to every sample.
//!
//! The headset sends no timestamps. Bytes arrive in bursts (Bluetooth and
//! USB both batch), so several samples share one arrival time even though
//! they were digitised `1 / sample_rate` seconds apart, and a burst can be
//! held up for tens of milliseconds, or much longer after a stall.
//!
//! # Model
//!
//! Sample `n` was digitised at `n / sample_rate` on the headset's clock. The
//! host sees it at `arrival = n / sample_rate + offset + delay`, where `delay`
//! is never negative and is smallest for the newest sample of a prompt read.
//! So `arrival - n / sample_rate` is always at or above `offset`, and the
//! smallest value seen is the best estimate of it. A sample's timestamp is
//! `n / sample_rate + offset`: evenly spaced, with the jitter of the link
//! removed.
//!
//! The offset is not constant: the headset's clock and the host's differ by
//! up to a few hundred parts per million. Downward moves are taken at once (a
//! smaller value is always better evidence). Upward moves are taken once per
//! [`WINDOW_SECONDS`] of host time from the smallest value in that window,
//! limited to [`MAX_DRIFT`], so slow reads cannot push timestamps late. If
//! [`RESYNC_WINDOWS`] windows in a row all sit more than [`RESYNC_SECONDS`]
//! above the offset, the stream has jumped (samples were lost without being
//! counted, or the host clock was set) and the offset is moved there in one
//! step. A shorter stretch of late reads (a stalled link, a busy host) is
//! treated as delay and leaves the spacing alone.
//!
//! # What the timestamps mean
//!
//! Host clock time (seconds, same clock as `arrival`) at which the sample
//! reached this program over the fastest path seen, not the moment it was
//! digitised: the constant part of the link's latency is not known and is
//! not removed. Timestamps from one stream never go backwards: a timestamp
//! that would be earlier than the last one handed out is raised to it. So
//! after the host clock steps back, or after the offset moves down by more
//! than the length of the chunk before, consecutive chunks share one `time`
//! until the fitted times pass it again, and in that stretch
//! `time + k / sample_rate` of one chunk overlaps the next. During the
//! first second or so the offset is still settling and spacing can be
//! uneven. Later, a read that arrives sooner than any before it moves the
//! offset down, which shortens one step: by about a millisecond at a time in
//! the logs from this headset.

/// How often the offset may move upward, in seconds of host time.
pub const WINDOW_SECONDS: f64 = 2.0;
/// Largest clock-rate difference followed upward, as a fraction (500 ppm).
pub const MAX_DRIFT: f64 = 500e-6;
/// Windows this far above the offset, in seconds, point to a jump, not drift.
///
/// The packet counter has 7 bits, so a run of exactly 128 lost packets (or a
/// multiple) is not seen as a loss: at 500 Hz it moves every arrival 0.256 s
/// later than the sample count predicts. This threshold sits just below that,
/// so such an uncounted loss is taken as a jump; the margin is 6 ms. At
/// 1000 Hz and above, 128 samples last 0.128 s or less, under this threshold,
/// so there such a loss is not taken as a jump.
pub const RESYNC_SECONDS: f64 = 0.25;
/// Consecutive windows that must agree before a jump is taken.
pub const RESYNC_WINDOWS: u32 = 5;

/// Running state of the clock model for one stream.
#[derive(Debug, Clone, Default)]
pub struct ClockState {
    /// Current estimate of `arrival - index / sample_rate` at zero delay.
    offset: Option<f64>,
    /// Smallest `arrival - index / sample_rate` in the current window.
    window_min: Option<f64>,
    /// Arrival time at which the current window began.
    window_start: f64,
    /// Arrival time of the read being observed.
    read_arrival: f64,
    /// Windows in a row that sat more than `RESYNC_SECONDS` above the offset.
    late_windows: u32,
    /// Last timestamp handed out.
    last_timestamp: Option<f64>,
}

impl ClockState {
    /// Take in one sample's arrival. Call for every sample, in order, before
    /// asking for the timestamps of the read it came in.
    ///
    /// * `arrival` - host clock reading when the bytes holding this sample were read
    /// * `index` - sample number since the stream started, counting lost samples
    /// * `sample_rate` - nominal rate reported by the device, in Hz
    pub fn observe(&mut self, arrival: f64, index: u64, sample_rate: f64) {
        let usable = arrival.is_finite() && sample_rate.is_finite() && sample_rate > 0.0;
        if !usable {
            return;
        }
        let seen = arrival - index as f64 / sample_rate;
        let Some(offset) = self.offset else {
            self.offset = Some(seen);
            self.window_start = arrival;
            self.read_arrival = arrival;
            return;
        };
        // A window closes only when a new read begins, and only on reads that
        // are complete: within one read the oldest samples always look late.
        if arrival != self.read_arrival {
            let window = arrival - self.window_start;
            if !(0.0..WINDOW_SECONDS * 10.0).contains(&window) {
                // The host clock was set, or nothing was read for a long
                // time: this window measures nothing. Start again.
                self.window_min = None;
                self.window_start = arrival;
            } else if window >= WINDOW_SECONDS {
                self.close_window(offset, window);
                self.window_start = arrival;
            }
            self.read_arrival = arrival;
        }
        if self.offset.is_some_and(|offset| seen < offset) {
            self.offset = Some(seen);
        }
        self.window_min = Some(self.window_min.map_or(seen, |m| m.min(seen)));
    }

    /// Move the offset upward from the smallest value of a finished window.
    fn close_window(&mut self, offset: f64, window: f64) {
        let Some(smallest) = self.window_min.take() else {
            return;
        };
        let excess = smallest - offset;
        if excess > RESYNC_SECONDS {
            self.late_windows += 1;
            if self.late_windows >= RESYNC_WINDOWS {
                self.offset = Some(smallest);
                self.late_windows = 0;
            }
            return;
        }
        self.late_windows = 0;
        if excess > 0.0 {
            self.offset = Some(offset + excess.min(MAX_DRIFT * window));
        }
    }

    /// Timestamp, in seconds, of sample `index`, from what has been observed
    /// so far. Never smaller than the previous timestamp returned. Before
    /// anything has been observed the result is `index / sample_rate`.
    pub fn timestamp(&mut self, index: u64, sample_rate: f64) -> f64 {
        let position = if sample_rate.is_finite() && sample_rate > 0.0 {
            index as f64 / sample_rate
        } else {
            0.0
        };
        let mut time = position + self.offset.unwrap_or(0.0);
        if let Some(last) = self.last_timestamp {
            time = time.max(last);
        }
        self.last_timestamp = Some(time);
        time
    }

    /// Current offset estimate (`timestamp - index / sample_rate`), if any
    /// sample has been observed.
    pub fn offset(&self) -> Option<f64> {
        self.offset
    }
}

/// Timestamp, in seconds, for one sample: [`ClockState::observe`] followed by
/// [`ClockState::timestamp`].
///
/// When several samples arrive in one read, observe all of them before taking
/// timestamps (as [`crate::chunk::Chunker`] does): the newest sample of a
/// read is the one that waited least, so it places the others.
pub fn assign_timestamp(arrival: f64, index: u64, sample_rate: f64, state: &mut ClockState) -> f64 {
    state.observe(arrival, index, sample_rate);
    state.timestamp(index, sample_rate)
}
