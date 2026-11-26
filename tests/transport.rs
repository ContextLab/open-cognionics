//! Tests of the serial transport against a pseudo-terminal that plays bytes
//! recorded from a Quick-20 (see `tests/common/pty_device.rs`).
//!
//! No test here opens a real serial port: the dongle may be plugged in.
#![cfg(all(unix, feature = "serial"))]

mod common;

use std::fs::OpenOptions;
use std::io::{ErrorKind, Read};
use std::os::unix::fs::OpenOptionsExt;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use common::pty_device::{PtyDevice, BACKLOG_SECONDS, PACKET_LEN};
use common::temp::TempDir;
use common::{config_reply, garbage_stream, onhead_stream, REAL_RATE};
use open_cognionics::chunk::StreamEvent;
use open_cognionics::config::{extract_words, ConfigError, DeviceConfig};
use open_cognionics::legacy::Layout;
use open_cognionics::transport::{
    choose_headset, find_headsets, is_pseudo_terminal, relaxes_port_settings, select_headset,
    Headset, PortInfo, SinkId, TransportError, CONNECT_LIMIT, MAX_REQUESTS, RESEND_AFTER,
};

fn start_device() -> PtyDevice {
    PtyDevice::start(onhead_stream(), Some(config_reply()), REAL_RATE)
}

fn open(device: &PtyDevice) -> Headset {
    Headset::open(Some(&device.path())).expect("open the pty device")
}

/// Everything a subscriber receives during `period`.
fn collect_for(rx: &Receiver<StreamEvent>, period: Duration) -> Vec<StreamEvent> {
    let deadline = Instant::now() + period;
    let mut events = Vec::new();
    loop {
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return events;
        }
        match rx.recv_timeout(left) {
            Ok(event) => events.push(event),
            Err(RecvTimeoutError::Timeout) => return events,
            Err(RecvTimeoutError::Disconnected) => panic!("stream ended early: {events:?}"),
        }
    }
}

/// Asserts the events are chunks only, with contiguous indices; returns the
/// number of samples.
fn assert_contiguous_chunks(events: &[StreamEvent]) -> usize {
    let mut next = None;
    let mut samples = 0;
    for event in events {
        let StreamEvent::Chunk(chunk) = event else {
            panic!("expected only chunks, got {event:?}");
        };
        if let Some(next) = next {
            assert_eq!(chunk.index, next, "chunk indices are not contiguous");
        }
        assert_eq!(chunk.channels, 23);
        assert_eq!(chunk.counts.len(), chunk.samples * chunk.channels);
        next = Some(chunk.index + chunk.samples as u64);
        samples += chunk.samples;
    }
    samples
}

fn port_info(path: &str) -> PortInfo {
    PortInfo {
        path: path.to_string(),
        description: "Cognionics Quick-20 20CH".to_string(),
        serial_number: Some(path.to_string()),
    }
}

#[test]
fn pty_device_bytes_read_equal_bytes_played() {
    // Ten packets looped 2.5 times, so the loop and counter rewrite are covered.
    let stream = onhead_stream()[..10 * PACKET_LEN].to_vec();
    let device = PtyDevice::start(stream.clone(), Some(config_reply()), 1_000_000);
    let mut port = OpenOptions::new()
        .read(true)
        .custom_flags(nix::fcntl::OFlag::O_NONBLOCK.bits())
        .open(device.path())
        .unwrap();
    let want = 25 * PACKET_LEN;
    let mut got = Vec::new();
    let mut buf = [0u8; 4096];
    let deadline = Instant::now() + Duration::from_secs(10);
    while got.len() < want && Instant::now() < deadline {
        match port.read(&mut buf) {
            Ok(n) => got.extend_from_slice(&buf[..n]),
            Err(e) if e.kind() == ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(1))
            }
            Err(e) => panic!("read failed: {e}"),
        }
    }
    device.hang_up();
    assert!(got.len() >= want, "read only {} bytes", got.len());
    let played = device.played();
    assert_eq!(got[..], played[..got.len()]);
    for (i, packet) in got[..want].chunks(PACKET_LEN).enumerate() {
        let original = &stream[(i % 10) * PACKET_LEN..][..PACKET_LEN];
        assert_eq!(packet[0], original[0]);
        assert_eq!(packet[1], (i % 128) as u8, "counter of packet {i}");
        assert_eq!(packet[2..], original[2..]);
    }
    // Nothing was written to the device, so no reply was played.
    assert!(device.written().is_empty());
    assert!(device.played_after_config().is_none());
}

