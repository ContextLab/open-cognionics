//! The clock model: timestamps from arrival times (see `src/timing.rs`).

use open_cognionics::timing::{
    assign_timestamp, ClockState, MAX_DRIFT, RESYNC_SECONDS, RESYNC_WINDOWS, WINDOW_SECONDS,
};

const RATE: f64 = 500.0;
const PERIOD: f64 = 1.0 / RATE;
const PACKET: u64 = 75;

/// One read on the port: when it returned and the sample indices it completed.
struct Read {
    arrival: f64,
    first: u64,
    count: u64,
}

/// The reads logged from the real headset (`tests/fixtures/read_timing_10s.csv`).
fn logged_reads() -> Vec<Read> {
    let text = std::fs::read_to_string("tests/fixtures/read_timing_10s.csv").unwrap();
    text.lines()
        .skip(1)
        .map(|line| {
            let f: Vec<&str> = line.split(',').collect();
            let arrival: f64 = f[0].parse().unwrap();
            let offset: u64 = f[1].parse().unwrap();
            let nbytes: u64 = f[2].parse().unwrap();
            let first = offset / PACKET;
            Read {
                arrival,
                first,
                count: (offset + nbytes) / PACKET - first,
            }
        })
        .collect()
}

/// Reads of `per_read` samples from a device whose clock runs at `rate`,
/// each delivered `delay(k)` seconds after its newest sample was digitised.
fn steady_reads(
    seconds: f64,
    rate: f64,
    per_read: u64,
    start: f64,
    delay: impl Fn(u64) -> f64,
) -> Vec<Read> {
    let reads = (seconds * rate) as u64 / per_read;
    (0..reads)
        .map(|k| {
            let first = k * per_read;
            let newest = first + per_read - 1;
            Read {
                arrival: start + newest as f64 / rate + delay(k),
                first,
                count: per_read,
            }
        })
        .collect()
}

/// Run reads through the model the way the chunker does: observe every
/// sample of a read, then stamp them. Returns (index, timestamp, arrival).
fn stamp(reads: &[Read]) -> Vec<(u64, f64, f64)> {
    let mut clock = ClockState::default();
    let mut out = Vec::new();
    for read in reads {
        for i in read.first..read.first + read.count {
            clock.observe(read.arrival, i, RATE);
        }
        for i in read.first..read.first + read.count {
            out.push((i, clock.timestamp(i, RATE), read.arrival));
        }
    }
    out
}

fn steps(stamped: &[(u64, f64, f64)]) -> Vec<f64> {
    stamped.windows(2).map(|w| w[1].1 - w[0].1).collect()
}

#[test]
fn real_reads_give_evenly_spaced_timestamps() {
    let reads = logged_reads();
    assert_eq!(reads.len(), 201);
    let stamped = stamp(&reads);
    assert!(stamped.len() > 4900);
    assert!(steps(&stamped).iter().all(|d| *d >= 0.0));
    // The first reads drain about 0.6 s of buffered stream. Once that is
    // through, steps are one sample period, except where a read arrives
    // sooner than any before it and the offset moves down: in this log that
    // happens a handful of times and by at most 1.5 ms.
    let settled: Vec<_> = stamped.iter().filter(|s| s.0 >= 500).cloned().collect();
    let mut corrections = 0;
    for d in steps(&settled) {
        assert!((d - PERIOD).abs() < 1.5e-3, "step {d}");
        if (d - PERIOD).abs() > 1e-9 {
            corrections += 1;
        }
    }
    assert!(corrections <= 10, "{corrections} corrections");
}

#[test]
fn real_reads_are_never_stamped_after_they_arrived() {
    let stamped = stamp(&logged_reads());
    for (i, time, arrival) in &stamped {
        assert!(time <= &(arrival + 1e-9), "sample {i}: {time} > {arrival}");
    }
    // The newest sample of each settled read is stamped close to its arrival:
    // the reads come about 50 ms apart, and the delay beyond the fastest
    // read seen stays under 15 ms (12.2 ms at worst in this log).
    let reads = logged_reads();
    let mut worst: f64 = 0.0;
    for read in reads.iter().filter(|r| r.first >= 500 && r.count > 0) {
        let newest = read.first + read.count - 1;
        let (_, time, arrival) = stamped.iter().find(|s| s.0 == newest).unwrap();
        worst = worst.max(arrival - time);
    }
    assert!(worst < 0.015, "worst lag {worst}");
}

#[test]
fn a_backlog_at_the_start_does_not_leave_timestamps_late() {
    // The log starts with about 0.6 s of buffered samples delivered at once.
    // Stamping by arrival would put sample 0 at the first read; the model
    // puts it about 0.6 s before the first read.
    let reads = logged_reads();
    let stamped = stamp(&reads);
    let settled = stamped.iter().find(|s| s.0 == 2500).unwrap();
    let start_of_stream = settled.1 - 2500.0 * PERIOD;
    let lead = reads[0].arrival - start_of_stream;
    assert!(lead > 0.5 && lead < 0.7, "lead {lead}");
}

