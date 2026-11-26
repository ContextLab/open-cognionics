//! The session server (`src/bin/serve.rs`) end to end: the real binary reads
//! a pseudo-terminal that plays bytes recorded from a Quick-20 (see
//! `tests/common/pty_device.rs`) and a real websocket client talks to it.
//!
//! No test here opens a real serial port: the dongle may be plugged in.
#![cfg(all(unix, feature = "server"))]

#[allow(dead_code)] // not every test uses every helper
mod common;

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicU16, AtomicU32, Ordering};
use std::time::{Duration, Instant};

use common::bdf::{all_labels, read_bdf, replay_rows};
use common::pty_device::PtyDevice;
use common::{config_reply, onhead_stream, REAL_RATE};
use nix::sys::signal::{kill, Signal};
use nix::unistd::Pid;
use open_cognionics::record::SESSION_FILES;
use serde_json::Value;
use tungstenite::client::IntoClientRequest;
use tungstenite::stream::MaybeTlsStream;
use tungstenite::{Message, WebSocket};

/// A running server. Dropping it kills the process, waits for it and removes
/// its output directory, so nothing outlives a failed assertion.
struct Server {
    child: Child,
    http_port: u16,
    out: PathBuf,
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.out);
    }
}

impl Server {
    fn signal(&self, signal: Signal) {
        kill(Pid::from_raw(self.child.id() as i32), signal).unwrap();
    }