#[test]
fn open_reads_configuration_then_streams() {
    let device = start_device();
    let headset = open(&device);
    let recorded = std::fs::read("tests/fixtures/config_reply.bin").unwrap();
    let expected = DeviceConfig::from_words(&extract_words(&recorded).unwrap().words).unwrap();
    assert_eq!(*headset.config(), expected);
    assert_eq!(
        headset.config().layout,
        Layout {
            eeg: 20,
            ext: 0,
            acc: 3
        }
    );
    assert_eq!(headset.config().sample_rate, 500.0);
    assert_eq!(headset.config().labels[..3], ["F7", "Fp1", "Fp2"]);
    assert_eq!(headset.port(), device.path());

    let rx = headset.subscribe(1024);
    let events = collect_for(&rx, Duration::from_secs(2));
    // 2 s at 500 Hz is about 1000 samples; the bound only rules out a stall.
    let samples = assert_contiguous_chunks(&events);
    assert!(samples >= 500, "only {samples} samples in 2 s");
    assert!(headset.sample_index() >= samples as u64);
    headset.close();
    assert_eq!(device.written(), [0x14]);
}

#[test]
fn impedance_commands_write_exactly_one_byte_each() {
    let device = start_device();
    let headset = open(&device);
    headset.set_impedance_check(true).unwrap();
    headset.set_impedance_check(false).unwrap();
    headset.close();
    assert_eq!(device.written(), [0x14, 0x11, 0x12]);
}

#[test]
fn no_api_sequence_writes_0x13() {
    let device = start_device();
    let headset = open(&device);
    let _ = headset.config();
    let _ = headset.port();
    let _ = headset.sample_index();
    let rx = headset.subscribe(4);
    let calls = Arc::new(AtomicUsize::new(0));
    let counter = Arc::clone(&calls);
    let sink = headset.add_raw_sink(Box::new(
        move |_bytes: &[u8], _index: u64, _arrival: f64| {
            counter.fetch_add(1, Ordering::SeqCst);
        },
    ));
    headset.set_impedance_check(true).unwrap();
    let _ = collect_for(&rx, Duration::from_millis(200));
    headset.set_impedance_check(false).unwrap();
    headset.remove_raw_sink(sink);
    let after_removal = calls.load(Ordering::SeqCst);
    assert!(after_removal > 0, "the raw sink was never called");
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(calls.load(Ordering::SeqCst), after_removal);
    headset.close();
    // A second connection, closed by drop rather than close().
    drop(open(&device));
    let _ = find_headsets();

    let written = device.written();
    assert!(!written.contains(&0x13), "0x13 was written: {written:02x?}");
    assert!(
        written.iter().all(|b| [0x11, 0x12, 0x14].contains(b)),
        "unexpected byte written: {written:02x?}"
    );
    assert_eq!(written, [0x14, 0x11, 0x12, 0x14]);
}

#[test]
fn opens_when_first_request_is_ignored() {
    let device = PtyDevice::start_ignoring(onhead_stream(), Some(config_reply()), REAL_RATE, 1);
    let headset = open(&device);
    assert_eq!(
        headset.config().layout,
        Layout {
            eeg: 20,
            ext: 0,
            acc: 3
        }
    );
    assert_eq!(headset.config().sample_rate, 500.0);
    let rx = headset.subscribe(1024);
    let events = collect_for(&rx, Duration::from_secs(2));
    let samples = assert_contiguous_chunks(&events);
    assert!(samples >= 500, "only {samples} samples in 2 s");
    headset.close();
    assert_eq!(device.written(), [0x14, 0x14]);
}

