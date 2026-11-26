//! Local web server: reads the headset through the library, streams samples
//! to a browser page and records the session.
//!
//!     cargo run --features server -- [serial-port] [http-port]
//!
//! With no serial port (and no `OPEN_COGNIONICS_PORT` in the environment) the
//! only plugged-in headset dongle is used. The page is served on
//! `http://127.0.0.1:<http-port>` (default 8737) and the live data on the
//! next port up (so `http-port` is at most 65534). Each connection to the
//! headset writes a new session directory `<out>/session-<unix time>/`
//! (`<out>` is `captures`, or `OPEN_COGNIONICS_OUT`; a suffix `-<n>` is added
//! if the name is taken) holding `raw.bin`, `markers.jsonl`, `clock.jsonl`,
//! `meta.json` and `recording.bdf`.
//!
//! A session's `raw.bin` begins at the first bytes read after the recorder
//! was attached (the recorder is started right after the headset opens). The
//! recording is fed on the headset's reader thread from the raw bytes, not
//! from the events sent to the browsers, so a slow page or a busy event loop
//! cannot make it lose samples. Every sample number it saves (markers, BDF
//! annotations) counts from the first whole packet in `raw.bin`; the
//! `stream_start` marker is at sample 0.
//!
//! When the headset cannot be reached, or the link drops, the error is
//! printed, sent to connected browsers as `{"t":"error","msg":...}`, the
//! recording is finished, and a new connection is tried every few seconds
//! (the same message is repeated at most once a minute). If the recording
//! cannot be started the live view continues and the page shows the error.
//! Ctrl-C (SIGINT) or SIGTERM finishes the recording and exits with status 0.
//!
//! The only bytes this program can cause to be written to the headset are the
//! configuration request (when opening) and the impedance check on / off
//! commands, all sent by the library.

use open_cognionics::chunk::StreamEvent;
use open_cognionics::config::DeviceConfig;
use open_cognionics::record::{Recording, SessionMeta};
use open_cognionics::scale;
use open_cognionics::transport::Headset;
use serde_json::json;
use std::io::ErrorKind;
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError, SyncSender};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tungstenite::handshake::server::{ErrorResponse, Request, Response};
use tungstenite::http::StatusCode;
use tungstenite::Message;

const PAGE: &str = include_str!("../../web/index.html");
const ORDER_PAGE: &str = include_str!("../../web/order.html");

/// Messages a client may fall behind by (about ten seconds) before it is dropped.
const CLIENT_QUEUE: usize = 256;
/// Events the headset may be ahead of this program by before some are missed.
const EVENT_QUEUE: usize = 4096;
/// Wait between attempts to reach the headset.
const RETRY_AFTER: Duration = Duration::from_secs(3);
/// The page has no participant field.
const PARTICIPANT: &str = "unknown";

type Hub = Arc<Mutex<Vec<SyncSender<Arc<str>>>>>;
type SharedRecorder = Arc<Mutex<Option<Recording>>>;

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|e| e.into_inner())
}

/// Set by SIGINT / SIGTERM; the headset loop finishes the recording and returns.
static STOP: AtomicBool = AtomicBool::new(false);

/// State shared by the headset thread and the client threads.
struct Shared {
    hub: Hub,
    recorder: SharedRecorder,
    headset: Mutex<Option<Arc<Headset>>>,
    /// What a client that connects now is sent first: `hello` while the
    /// headset is connected, else the current error. Lock order: `hub`, then
    /// `greeting`.
    greeting: Mutex<Vec<String>>,
}

fn unix_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

/// `kind` is "link" (the headset is missing or dropped) or "note" (something
/// the user should know that does not affect the link: a mark not saved, a
/// recording that could not start).
fn error_message(kind: &str, text: &str) -> String {
    json!({ "t": "error", "kind": kind, "msg": text }).to_string()
}

impl Shared {
    fn broadcast(&self, message: String) {
        let message: Arc<str> = message.into();
        lock(&self.hub).retain(|client| client.try_send(message.clone()).is_ok());
    }

    /// Make `messages` what new clients are sent first, and send them to the
    /// clients already connected. A client sees them once: it is either
    /// registered before (and gets the broadcast) or after (and gets the
    /// greeting), never both.
    fn publish(&self, messages: Vec<String>) {
        let mut hub = lock(&self.hub);
        for message in &messages {
            let message: Arc<str> = message.as_str().into();
            hub.retain(|client| client.try_send(message.clone()).is_ok());
        }
        *lock(&self.greeting) = messages;
    }