#[test]
fn jitter_is_removed() {
    // 8 samples per read, delays between 3 and 15 ms, 3 ms about once in 16.
    let reads = steady_reads(20.0, RATE, 8, 1000.0, |k| {
        0.003 + 0.012 * ((k * 7 % 16) as f64 / 15.0)
    });
    let stamped = stamp(&reads);
    let settled: Vec<_> = stamped.iter().filter(|s| s.0 >= 500).cloned().collect();
    for d in steps(&settled) {
        assert!((d - PERIOD).abs() < 1e-9, "step {d}");
    }
    // Timestamps sit at the smallest delay, 3 ms after digitisation.
    let (i, time, _) = settled[100];
    assert!((time - (1000.0 + i as f64 / RATE + 0.003)).abs() < 1e-9);
}

#[test]
fn a_slow_device_clock_is_followed() {
    // Device 200 ppm slow: against the nominal rate, arrivals drift 24 ms
    // late over 120 s. Timestamps must follow, not stay at the first offset.
    let rate = RATE * (1.0 - 200e-6);
    let reads = steady_reads(120.0, rate, 8, 50.0, |_| 0.004);
    let stamped = stamp(&reads);
    let (_, time, arrival) = stamped.last().unwrap();
    assert!((arrival - time).abs() < 0.002, "lag {}", arrival - time);
    assert!(steps(&stamped).iter().all(|d| *d >= 0.0));
}

#[test]
fn a_fast_device_clock_is_followed() {
    let rate = RATE * (1.0 + 200e-6);
    let reads = steady_reads(120.0, rate, 8, 50.0, |_| 0.004);
    let stamped = stamp(&reads);
    let (_, time, arrival) = stamped.last().unwrap();
    assert!((arrival - time).abs() < 0.002, "lag {}", arrival - time);
    assert!(steps(&stamped).iter().all(|d| *d >= 0.0));
}

#[test]
fn a_stall_then_a_burst_keeps_the_spacing() {
    // Nothing is read for 3 s, then the held samples arrive at once.
    let mut reads = steady_reads(10.0, RATE, 8, 0.0, |_| 0.004);
    let stalled: Vec<Read> = reads.drain(250..437).collect();
    let resume = reads[250].arrival;
    reads.insert(
        250,
        Read {
            arrival: resume,
            first: stalled[0].first,
            count: stalled.iter().map(|r| r.count).sum(),
        },
    );
    let stamped = stamp(&reads);
    assert_eq!(stamped.len(), 5000);
    for d in steps(&stamped) {
        assert!((d - PERIOD).abs() < 1e-9, "step {d}");
    }
}

#[test]
fn an_uncounted_jump_is_resynchronised_after_the_set_number_of_windows() {
    // After 6 s every arrival is 1 s later than the sample count predicts
    // (samples lost without being counted, or the host clock was set).
    let jump = 1.0;
    assert!(jump > RESYNC_SECONDS);
    let reads = steady_reads(30.0, RATE, 8, 0.0, |k| {
        if k * 8 >= 3000 {
            0.004 + jump
        } else {
            0.004
        }
    });
    let stamped = stamp(&reads);
    assert!(steps(&stamped).iter().all(|d| *d >= 0.0));
    let after = 3000 + (f64::from(RESYNC_WINDOWS + 1) * WINDOW_SECONDS * RATE) as u64 + 8;
    for (i, time, arrival) in stamped.iter().filter(|s| s.0 >= after) {
        assert!(arrival - time < 0.020, "sample {i}: lag {}", arrival - time);
    }
    // Before the jump is confirmed, timestamps keep the old spacing.
    let (_, before, _) = stamped[3100];
    assert!((before - (3100.0 * PERIOD + 0.004)).abs() < 1e-3);
}

#[test]
fn timestamps_never_go_backwards_when_the_host_clock_steps_back() {
    let reads = steady_reads(10.0, RATE, 8, 100.0, |k| {
        if k >= 300 {
            0.004 - 0.5
        } else {
            0.004
        }
    });
    let stamped = stamp(&reads);
    assert!(steps(&stamped).iter().all(|d| *d >= 0.0));
    // After the held stretch, timestamps follow the new clock.
    let (_, time, arrival) = stamped.last().unwrap();
    assert!((arrival - time).abs() < 0.020);
}

#[test]
fn one_sample_at_a_time_matches_observe_then_timestamp() {
    let mut a = ClockState::default();
    let mut b = ClockState::default();
    for i in 0..2000u64 {
        let arrival = 10.0 + i as f64 * PERIOD + 0.002 * ((i % 5) as f64);
        let one = assign_timestamp(arrival, i, RATE, &mut a);
        b.observe(arrival, i, RATE);
        assert_eq!(one, b.timestamp(i, RATE));
    }
}