#[test]
fn streaming_device_that_never_answers_gives_no_configuration() {
    let device = PtyDevice::start(onhead_stream(), None, REAL_RATE);
    let started = Instant::now();
    let result = Headset::open(Some(&device.path()));
    let elapsed = started.elapsed();
    match result {
        Err(TransportError::NoConfiguration {
            requests,
            at_limit: false,
        }) => assert_eq!(requests, MAX_REQUESTS),
        other => panic!("got {:?}", other.err()),
    }
    // 300 ms discard + MAX_REQUESTS x RESEND_AFTER = 6.3 s, with room for a
    // slow machine.
    assert!(
        elapsed >= RESEND_AFTER * MAX_REQUESTS as u32,
        "gave up after {elapsed:?}"
    );
    assert!(elapsed < Duration::from_secs(8), "took {elapsed:?}");
    let written = device.written();
    assert_eq!(written.len(), MAX_REQUESTS, "{written:02x?}");
    assert!(written.iter().all(|&b| b == 0x14), "{written:02x?}");
}

/// `Headset::open` on another thread, so a hang fails the test instead of
/// stalling it: the result and how long it took, or a panic once the open
/// has run for well past `CONNECT_LIMIT`.
fn open_within_limit(path: String) -> (Result<Headset, TransportError>, Duration) {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let started = Instant::now();
        let result = Headset::open(Some(&path));
        let _ = tx.send((result, started.elapsed()));
    });
    match rx.recv_timeout(CONNECT_LIMIT + Duration::from_secs(10)) {
        Ok(outcome) => outcome,
        Err(_) => panic!("Headset::open was still running after {CONNECT_LIMIT:?} + 10 s"),
    }
}

#[test]
fn open_after_30_s_of_unread_streaming_is_prompt_and_gives_recent_samples() {
    let device = start_device();
    std::thread::sleep(Duration::from_secs(30)); // the headset streams; nobody reads the port
    let (result, elapsed) = open_within_limit(device.path());
    let headset = result.expect("open after a backlog");
    // The first request was answered: about the 0.3 s discard, no resend.
    assert!(elapsed < RESEND_AFTER, "took {elapsed:?}");
    assert_eq!(device.written(), [0x14]);
    // Recent: when the reply was played the device was at most 0.1 s behind
    // its own clock; all but BACKLOG_SECONDS of the 30 s were lost unread and
    // that second was discarded before the request.
    let lag = device.lag_at_config().expect("a reply was played");
    assert!(lag <= 0.1, "the stream after the reply was {lag} s late");
    assert!(device.dropped_packets() as f64 >= (30.0 - BACKLOG_SECONDS - 1.0) * 500.0);
    let rx = headset.subscribe(1024);
    let events = collect_for(&rx, Duration::from_secs(2));
    // Continuous: no sample lost. Indices count from the reply; the reader
    // starts before `subscribe`, so the first chunk may begin a little later.
    let samples = assert_contiguous_chunks(&events);
    assert!(samples >= 800, "only {samples} samples in 2 s");
    match &events[0] {
        StreamEvent::Chunk(chunk) => assert!(chunk.index < 100, "{}", chunk.index),
        other => panic!("expected a chunk first, got {other:?}"),
    }
    headset.close();
}

#[test]
fn a_reply_that_never_ends_gives_no_configuration_within_the_limit() {
    let mut reply = config_reply();
    reply.truncate(reply.len() - 4); // no end word
    let device = PtyDevice::start(onhead_stream(), Some(reply), REAL_RATE);
    let (result, elapsed) = open_within_limit(device.path());
    // The reply to the first request starts and never ends: given up 5 s
    // after that request (about 5.3 s), before the overall limit.
    assert!(
        matches!(
            result,
            Err(TransportError::NoConfiguration {
                requests: 1,
                at_limit: false
            })
        ),
        "got {:?}",
        result.err()
    );
    assert!(elapsed < CONNECT_LIMIT, "took {elapsed:?}");
    let written = device.written();
    assert!(!written.is_empty() && written.iter().all(|&b| b == 0x14));
}