    /// Tell every browser (and any that connect later) about a link error,
    /// and print it if `print`.
    fn report(&self, text: &str, print: bool) {
        if print {
            eprintln!("{text}");
        }
        self.publish(vec![error_message("link", text)]);
    }

    /// Print a note and tell the browsers now connected.
    fn note(&self, text: &str) {
        eprintln!("{text}");
        self.broadcast(error_message("note", text));
    }

    /// Record a marker at the recording's current position. The error is
    /// the text to give a client that asked for a mark nothing could save;
    /// the server's own marks ignore it.
    fn mark(&self, label: &str) -> Result<(), String> {
        let unix_time = unix_now();
        match lock(&self.recorder).as_mut() {
            Some(recording) => {
                recording.mark(label, unix_time);
                Ok(())
            }
            None => Err(format!(
                "mark {label:?} was not saved: no recording is running"
            )),
        }
    }

    fn set_impedance(&self, on: bool) {
        let headset = lock(&self.headset).clone();
        let Some(headset) = headset else {
            self.note("the headset is not connected; the contact check was not changed");
            return;
        };
        match headset.set_impedance_check(on) {
            Ok(()) => {
                // Without a recording there is nothing to note it in.
                let _ = self.mark(if on {
                    "sent_impedance_on"
                } else {
                    "sent_impedance_off"
                });
            }
            Err(e) => self.note(&e.to_string()),
        }
    }
}

fn session_dir(out: &Path) -> PathBuf {
    let unix = unix_now() as u64;
    let mut dir = out.join(format!("session-{unix}"));
    let mut n = 1;
    while dir.exists() {
        dir = out.join(format!("session-{unix}-{n}"));
        n += 1;
    }
    dir
}

/// Rows of EEG microvolts and the last accelerometer reading of a chunk.
fn chunk_rows(
    config: &DeviceConfig,
    counts: &[i32],
    channels: usize,
    rows: &mut Vec<Vec<f32>>,
) -> [f64; 3] {
    let layout = config.layout;
    let microvolts = config.eeg_volts_per_count() * 1e6;
    let mut acc = [0.0f64; 3];
    for row in counts.chunks_exact(channels) {
        rows.push(
            row[..layout.eeg]
                .iter()
                .map(|&c| (c as f64 * microvolts) as f32)
                .collect(),
        );
        let first = layout.eeg + layout.ext;
        for (a, &c) in acc.iter_mut().zip(&row[first..]) {
            *a = scale::acc_units(c);
        }
    }
    acc
}

/// Stream one connection to the headset to the browsers and the recording.
/// Returns why it ended, with a note if the recording could not be finished.
fn run_session(shared: &Shared, headset: Arc<Headset>, out: &Path) -> String {
    let config = headset.config().clone();
    // Start the recording first thing, so that its raw.bin begins as early as
    // it can.
    let dir = session_dir(out);
    let meta = SessionMeta {
        participant: PARTICIPANT.into(),
        participant_uuid: None,
        start_unix: unix_now(),
        config: config.clone(),
        port: headset.port().to_string(),
        software_version: env!("CARGO_PKG_VERSION").into(),
    };
    let started = Recording::start(&dir, meta);
    let events = headset.subscribe(EVENT_QUEUE);
    let mut hello = vec![
        json!({ "t": "hello", "fs": config.sample_rate, "labels": config.labels }).to_string(),
    ];
    let sink = match started {
        Ok(mut recording) => {
            println!("recording to {}", dir.display());
            // Before any byte is taken: sample 0.
            recording.mark("stream_start", unix_now());
            *lock(&shared.recorder) = Some(recording);
            let sink_recorder = shared.recorder.clone();
            Some(
                headset.add_raw_sink(Box::new(move |bytes, _index, arrival| {
                    if let Some(recording) = lock(&sink_recorder).as_mut() {
                        recording.take(bytes, arrival);
                    }
                })),
            )
        }
        Err(e) => {
            // The live view still works; the page shows why nothing is recorded.
            // This note is kept in the greeting so that a client that connects
            // later is told too.
            let text = format!("could not start the recording in {}: {e}", dir.display());
            eprintln!("{text}");
            hello.push(error_message("note", &text));
            None
        }
    };
    *lock(&shared.headset) = Some(headset.clone());
    shared.publish(hello);

    let (mut rows, mut first, mut acc): (Vec<Vec<f32>>, u64, [f64; 3]) = (Vec::new(), 0, [0.0; 3]);
    let (mut lost, mut battery, mut impedance) = (0u64, 0.0, false);
    let mut last_flush = Instant::now();
    let mut reason = None;
    while reason.is_none() {
        if STOP.load(Ordering::SeqCst) {
            reason = Some("stopped".to_string());
            break;
        }
        let event = match events.recv_timeout(Duration::from_millis(40)) {
            Ok(event) => Some(event),
            Err(RecvTimeoutError::Timeout) => None,
            Err(RecvTimeoutError::Disconnected) => {
                reason = Some(
                    headset
                        .disconnected()
                        .unwrap_or_else(|| "the event stream closed".into()),
                );
                None
            }
        };
        let mut flush_now = false;
        if let Some(event) = event {
            match event {
                StreamEvent::Chunk(chunk) => {
                    if rows.is_empty() {
                        first = chunk.index;
                    }
                    acc = chunk_rows(&config, &chunk.counts, chunk.channels, &mut rows);
                    battery = chunk.battery_volts;
                    impedance = chunk.impedance_on;
                }
                StreamEvent::Lost { count, .. } => {
                    lost += u64::from(count);
                    flush_now = true;
                }
                StreamEvent::Disconnected(why) => reason = Some(why),
            }
        }
        if !rows.is_empty() && (flush_now || last_flush.elapsed() >= Duration::from_millis(40)) {
            let message = json!({
                "t": "d", "i": first, "x": rows, "acc": acc,
                "bat": battery, "imp": impedance, "lost": lost,
            });
            shared.broadcast(message.to_string());
            rows.clear();
            last_flush = Instant::now();
        }
    }

    if let Some(sink) = sink {
        headset.remove_raw_sink(sink);
    }
    *lock(&shared.headset) = None;
    let recording = lock(&shared.recorder).take();
    let mut reason = reason.unwrap_or_default();
    if let Some(mut recording) = recording {
        if let Some(why) = headset.disconnected() {
            // Annotated in the BDF+ file at the last recorded sample.
            recording.disconnected(&why);
        }
        // Never rely on a drop to flush: finish explicitly and say how it went.
        match recording.finish() {
            Ok(()) => println!("recording finished: {}", dir.display()),
            Err(e) => reason.push_str(&format!(
                "; the recording in {} may be incomplete: {e}",
                dir.display()
            )),
        }
    }
    // Closes the port (the last reference; a client thread may hold one briefly).
    drop(headset);
    reason
}