#[test]
fn nonsense_inputs_do_not_poison_the_clock() {
    let mut clock = ClockState::default();
    assert_eq!(clock.timestamp(10, RATE), 10.0 * PERIOD);
    let mut clock = ClockState::default();
    clock.observe(f64::NAN, 0, RATE);
    clock.observe(5.0, 0, 0.0);
    clock.observe(5.0, 0, f64::INFINITY);
    assert_eq!(clock.offset(), None);
    clock.observe(5.0, 0, RATE);
    clock.observe(f64::NAN, 1, RATE);
    assert_eq!(clock.offset(), Some(5.0));
    assert!(clock.timestamp(1, f64::NAN).is_finite());
}

/// Host clock readings near now: the magnitude the live code sees.
const EPOCH: f64 = 1.8e9;

#[test]
fn epoch_magnitude_clocks_keep_the_spacing_to_a_microsecond() {
    // As `jitter_is_removed`, at 1.8e9 s, where one f64 step is 2.4e-7 s.
    let reads = steady_reads(20.0, RATE, 8, EPOCH, |k| {
        0.003 + 0.012 * ((k * 7 % 16) as f64 / 15.0)
    });
    let stamped = stamp(&reads);
    let settled: Vec<_> = stamped.iter().filter(|s| s.0 >= 500).cloned().collect();
    for d in steps(&settled) {
        assert!((d - PERIOD).abs() < 1e-6, "step {d}");
    }
    for (i, time, arrival) in &settled {
        assert!(
            (time - (EPOCH + *i as f64 / RATE + 0.003)).abs() < 1e-6,
            "sample {i}"
        );
        assert!(*time <= arrival + 1e-6);
    }
}

#[test]
fn a_long_gap_between_reads_does_not_move_the_offset_up() {
    // The device digitises sample n at EPOCH + n / RATE throughout. Reads
    // deliver it 3 ms later, then 8 ms later from 6 s on. Nothing is read
    // from 10 s to 35 s (the program was stopped); the indices go on.
    // A window that spans the gap (more than 10 windows long) measures
    // nothing and is dropped: if it were closed, its 25 s length would let
    // the offset rise by up to MAX_DRIFT x 25 s = 12.5 ms in one step.
    let reads: Vec<Read> = steady_reads(
        40.0,
        RATE,
        50,
        EPOCH,
        |k| if k >= 60 { 0.008 } else { 0.003 },
    )
    .into_iter()
    .filter(|r| r.arrival < EPOCH + 10.0 || r.arrival > EPOCH + 35.0)
    .collect();
    let stamped = stamp(&reads);
    assert!(steps(&stamped).iter().all(|d| *d >= 0.0));
    let lag = |stamped: &[(u64, f64, f64)], at: u64| {
        let (i, time, _) = stamped.iter().find(|s| s.0 >= at).unwrap();
        time - (EPOCH + *i as f64 / RATE)
    };
    let before = lag(&stamped, 4900);
    let after = lag(&stamped, 17500);
    // Before the gap the offset has risen from 3 ms by MAX_DRIFT x 2 s per
    // window since the delay grew; the first read after the gap gets the same.
    assert!(
        before > 0.0035 && before < 0.0065,
        "before the gap {before}"
    );
    assert!(
        (after - before).abs() < 1e-6,
        "before {before}, after {after}"
    );
    // Later windows go on rising by at most MAX_DRIFT x their length.
    let later = lag(&stamped, 19500);
    assert!(
        later - after <= MAX_DRIFT * 2.0 * WINDOW_SECONDS + 1e-6,
        "{after} -> {later}"
    );
}

#[test]
fn a_forward_step_of_the_host_clock_is_taken_after_the_set_number_of_windows() {
    let step = 2.0;
    let reads = steady_reads(30.0, RATE, 8, EPOCH, |k| {
        if k * 8 >= 3000 {
            0.004 + step
        } else {
            0.004
        }
    });
    let stamped = stamp(&reads);
    let d = steps(&stamped);
    assert!(d.iter().all(|d| *d >= 0.0));
    // Exactly one step carries the clock change; all others are one period.
    let jumps: Vec<_> = d.iter().filter(|d| (**d - PERIOD).abs() > 1e-6).collect();
    assert_eq!(jumps.len(), 1, "{jumps:?}");
    assert!((jumps[0] - (step + PERIOD)).abs() < 1e-6, "{jumps:?}");
    // It is taken only after RESYNC_WINDOWS windows agree: at least that
    // many window lengths after the change, and at most one more.
    let at = d.iter().position(|d| (*d - PERIOD).abs() > 1e-6).unwrap() as u64 + 1;
    let windows = (at - 3000) as f64 / RATE / WINDOW_SECONDS;
    assert!(
        windows >= f64::from(RESYNC_WINDOWS) - 1.0 && windows <= f64::from(RESYNC_WINDOWS) + 1.0,
        "taken after {windows} windows"
    );
    // After the step the newest sample of each read is stamped at its
    // arrival again, as before it.
    let (_, time, arrival) = stamped.last().unwrap();
    assert!((arrival - time).abs() < 1e-6, "lag {}", arrival - time);
}