#[test]
fn a_reply_that_starts_late_and_never_ends_is_cut_off_at_the_limit() {
    // The first three requests go unanswered; the fourth (about 4.8 s in)
    // gets a reply that starts and never ends. Its own 5 s would run to about
    // 9.8 s, so the 8 s limit ends the wait.
    let mut reply = config_reply();
    reply.truncate(reply.len() - 4); // no end word
    let device = PtyDevice::start_ignoring(onhead_stream(), Some(reply), REAL_RATE, 3);
    let (result, elapsed) = open_within_limit(device.path());
    let error = result.err().expect("no configuration");
    assert_eq!(
        error,
        TransportError::NoConfiguration {
            requests: MAX_REQUESTS,
            at_limit: true
        }
    );
    assert!(
        error.to_string().contains("within the 8 s limit"),
        "{error}"
    );
    // At the limit, give or take one read (20 ms) and scheduling: 0.5 s.
    assert!(elapsed >= CONNECT_LIMIT, "took {elapsed:?}");
    assert!(
        elapsed < CONNECT_LIMIT + Duration::from_millis(500),
        "took {elapsed:?}"
    );
    assert_eq!(device.written(), [0x14; MAX_REQUESTS]);
}

#[test]
fn a_device_streaming_garbage_gives_no_configuration_within_the_limit() {
    for rate in [REAL_RATE, 2_000_000] {
        let device = PtyDevice::start(garbage_stream(), None, rate);
        let (result, elapsed) = open_within_limit(device.path());
        assert!(
            matches!(result, Err(TransportError::NoConfiguration { .. })),
            "at {rate} B/s got {:?}",
            result.err()
        );
        assert!(elapsed < CONNECT_LIMIT, "at {rate} B/s took {elapsed:?}");
        let written = device.written();
        assert!(!written.is_empty() && written.iter().all(|&b| b == 0x14));
    }
}

#[test]
fn a_device_that_never_takes_the_request_gives_no_configuration_within_the_limit() {
    // It streams but never reads what is written to it, as when the dongle's
    // flow control holds the computer's output.
    let device = PtyDevice::start_deaf(onhead_stream(), Some(config_reply()), REAL_RATE);
    let (result, elapsed) = open_within_limit(device.path());
    assert!(
        matches!(result, Err(TransportError::NoConfiguration { .. })),
        "got {:?}",
        result.err()
    );
    assert!(elapsed < CONNECT_LIMIT, "took {elapsed:?}");
    // What the device never took is discarded on giving up.
    assert!(device.written().iter().all(|&b| b == 0x14));
}

#[test]
fn silent_device_gives_no_data() {
    let device = PtyDevice::silent();
    let started = Instant::now();
    let result = Headset::open(Some(&device.path()));
    let elapsed = started.elapsed();
    assert!(
        matches!(result, Err(TransportError::NoData)),
        "got {:?}",
        result.err()
    );
    // 300 ms discard + RESEND_AFTER after the one request: about 1.8 s.
    assert!(elapsed >= RESEND_AFTER, "gave up after {elapsed:?}");
    assert!(elapsed < Duration::from_secs(4), "took {elapsed:?}");
    assert_eq!(device.written(), [0x14]);
}

#[test]
fn answered_first_request_sends_one_byte() {
    let device = start_device();
    let headset = open(&device);
    std::thread::sleep(Duration::from_millis(500));
    headset.close();
    assert_eq!(device.written(), [0x14]);
}

#[test]
fn error_messages_say_what_happened() {
    assert_eq!(
        TransportError::NoData.to_string(),
        "nothing received from the headset: is it switched on and in range?"
    );
    assert_eq!(
        TransportError::NoConfiguration {
            requests: 4,
            at_limit: false
        }
        .to_string(),
        "the headset is streaming but did not answer 4 configuration requests"
    );
    assert_eq!(
        TransportError::NoConfiguration {
            requests: 4,
            at_limit: true
        }
        .to_string(),
        "the headset is streaming but no complete configuration arrived within the 8 s limit \
         (4 configuration requests sent)"
    );
}