    fn wait_exit(&mut self, within: Duration) -> ExitStatus {
        let deadline = Instant::now() + within;
        while Instant::now() < deadline {
            if let Some(status) = self.child.try_wait().unwrap() {
                return status;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        panic!("the server did not exit within {within:?}");
    }
}

/// Two consecutive free ports, never handed out twice to this test process
/// (tests run in parallel and the ports are only bound by the server later).
fn free_ports() -> u16 {
    static NEXT: AtomicU16 = AtomicU16::new(0);
    let base = 20_000 + (std::process::id() % 400) as u16 * 100;
    loop {
        let port = base + NEXT.fetch_add(2, Ordering::SeqCst);
        assert!(port < 60_000, "no free ports");
        if TcpListener::bind(("127.0.0.1", port)).is_ok()
            && TcpListener::bind(("127.0.0.1", port + 1)).is_ok()
        {
            return port;
        }
    }
}

fn start_server(port_path: &str) -> Server {
    static COUNT: AtomicU32 = AtomicU32::new(0);
    let out = std::env::temp_dir().join(format!(
        "open-cognionics-serve-{}-{}",
        std::process::id(),
        COUNT.fetch_add(1, Ordering::SeqCst)
    ));
    let _ = std::fs::remove_dir_all(&out);
    start_server_writing_to(port_path, out)
}

fn start_server_writing_to(port_path: &str, out: PathBuf) -> Server {
    let http_port = free_ports();
    let child = Command::new(env!("CARGO_BIN_EXE_open-cognionics"))
        .arg(port_path)
        .arg(http_port.to_string())
        .env("OPEN_COGNIONICS_OUT", &out)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("start the server");
    Server {
        child,
        http_port,
        out,
    }
}

type Client = WebSocket<MaybeTlsStream<TcpStream>>;

fn connect_with_origin(server: &Server, origin: &str) -> tungstenite::Result<Client> {
    let url = format!("ws://127.0.0.1:{}", server.http_port + 1);
    let mut request = url.into_client_request().unwrap();
    request
        .headers_mut()
        .insert("Origin", origin.parse().unwrap());
    let (socket, _) = tungstenite::connect(request)?;
    if let MaybeTlsStream::Plain(stream) = socket.get_ref() {
        stream
            .set_read_timeout(Some(Duration::from_millis(200)))
            .unwrap();
    }
    Ok(socket)
}

/// Connect as the page does, waiting for the server to come up.
fn connect(server: &Server) -> Client {
    let origin = format!("http://127.0.0.1:{}", server.http_port);
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        match connect_with_origin(server, &origin) {
            Ok(client) => return client,
            Err(e) if Instant::now() > deadline => panic!("could not connect: {e}"),
            Err(_) => std::thread::sleep(Duration::from_millis(100)),
        }
    }
}

/// The next message from the server, or `None` after a quiet moment.
fn next_message(client: &mut Client) -> Option<Value> {
    match client.read() {
        Ok(Message::Text(text)) => Some(serde_json::from_str(text.as_str()).unwrap()),
        Ok(_) => None,
        Err(tungstenite::Error::Io(e))
            if matches!(
                e.kind(),
                std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
            ) =>
        {
            None
        }
        Err(e) => panic!("websocket failed: {e}"),
    }
}

/// Read until a message of type `t` arrives (within 30 s); returns it.
fn wait_for(client: &mut Client, t: &str) -> Value {
    let deadline = Instant::now() + Duration::from_secs(30);
    while Instant::now() < deadline {
        if let Some(m) = next_message(client) {
            if m["t"] == t {
                return m;
            }
        }
    }
    panic!("no `{t}` message within 30 s");
}

/// Read `error` messages until one contains `text`.
fn wait_for_error(client: &mut Client, text: &str, kind: &str) -> Value {
    let deadline = Instant::now() + Duration::from_secs(30);
    while Instant::now() < deadline {
        let m = wait_for(client, "error");
        if m["msg"].as_str().unwrap().contains(text) {
            assert_eq!(m["kind"], kind, "{m}");
            return m;
        }
    }
    panic!("no error containing `{text}` within 30 s");
}

fn wait_until(what: &str, mut done: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(30);
    while Instant::now() < deadline {
        if done() {
            return;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    panic!("timed out waiting for {what}");
}

fn sessions(out: &Path) -> Vec<PathBuf> {
    let mut found: Vec<_> = std::fs::read_dir(out)
        .map(|d| d.flatten().map(|e| e.path()).collect())
        .unwrap_or_default();
    found.sort();
    found
}

fn send(client: &mut Client, text: &str) {
    client.send(Message::text(text)).unwrap();
}

/// Wait for hello and `rows` rows of data.
fn stream_rows(client: &mut Client, rows: usize) -> Value {
    let hello = wait_for(client, "hello");
    let mut seen = 0;
    while seen < rows {
        let d = wait_for(client, "d");
        seen += d["x"].as_array().unwrap().len();
    }
    hello
}

/// The BDF's header record count, which must be the real one: not -1, and
/// consistent with the file length.
fn check_bdf(path: &Path) {
    let bytes = std::fs::read(path).unwrap();
    let field = |from: usize, to: usize| -> i64 {
        std::str::from_utf8(&bytes[from..to])
            .unwrap()
            .trim()
            .parse()
            .unwrap()
    };
    let records = field(236, 244);
    assert!(records > 0, "record count {records}");
    let signals = field(252, 256) as usize;
    assert_eq!(field(184, 192) as usize, 256 * (signals + 1));
    let per_record: i64 = (0..signals)
        .map(|i| field(256 + signals * 216 + i * 8, 256 + signals * 216 + i * 8 + 8))
        .sum();
    assert_eq!(
        bytes.len() as i64,
        256 * (signals as i64 + 1) + records * per_record * 3,
        "file length does not match {records} records"
    );
}

#[test]
fn streams_records_and_finishes_when_the_link_drops() {
    let device = PtyDevice::start(onhead_stream(), Some(config_reply()), REAL_RATE);
    let server = start_server(&device.path());
    let mut client = connect(&server);

    let hello = wait_for(&mut client, "hello");
    assert_eq!(hello["fs"], 500.0);
    let labels = hello["labels"].as_array().unwrap();
    assert_eq!(labels.len(), 20);
    assert_eq!(labels[1], "Fp1");

    let mut rows = 0;
    let mut last_index = None;
    while rows < 100 {
        let d = wait_for(&mut client, "d");
        for row in d["x"].as_array().unwrap() {
            assert_eq!(row.as_array().unwrap().len(), 20);
            rows += 1;
        }
        assert_eq!(d["acc"].as_array().unwrap().len(), 3);
        assert!(d["bat"].as_f64().unwrap() > 0.0);
        if let Some(previous) = last_index {
            assert!(d["i"].as_u64().unwrap() > previous);
        }
        last_index = d["i"].as_u64();
    }

    send(&mut client, r#"{"cmd":"mark","label":"test_mark"}"#);
    send(&mut client, r#"{"cmd":"impedance","on":true}"#);
    send(&mut client, r#"{"cmd":"impedance","on":false}"#);
    wait_until("the impedance commands to reach the device", || {
        let written = device.written();
        written.contains(&0x11) && written.contains(&0x12)
    });
    // Let the marker reach the file, then pull the plug.
    let dir = sessions(&server.out).pop().expect("a session directory");
    wait_until("the marker", || {
        std::fs::read_to_string(dir.join("markers.jsonl"))
            .is_ok_and(|text| text.contains("test_mark") && text.contains("sent_impedance_off"))
    });
    device.hang_up();

    let error = wait_for_error(&mut client, "lost the headset", "link");
    assert!(error["msg"].as_str().unwrap().contains("lost the headset"));

    wait_until("meta.json to be finished", || {
        std::fs::read_to_string(dir.join("meta.json"))
            .ok()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .is_some_and(|meta| meta["finished"] == true)
    });
    let meta: Value =
        serde_json::from_str(&std::fs::read_to_string(dir.join("meta.json")).unwrap()).unwrap();
    assert_eq!(meta["participant"], "unknown");
    for name in ["raw.bin", "markers.jsonl", "clock.jsonl", "recording.bdf"] {
        assert!(dir.join(name).metadata().unwrap().len() > 0, "{name}");
    }
    check_bdf(&dir.join("recording.bdf"));
    let markers = std::fs::read_to_string(dir.join("markers.jsonl")).unwrap();
    assert!(markers.contains("stream_start"));
    assert!(markers.contains("test_mark"));
    // Only 0x11, 0x12 and 0x14 are ever written; never 0x13.
    assert!(device
        .written()
        .iter()
        .all(|b| [0x11, 0x12, 0x14].contains(b)));

    // A mark with no recording running is refused, and the sender is told.
    send(&mut client, r#"{"cmd":"mark","label":"late_mark"}"#);
    let refusal = wait_for_error(&mut client, "was not saved", "note");
    assert!(refusal["msg"].as_str().unwrap().contains("late_mark"));
    assert!(refusal["msg"]
        .as_str()
        .unwrap()
        .contains("no recording is running"));
    assert!(!std::fs::read_to_string(dir.join("markers.jsonl"))
        .unwrap()
        .contains("late_mark"));

    // A client that connects while the headset is away is told at once.
    let mut late = connect(&server);
    let first = first_message(&mut late);
    assert_eq!(first["t"], "error", "{first}");
}

fn finishes_on_signal(signal: Signal) {
    let device = PtyDevice::start(onhead_stream(), Some(config_reply()), REAL_RATE);
    let mut server = start_server(&device.path());
    let mut client = connect(&server);
    stream_rows(&mut client, 100);
    std::thread::sleep(Duration::from_secs(2));
    let dir = sessions(&server.out).pop().expect("a session directory");

    server.signal(signal);
    let status = server.wait_exit(Duration::from_secs(10));
    assert!(status.success(), "{status:?}");

    let meta: Value =
        serde_json::from_str(&std::fs::read_to_string(dir.join("meta.json")).unwrap()).unwrap();
    assert_eq!(meta["finished"], true);
    check_bdf(&dir.join("recording.bdf"));
    assert!(dir.join("raw.bin").metadata().unwrap().len() > 0);
}

#[test]
fn sigint_finishes_the_recording_and_exits_cleanly() {
    finishes_on_signal(Signal::SIGINT);
}

#[test]
fn sigterm_finishes_the_recording_and_exits_cleanly() {
    finishes_on_signal(Signal::SIGTERM);
}

#[test]
fn refuses_a_websocket_from_another_origin() {
    let device = PtyDevice::start(onhead_stream(), Some(config_reply()), REAL_RATE);
    let server = start_server(&device.path());
    // Wait until the right origin works, then try the wrong one.
    drop(connect(&server));
    assert!(connect_with_origin(&server, "http://example.com").is_err());
}

#[test]
fn reports_a_missing_headset_once_instead_of_stopping_or_repeating() {
    let server = start_server("/dev/does-not-exist");
    let mut client = connect(&server);
    // The first message may be the "connecting" notice; the failure follows.
    let error = wait_for_error(&mut client, "could not", "link");
    assert!(
        error["msg"].as_str().unwrap().contains("No such file"),
        "{error}"
    );
    // The server retries every 3 s but says the same thing only once a minute.
    let until = Instant::now() + Duration::from_secs(7);
    while Instant::now() < until {
        if let Some(m) = next_message(&mut client) {
            panic!("repeated message: {m}");
        }
    }
    assert!(sessions(&server.out).is_empty());
    // A client that connects now is told the current state at once.
    let mut late = connect(&server);
    let first = first_message(&mut late);
    assert_eq!(first["t"], "error");
    assert!(first["msg"].as_str().unwrap().contains("could not"));
}

fn get(server: &Server, path: &str) -> String {
    let mut stream = TcpStream::connect(("127.0.0.1", server.http_port)).unwrap();
    write!(
        stream,
        "GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
    )
    .unwrap();
    let mut text = String::new();
    stream.read_to_string(&mut text).unwrap();
    text
}

#[test]
fn serves_both_pages() {
    let server = start_server("/dev/does-not-exist");
    connect(&server); // the servers are up once the websocket answers
    for (path, title) in [("/", "Quick-20 session"), ("/order", "Channel order check")] {
        let response = get(&server, path);
        assert!(
            response.starts_with("HTTP/1.1 200"),
            "{path}: {response:.80}"
        );
        assert!(
            response.contains(&format!("<title>{title}</title>")),
            "{path}"
        );
    }
}

/// A file or directory removed when dropped, even if the test panics.
struct Scratch(PathBuf);

impl Scratch {
    fn new(name: &str) -> Scratch {
        let path = std::env::temp_dir().join(format!(
            "open-cognionics-{name}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .subsec_nanos()
        ));
        let _ = std::fs::remove_file(&path);
        Scratch(path)
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[test]
fn keeps_streaming_and_says_so_when_the_recording_cannot_start() {
    let device = PtyDevice::start(onhead_stream(), Some(config_reply()), REAL_RATE);
    // A session directory cannot be created below a regular file.
    let blocker = Scratch::new("blocker");
    std::fs::write(&blocker.0, b"not a directory").unwrap();
    let server = start_server_writing_to(&device.path(), blocker.0.join("sessions"));
    let mut client = connect(&server);
    let hello = wait_for(&mut client, "hello");
    assert_eq!(hello["labels"].as_array().unwrap().len(), 20);
    let error = wait_for_error(&mut client, "could not start the recording", "note");
    assert!(error["msg"].as_str().unwrap().contains("sessions"));
    wait_for(&mut client, "d");
    send(&mut client, r#"{"cmd":"mark","label":"m"}"#);
    wait_for_error(&mut client, "was not saved", "note");
}

/// Messages that arrive in the next `period`.
fn collect_for(client: &mut Client, period: Duration) -> Vec<Value> {
    let until = Instant::now() + period;
    let mut found = Vec::new();
    while Instant::now() < until {
        if let Some(m) = next_message(client) {
            found.push(m);
        }
    }
    found
}

fn first_message(client: &mut Client) -> Value {
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        if let Some(m) = next_message(client) {
            return m;
        }
    }
    panic!("no message within 10 s");
}

#[test]
fn a_drop_after_a_reconnect_is_reported_again_and_hello_comes_once() {
    let first = PtyDevice::start(onhead_stream(), Some(config_reply()), REAL_RATE);
    let second = PtyDevice::start(onhead_stream(), Some(config_reply()), REAL_RATE);
    // The server opens the same path each time; the link decides which device it is.
    let link = Scratch::new("link");
    std::os::unix::fs::symlink(first.path(), &link.0).unwrap();
    let server = start_server(link.0.to_str().unwrap());
    let mut client = connect(&server);

    // Exactly one hello per connection to the headset.
    let mut hellos = 0;
    let mut rows = 0;
    while rows < 100 {
        let m = wait_for_any(&mut client);
        match m["t"].as_str().unwrap() {
            "hello" => hellos += 1,
            "d" => rows += m["x"].as_array().unwrap().len(),
            _ => {}
        }
    }
    assert_eq!(hellos, 1);

    // Point the link at the second device, then pull the plug on the first.
    std::fs::remove_file(&link.0).unwrap();
    std::os::unix::fs::symlink(second.path(), &link.0).unwrap();
    first.hang_up();
    let lost = wait_for_error(&mut client, "lost the headset", "link");
    let mut after = collect_for(&mut client, Duration::from_secs(1));
    let deadline = Instant::now() + Duration::from_secs(30);
    while !after.iter().any(|m| m["t"] == "hello") {
        assert!(Instant::now() < deadline, "no second hello within 30 s");
        after.extend(collect_for(&mut client, Duration::from_millis(500)));
    }
    after.extend(collect_for(&mut client, Duration::from_secs(1)));
    assert_eq!(after.iter().filter(|m| m["t"] == "hello").count(), 1);

    // A second drop within a minute says the same thing again.
    second.hang_up();
    let again = wait_for_error(&mut client, "lost the headset", "link");
    assert_eq!(again["msg"], lost["msg"]);
    let mut late = connect(&server);
    let greeting = first_message(&mut late);
    assert_eq!(greeting["t"], "error", "{greeting}");
    assert_eq!(greeting["kind"], "link");
}

/// The next message of any type.
fn wait_for_any(client: &mut Client) -> Value {
    let deadline = Instant::now() + Duration::from_secs(30);
    while Instant::now() < deadline {
        if let Some(m) = next_message(client) {
            return m;
        }
    }
    panic!("no message within 30 s");
}

#[test]
fn sigint_between_connection_attempts_exits_promptly() {
    let mut server = start_server("/dev/does-not-exist");
    let mut client = connect(&server);
    wait_for_error(&mut client, "could not", "link");
    server.signal(Signal::SIGINT);
    let status = server.wait_exit(Duration::from_secs(2));
    assert!(status.success(), "{status:?}");
}

/// Wait until the session's `meta.json` says it is finished.
fn wait_finished(dir: &Path) {
    wait_until("meta.json to be finished", || {
        std::fs::read_to_string(dir.join("meta.json"))
            .ok()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .is_some_and(|meta| meta["finished"] == true)
    });
}

fn markers(dir: &Path) -> Vec<Value> {
    std::fs::read_to_string(dir.join("markers.jsonl"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

/// Every sample of the BDF equals the sample with the same index decoded
/// from `raw.bin` (lost samples, which the BDF fills, are skipped), and the
/// BDF holds no other data than whole-record padding after the last one.
fn assert_bdf_matches_raw(dir: &Path, rows: &[Option<Vec<i32>>]) {
    let bdf = read_bdf(&dir.join("recording.bdf"));
    assert_eq!(bdf.data.len(), 23);
    assert_eq!(bdf.data[0].len(), rows.len().div_ceil(500) * 500);
    for (s, row) in rows.iter().enumerate() {
        if let Some(row) = row {
            assert_eq!(&bdf.sample(s), row, "sample {s}");
        }
    }
    if !rows.len().is_multiple_of(500) {
        let end = rows.len() as f64 / 500.0;
        assert!(
            all_labels(&bdf).contains(&(end, "padding".to_string())),
            "padding at {end}"
        );
    }
}

#[test]
fn markers_and_bdf_count_samples_from_the_start_of_raw_bin() {
    let device = PtyDevice::start(onhead_stream(), Some(config_reply()), REAL_RATE);
    let server = start_server(&device.path());
    let mut client = connect(&server);
    stream_rows(&mut client, 1000);
    send(&mut client, r#"{"cmd":"mark","label":"mid"}"#);
    let dir = sessions(&server.out).pop().expect("a session directory");
    wait_until("the marker", || {
        std::fs::read_to_string(dir.join("markers.jsonl")).is_ok_and(|text| text.contains("mid"))
    });
    // Let the recording run on past the marker, then pull the plug.
    let mut after = 0;
    while after < 500 {
        after += wait_for(&mut client, "d")["x"].as_array().unwrap().len();
    }
    device.hang_up();
    wait_finished(&dir);

    for name in SESSION_FILES {
        assert!(dir.join(name).is_file(), "{name}");
    }
    let marks = markers(&dir);
    assert_eq!(marks[0]["label"], "stream_start");
    assert_eq!(marks[0]["sample"], 0);
    let mid = marks.iter().find(|m| m["label"] == "mid").unwrap();
    let sample = mid["sample"].as_u64().unwrap() as usize;
    assert!(sample >= 1000, "{mid}");

    let rows = replay_rows(&dir);
    assert!(sample < rows.len(), "{sample} of {}", rows.len());
    let bdf = read_bdf(&dir.join("recording.bdf"));
    let labels = all_labels(&bdf);
    let onset = labels
        .iter()
        .find(|(_, label)| label == "mid")
        .map(|(onset, _)| *onset)
        .expect("the marker is annotated");
    assert_eq!((onset * 500.0).round() as usize, sample);
    assert_eq!(Some(bdf.sample(sample)), rows[sample]);
    assert!(labels.contains(&(0.0, "stream_start".to_string())));
    assert_bdf_matches_raw(&dir, &rows);

    let clock = std::fs::read_to_string(dir.join("clock.jsonl")).unwrap();
    let first: Value = serde_json::from_str(clock.lines().next().unwrap()).unwrap();
    assert_eq!(first["sample"], 0);
}

#[test]
fn a_client_that_never_reads_and_a_stalled_server_cost_the_recording_nothing() {
    let device = PtyDevice::start(onhead_stream(), Some(config_reply()), REAL_RATE);
    let server = start_server(&device.path());
    let mut client = connect(&server);
    stream_rows(&mut client, 100);
    // Connected, never read: the server's queue for it fills.
    let idle = connect(&server);
    // The whole server stops for two seconds, then carries on.
    server.signal(Signal::SIGSTOP);
    std::thread::sleep(Duration::from_secs(2));
    server.signal(Signal::SIGCONT);
    std::thread::sleep(Duration::from_secs(3));
    let dir = sessions(&server.out).pop().expect("a session directory");
    device.hang_up();
    wait_finished(&dir);
    drop(idle);

    let raw = std::fs::read(dir.join("raw.bin")).unwrap();
    let played = device.played_after_config().expect("a configuration reply");
    let probe = &raw[..10 * 75];
    let start = played
        .windows(probe.len())
        .position(|w| w == probe)
        .expect("raw bytes are from the played stream");
    assert_eq!(raw[..], played[start..start + raw.len()]);
    let rows = replay_rows(&dir);
    assert!(rows.iter().all(Option::is_some), "the device lost nothing");
    assert!(rows.len() >= 2000, "only {} samples", rows.len());
    assert_bdf_matches_raw(&dir, &rows);
}

#[test]
fn refuses_http_port_65535_with_a_message() {
    let out = Scratch::new("serve-port");
    let mut child = Command::new(env!("CARGO_BIN_EXE_open-cognionics"))
        .arg("/dev/does-not-exist")
        .arg("65535")
        .env("OPEN_COGNIONICS_OUT", &out.0)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .expect("start the server");
    let deadline = Instant::now() + Duration::from_secs(10);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("the server did not exit");
        }
        std::thread::sleep(Duration::from_millis(50));
    };
    let mut stderr = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut stderr)
        .unwrap();
    assert!(!status.success(), "{status:?}");
    assert!(stderr.contains("65534"), "{stderr}");
    assert!(!out.0.exists(), "refused, but wrote {:?}", out.0);
    assert!(!stderr.contains("panicked"), "{stderr}");
}