/// Says each failure when it changes, and the same one at most once a minute.
#[derive(Default)]
struct Reporter {
    last: Option<(String, Instant)>,
}

impl Reporter {
    /// `session` is how long the connection that just ended lasted, if one
    /// was made. A failure after a session is always sent to the pages (they
    /// were told the link was live); it is printed unless it repeats the last
    /// printed text within a minute after a session shorter than ten seconds.
    /// A failure to connect that repeats the last text is not said again.
    fn report(&mut self, shared: &Shared, text: String, session: Option<Duration>) {
        let same_recent = matches!(
            &self.last,
            Some((last, at)) if *last == text && at.elapsed() < Duration::from_secs(60)
        );
        if session.is_none() && same_recent {
            return;
        }
        let flapping = session.is_some_and(|d| d < Duration::from_secs(10));
        let print = !(same_recent && flapping);
        shared.report(&text, print);
        if print {
            self.last = Some((text, Instant::now()));
        }
    }
}

/// Sleep for `period`, or until a stop is requested.
fn sleep_unless_stopped(period: Duration) {
    let until = Instant::now() + period;
    while Instant::now() < until && !STOP.load(Ordering::SeqCst) {
        thread::sleep(Duration::from_millis(50));
    }
}

/// Connect to the headset again and again, serving each connection, until
/// a stop is requested.
fn headset_loop(shared: &Shared, port: Option<&str>, out: &Path) {
    let mut reporter = Reporter::default();
    while !STOP.load(Ordering::SeqCst) {
        match Headset::open(port) {
            Ok(headset) => {
                let started = Instant::now();
                let reason = run_session(shared, Arc::new(headset), out);
                if STOP.load(Ordering::SeqCst) {
                    if reason != "stopped" {
                        eprintln!("{reason}");
                    }
                    return;
                }
                reporter.report(
                    shared,
                    format!("lost the headset: {reason}"),
                    Some(started.elapsed()),
                );
            }
            Err(e) => reporter.report(shared, format!("could not reach the headset: {e}"), None),
        }
        sleep_unless_stopped(RETRY_AFTER);
    }
}