#[test]
fn subscribing_after_a_hang_up_gets_disconnected_at_once() {
    let device = start_device();
    let headset = open(&device);
    let rx = headset.subscribe(4096);
    rx.recv_timeout(Duration::from_secs(5))
        .expect("a first chunk");
    device.hang_up();
    // Wait until the reader has stopped: the first channel closes.
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let left = deadline.saturating_duration_since(Instant::now());
        match rx.recv_timeout(left) {
            Ok(_) => {}
            Err(RecvTimeoutError::Disconnected) => break,
            Err(RecvTimeoutError::Timeout) => panic!("the reader did not stop"),
        }
    }
    let late = headset.subscribe(1);
    match late.recv_timeout(Duration::from_millis(100)) {
        Ok(StreamEvent::Disconnected(reason)) => assert!(!reason.is_empty()),
        other => panic!("expected Disconnected at once, got {other:?}"),
    }
    assert_eq!(
        late.recv_timeout(Duration::from_millis(100)),
        Err(RecvTimeoutError::Disconnected)
    );
}

#[test]
fn hang_up_delivers_disconnected_and_does_not_panic() {
    let device = start_device();
    let headset = open(&device);
    let rx = headset.subscribe(4096);
    let first = rx
        .recv_timeout(Duration::from_secs(5))
        .expect("a first chunk");
    assert!(matches!(first, StreamEvent::Chunk(_)));
    device.hang_up();

    let deadline = Instant::now() + Duration::from_secs(5);
    let reason = loop {
        let left = deadline.saturating_duration_since(Instant::now());
        match rx.recv_timeout(left) {
            Ok(StreamEvent::Disconnected(reason)) => break reason,
            Ok(StreamEvent::Chunk(_)) => {}
            Ok(other) => panic!("unexpected event {other:?}"),
            Err(e) => panic!("no Disconnected event: {e:?}"),
        }
    };
    assert!(!reason.is_empty());
    // The reader thread has finished, so the channel closes.
    assert_eq!(
        rx.recv_timeout(Duration::from_secs(5)),
        Err(RecvTimeoutError::Disconnected)
    );
    assert!(headset.sample_index() > 0);
    assert!(matches!(
        headset.set_impedance_check(true),
        Err(TransportError::Disconnected(_))
    ));
    headset.close();
}

#[test]
fn stalled_subscriber_loses_chunks_but_raw_sink_gets_every_byte() {
    let device = start_device();
    let headset = open(&device);
    let stalled = headset.subscribe(4);
    let raw = Arc::new(Mutex::new(Vec::<u8>::new()));
    let last_index = Arc::new(Mutex::new(None::<u64>));
    let (raw_in, index_in) = (Arc::clone(&raw), Arc::clone(&last_index));
    headset.add_raw_sink(Box::new(move |bytes: &[u8], index: u64, _arrival: f64| {
        raw_in.lock().unwrap().extend_from_slice(bytes);
        *index_in.lock().unwrap() = Some(index);
    }));
    let reader = headset.subscribe(1024);
    let events = collect_for(&reader, Duration::from_secs(2));
    let samples = assert_contiguous_chunks(&events);
    assert!(samples >= 500, "only {samples} samples in 2 s");

    // The stalled subscriber is full, not removed: once emptied it fills again.
    let queued: Vec<_> = stalled.try_iter().collect();
    assert_eq!(queued.len(), 4);
    assert!(stalled.recv_timeout(Duration::from_secs(2)).is_ok());
    headset.close();

    let raw = raw.lock().unwrap().clone();
    let last_index = last_index.lock().unwrap().expect("the raw sink was called");
    let played = device.played_after_config().expect("a configuration reply");
    assert!(raw.len() >= 37_500, "only {} raw bytes", raw.len());
    // Find where the sink's bytes start in what was played after the reply;
    // the rewritten counters make any 10 packets unique in a 10 s stream.
    let probe = &raw[..10 * PACKET_LEN];
    let start = played
        .windows(probe.len())
        .position(|w| w == probe)
        .expect("raw bytes are not from the played stream");
    assert_eq!(raw[..], played[start..start + raw.len()]);
    // Sample index 0 is the first packet after the reply.
    assert_eq!(last_index, ((start + raw.len()) / PACKET_LEN) as u64);
}

#[test]
fn open_reports_a_missing_path() {
    let result = Headset::open(Some("/dev/open-cognionics-no-such-port"));
    assert!(
        matches!(result, Err(TransportError::Open(_))),
        "got {:?}",
        result.err()
    );
}

#[test]
fn open_none_with_no_headset_is_no_headset_error() {
    assert_eq!(
        select_headset(vec![]),
        Err(TransportError::NoHeadset { others: None })
    );
}

#[test]
fn one_headset_is_selected() {
    let only = port_info("/dev/cu.usbserial-A");
    assert_eq!(select_headset(vec![only.clone()]), Ok(only));
}

#[test]
fn several_headsets_are_an_error_listing_them() {
    let found = vec![
        port_info("/dev/cu.usbserial-A"),
        port_info("/dev/cu.usbserial-B"),
    ];
    assert_eq!(
        select_headset(found.clone()),
        Err(TransportError::SeveralHeadsets(found))
    );
}

#[test]
fn find_headsets_lists_only_headsets_once_each() {
    // Lists ports without opening any. How many there are depends on the machine.
    let found = find_headsets();
    for port in &found {
        assert!(
            port.description.contains("Cognionics") || port.description.contains("Quick"),
            "{port:?}"
        );
        assert!(!port.path.starts_with("/dev/tty."), "{port:?}");
    }
}

#[test]
fn a_sink_can_remove_itself_and_add_another_from_its_callback() {
    let device = start_device();
    let headset = Arc::new(open(&device));
    let weak = Arc::downgrade(&headset);
    let own_id = Arc::new(Mutex::new(None::<SinkId>));
    let first_calls = Arc::new(AtomicUsize::new(0));
    let second_calls = Arc::new(AtomicUsize::new(0));
    let (own, first, second) = (
        Arc::clone(&own_id),
        Arc::clone(&first_calls),
        Arc::clone(&second_calls),
    );
    let id = headset.add_raw_sink(Box::new(
        move |_bytes: &[u8], _index: u64, _arrival: f64| {
            first.fetch_add(1, Ordering::SeqCst);
            let Some(headset) = weak.upgrade() else {
                return;
            };
            let id = own.lock().unwrap().take();
            if let Some(id) = id {
                headset.remove_raw_sink(id);
                let second = Arc::clone(&second);
                headset.add_raw_sink(Box::new(
                    move |_bytes: &[u8], _index: u64, _arrival: f64| {
                        second.fetch_add(1, Ordering::SeqCst);
                    },
                ));
            }
        },
    ));
    *own_id.lock().unwrap() = Some(id);

    let deadline = Instant::now() + Duration::from_secs(3);
    while second_calls.load(Ordering::SeqCst) == 0 && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(5));
    }
    if second_calls.load(Ordering::SeqCst) == 0 {
        // The reader is stuck; leak the headset so the test fails instead of hanging.
        std::mem::forget(headset);
        panic!("the sink added from a callback was never called");
    }
    let first_after = first_calls.load(Ordering::SeqCst);
    std::thread::sleep(Duration::from_millis(100));
    assert_eq!(first_calls.load(Ordering::SeqCst), first_after);
    assert!(second_calls.load(Ordering::SeqCst) > 1);
    drop(headset);
}

#[test]
fn a_sink_can_drop_the_headset_from_its_callback() {
    let device = start_device();
    let headset = open(&device);
    let rx = headset.subscribe(4096);
    let slot = Arc::new(Mutex::new(None::<Headset>));
    let dropped = Arc::new(AtomicBool::new(false));
    let (s, d) = (Arc::clone(&slot), Arc::clone(&dropped));
    headset.add_raw_sink(Box::new(
        move |_bytes: &[u8], _index: u64, _arrival: f64| {
            let taken = s.lock().unwrap().take();
            if let Some(headset) = taken {
                drop(headset);
                d.store(true, Ordering::SeqCst);
            }
        },
    ));
    *slot.lock().unwrap() = Some(headset);

    // Closing is not a disconnection: the channel just ends once the reader stops.
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let left = deadline.saturating_duration_since(Instant::now());
        match rx.recv_timeout(left) {
            Ok(StreamEvent::Chunk(_)) => {}
            Ok(other) => panic!("unexpected event {other:?}"),
            Err(RecvTimeoutError::Disconnected) => break,
            Err(RecvTimeoutError::Timeout) => panic!("the reader did not stop"),
        }
    }
    assert!(
        dropped.load(Ordering::SeqCst),
        "dropping the headset panicked"
    );
}