/// Refuse websocket handshakes that do not come from our own page, so that
/// another site open in the same browser cannot read the stream or send commands.
// The handshake callback's error type is fixed by tungstenite.
#[allow(clippy::result_large_err)]
fn check_origin(
    request: &Request,
    response: Response,
    allowed: &[String],
) -> Result<Response, ErrorResponse> {
    let origin = request
        .headers()
        .get("Origin")
        .and_then(|value| value.to_str().ok());
    match origin {
        Some(origin) if allowed.iter().any(|a| a == origin) => Ok(response),
        _ => {
            let mut refusal = ErrorResponse::new(None);
            *refusal.status_mut() = StatusCode::FORBIDDEN;
            Err(refusal)
        }
    }
}

// The handshake callback's error type is fixed by tungstenite.
#[allow(clippy::result_large_err)]
fn serve_client(stream: TcpStream, shared: Arc<Shared>, allowed: Arc<Vec<String>>) {
    let handshake =
        |request: &Request, response: Response| check_origin(request, response, &allowed);
    let Ok(mut socket) = tungstenite::accept_hdr(stream, handshake) else {
        return;
    };
    let _ = socket
        .get_ref()
        .set_read_timeout(Some(Duration::from_millis(15)));
    let _ = socket
        .get_ref()
        .set_write_timeout(Some(Duration::from_secs(2)));
    let (tx, rx) = mpsc::sync_channel::<Arc<str>>(CLIENT_QUEUE);
    // Registering and reading the greeting under one lock (see `publish`)
    // means a client gets each state change exactly once.
    let greeting = {
        let mut hub = lock(&shared.hub);
        hub.push(tx);
        lock(&shared.greeting).clone()
    };
    for message in greeting {
        if socket.send(Message::text(message)).is_err() {
            return;
        }
    }

    loop {
        while let Ok(message) = rx.try_recv() {
            if socket.send(Message::text(message.to_string())).is_err() {
                return;
            }
        }
        match socket.read() {
            Ok(Message::Text(text)) => {
                let Ok(request) = serde_json::from_str::<serde_json::Value>(text.as_str()) else {
                    continue;
                };
                match request["cmd"].as_str() {
                    Some("impedance") => {
                        shared.set_impedance(request["on"].as_bool().unwrap_or(false));
                    }
                    Some("mark") => {
                        if let Some(label) = request["label"].as_str() {
                            if let Err(text) = shared.mark(label) {
                                eprintln!("{text}");
                                if socket
                                    .send(Message::text(error_message("note", &text)))
                                    .is_err()
                                {
                                    return;
                                }
                            }
                        }
                    }
                    _ => {}
                }
            }
            Ok(Message::Close(_)) => return,
            Ok(_) => {}
            Err(tungstenite::Error::Io(e))
                if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {}
            Err(_) => return,
        }
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    let port_name = args
        .get(1)
        .cloned()
        .or_else(|| std::env::var("OPEN_COGNIONICS_PORT").ok());
    let http_port: u16 = args.get(2).map_or(Ok(8737), |s| s.parse())?;
    let out = PathBuf::from(std::env::var("OPEN_COGNIONICS_OUT").unwrap_or("captures".into()));

    let Some(live_port) = http_port.checked_add(1) else {
        return Err(format!(
            "http-port {http_port} is too high: the live data uses the next port up, \
             so http-port must be at most 65534"
        )
        .into());
    };
    let http = tiny_http::Server::http(("127.0.0.1", http_port)).map_err(|e| e.to_string())?;
    let listener = TcpListener::bind(("127.0.0.1", live_port))?;
    let shared = Arc::new(Shared {
        hub: Arc::new(Mutex::new(Vec::new())),
        recorder: Arc::new(Mutex::new(None)),
        headset: Mutex::new(None),
        greeting: Mutex::new(vec![error_message("link", "connecting to the headset")]),
    });

    thread::spawn(move || {
        for request in http.incoming_requests() {
            let header =
                tiny_http::Header::from_bytes("Content-Type", "text/html; charset=utf-8").unwrap();
            let page = if request.url().starts_with("/order") {
                ORDER_PAGE
            } else {
                PAGE
            };
            let _ = request.respond(tiny_http::Response::from_string(page).with_header(header));
        }
    });
    {
        let shared = shared.clone();
        let allowed = Arc::new(vec![
            format!("http://127.0.0.1:{http_port}"),
            format!("http://localhost:{http_port}"),
        ]);
        thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let (shared, allowed) = (shared.clone(), allowed.clone());
                thread::spawn(move || serve_client(stream, shared, allowed));
            }
        });
    }

    ctrlc::set_handler(|| {
        // A second signal while the recording is being finished ends it at once.
        if STOP.swap(true, Ordering::SeqCst) {
            std::process::exit(130);
        }
    })?;

    println!("open http://127.0.0.1:{http_port}");
    headset_loop(&shared, port_name.as_deref(), &out);
    Ok(())
}