#[test]
fn a_panicking_sink_ends_the_stream_instead_of_hanging_it() {
    let device = start_device();
    let headset = open(&device);
    let rx = headset.subscribe(4096);
    headset.add_raw_sink(Box::new(|_bytes: &[u8], _index: u64, _arrival: f64| {
        panic!("raw sink failure staged by the test")
    }));
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let left = deadline.saturating_duration_since(Instant::now());
        match rx.recv_timeout(left) {
            Ok(StreamEvent::Chunk(_)) => {}
            Ok(StreamEvent::Disconnected(reason)) => {
                assert!(reason.contains("panicked"), "{reason}");
                break;
            }
            Ok(other) => panic!("unexpected event {other:?}"),
            Err(RecvTimeoutError::Disconnected) => break,
            Err(RecvTimeoutError::Timeout) => {
                std::mem::forget(headset);
                panic!("subscriber left waiting after a sink panicked");
            }
        }
    }
    // The headset still works as an object afterwards.
    let id = headset.add_raw_sink(Box::new(|_bytes: &[u8], _index: u64, _arrival: f64| {}));
    headset.remove_raw_sink(id);
    assert!(matches!(
        headset.set_impedance_check(true),
        Err(TransportError::Disconnected(_))
    ));
    headset.close();
    assert_eq!(device.written(), [0x14]);
}

#[test]
fn pseudo_terminal_names_are_recognised_positively() {
    for pty in ["/dev/ttys006", "/dev/ttys0", "/dev/pts/3", "/dev/pts/12"] {
        assert!(is_pseudo_terminal(std::path::Path::new(pty)), "{pty}");
    }
    for other in [
        "/dev/cu.usbserial-A0TEST01",
        "/dev/tty.usbserial-A0TEST01",
        "/dev/serial/by-id/usb-FTDI_Cognionics_A0TEST01-if00-port0",
        "/dev/ttyUSB0",
        "/dev/ttyS0",
        "/dev/ttys",
        "/dev/ttysa",
        "/dev/tty",
        "/dev/pts/ptmx",
        "/dev/pts/",
        "ttys006",
    ] {
        assert!(!is_pseudo_terminal(std::path::Path::new(other)), "{other}");
    }
}

#[test]
fn port_settings_are_relaxed_only_for_paths_that_resolve_to_a_pseudo_terminal() {
    let device = PtyDevice::start(onhead_stream(), None, REAL_RATE);
    let dir = TempDir::new("relax-test");
    let link_to_pty = dir.join("usb-FTDI_by-id-link-to-pty");
    std::os::unix::fs::symlink(device.path(), &link_to_pty).unwrap();
    let plain = dir.join("plain-file");
    std::fs::write(&plain, b"").unwrap();
    let link_to_plain = dir.join("ttys999");
    std::os::unix::fs::symlink(&plain, &link_to_plain).unwrap();
    // A dangling link named like a dongle: nothing behind it is touched.
    let dangling = dir.join("usb-dangling");
    std::os::unix::fs::symlink("/dev/cu.usbserial-OC-TEST-NONE", &dangling).unwrap();

    let relaxed = |p: &std::path::Path| relaxes_port_settings(p.to_str().unwrap());
    assert!(relaxes_port_settings(&device.path()));
    assert!(relaxed(&link_to_pty));
    assert!(!relaxed(&plain));
    assert!(!relaxed(&link_to_plain));
    assert!(!relaxed(&dangling));
    assert!(!relaxes_port_settings("/dev/null"));
    assert!(!relaxes_port_settings("/dev/open-cognionics-no-such-port"));
}

fn usb_port(path: &str, vid: u16, pid: u16, product: &str) -> serialport::SerialPortInfo {
    serialport::SerialPortInfo {
        port_name: path.to_string(),
        port_type: serialport::SerialPortType::UsbPort(serialport::UsbPortInfo {
            vid,
            pid,
            serial_number: Some("TEST0001".to_string()),
            manufacturer: Some("FTDI".to_string()),
            product: Some(product.to_string()),
        }),
    }
}

#[test]
fn no_headset_lists_the_other_serial_ports_seen() {
    // Port descriptions built here; nothing is listed from or opened on the machine.
    let others = vec![
        serialport::SerialPortInfo {
            port_name: "/dev/cu.Bluetooth-Incoming-Port".to_string(),
            port_type: serialport::SerialPortType::Unknown,
        },
        usb_port("/dev/cu.usbmodem1101", 0x2341, 0x0043, "Arduino Uno"),
        usb_port("/dev/tty.usbmodem1101", 0x2341, 0x0043, "Arduino Uno"),
    ];
    let err = choose_headset(&others).unwrap_err();
    let text = err.to_string();
    assert!(matches!(err, TransportError::NoHeadset { .. }), "{err:?}");
    assert!(text.contains("/dev/cu.Bluetooth-Incoming-Port"), "{text}");
    assert!(text.contains("/dev/cu.usbmodem1101"), "{text}");
    // The tty twin of a listed cu port is not listed again.
    assert!(!text.contains("/dev/tty.usbmodem1101"), "{text}");

    let err = choose_headset(&[]).unwrap_err();
    assert!(err.to_string().contains("no other serial port"), "{err}");

    // A headset among them is chosen; with two, both are named.
    let headset = usb_port(
        "/dev/cu.usbserial-TEST0001",
        0x0403,
        0x6001,
        "Cognionics Quick-20 20CH",
    );
    let mut ports = others.clone();
    ports.push(headset.clone());
    assert_eq!(
        choose_headset(&ports).unwrap().path,
        "/dev/cu.usbserial-TEST0001"
    );
    let mut second = headset;
    second.port_name = "/dev/cu.usbserial-TEST0002".to_string();
    ports.push(second);
    assert!(matches!(
        choose_headset(&ports),
        Err(TransportError::SeveralHeadsets(found)) if found.len() == 2
    ));
}

#[test]
fn a_headset_in_the_compressed_format_is_refused_at_open() {
    // The recorded reply with the data-mode word (W[2 * S]) set to 0.
    let mut reply = config_reply();
    let s = u32::from_be_bytes(reply[4 + 4 * 2..4 + 4 * 3].try_into().unwrap()) as usize;
    let at = 4 + 4 * (2 * s);
    reply[at..at + 4].copy_from_slice(&0u32.to_be_bytes());
    let device = PtyDevice::start(onhead_stream(), Some(reply), REAL_RATE);
    let result = Headset::open(Some(&device.path()));
    match result {
        Err(TransportError::Config(ConfigError::CompressedFormat)) => {}
        other => panic!("got {:?}", other.err()),
    }
    assert_eq!(device.written(), [0x14]);
}

#[test]
fn raw_sinks_get_the_host_time_of_each_read() {
    let device = start_device();
    let before = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();
    let headset = open(&device);
    let arrivals = Arc::new(Mutex::new(Vec::<f64>::new()));
    let sink_arrivals = Arc::clone(&arrivals);
    headset.add_raw_sink(Box::new(move |_bytes: &[u8], _index: u64, arrival: f64| {
        sink_arrivals.lock().unwrap().push(arrival);
    }));
    std::thread::sleep(Duration::from_millis(500));
    headset.close();
    let after = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs_f64();
    let arrivals = arrivals.lock().unwrap().clone();
    assert!(arrivals.len() > 5, "{} reads", arrivals.len());
    assert!(arrivals.windows(2).all(|w| w[0] <= w[1]));
    assert!(arrivals[0] >= before && *arrivals.last().unwrap() <= after);
}
