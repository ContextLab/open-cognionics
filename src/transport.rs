//! The live connection to a headset through its USB serial dongle.
//!
//! [`Headset::open`] asks the headset for its configuration, then a reader
//! thread turns the stream into [`StreamEvent`]s for every subscriber and
//! hands the raw bytes to every raw sink. Closing or dropping the
//! [`Headset`] stops the thread and closes the port.

use std::any::Any;
use std::fmt;
use std::fs;
use std::io::ErrorKind;
use std::panic::{self, AssertUnwindSafe};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TrySendError};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serialport::{ClearBuffer, FlowControl, SerialPort, SerialPortInfo, SerialPortType};

use crate::chunk::{Chunker, StreamEvent};
use crate::config::{extract_words, ConfigError, DeviceConfig, END_WORD, MARKER};

const BAUD_RATE: u32 = 3_000_000;
const READ_TIMEOUT: Duration = Duration::from_millis(20);
/// Input that queued up before the port was opened is stale.
const DISCARD_FOR: Duration = Duration::from_millis(300);
/// How long a configuration block may take to arrive once it has started,
/// counted from the last request.
const CONFIG_TIMEOUT: Duration = Duration::from_secs(5);
/// A configuration request with no start marker after it for this long is
/// sent again: on the real headset about one request in three is not answered.
/// If nothing at all has been received by then, opening fails with
/// [`TransportError::NoData`] instead.
pub const RESEND_AFTER: Duration = Duration::from_millis(1500);
/// The most configuration requests sent in one attempt.
pub const MAX_REQUESTS: usize = 4;
/// [`Headset::open`] returns or fails within this long of being called,
/// whatever the device sends. The clock starts when `open` is called, so
/// listing and opening the port count within it (an open call that the
/// operating system itself never returns from cannot be bounded here).
/// Normally it is much sooner: about 0.3 s when the first request is
/// answered, [`TransportError::NoData`] after about 1.8 s,
/// [`TransportError::NoConfiguration`] after about 6.3 s, or 5 s after the
/// request it answered for a reply that starts and does not end. Only a reply
/// (or bytes that look like the start of one) that begins after the fourth
/// request is cut off by this limit; the error then has `at_limit` set.
pub const CONNECT_LIMIT: Duration = Duration::from_secs(8);
/// FTDI FT232R, the chip in the headset's dongle.
const FTDI_VID: u16 = 0x0403;
const FTDI_PID: u16 = 0x6001;

/// A serial port that looks like a headset dongle.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PortInfo {
    /// Device path to pass to [`Headset::open`].
    pub path: String,
    /// The USB product string, e.g. "Cognionics Quick-20 20CH <serial>".
    pub description: String,
    pub serial_number: Option<String>,
}

/// Why a connection could not be made or was lost.
#[derive(Clone, Debug, PartialEq)]
pub enum TransportError {
    /// No headset dongle is plugged in.
    NoHeadset {
        /// The other serial ports seen when the ports were listed; `None`
        /// when the error comes from [`select_headset`], which is given only
        /// headset ports.
        others: Option<Vec<String>>,
    },
    /// More than one dongle is plugged in; pass a path to choose.
    SeveralHeadsets(Vec<PortInfo>),
    /// The port could not be opened or set up.
    Open(String),
    /// Nothing at all was received from the headset within
    /// [`RESEND_AFTER`] of the first configuration request.
    NoData,
    /// The headset streams but did not answer the configuration requests.
    NoConfiguration {
        /// How many requests were sent.
        requests: usize,
        /// True when [`CONNECT_LIMIT`] ended the wait (a reply was still
        /// arriving, or bytes that looked like one); false when the requests
        /// went unanswered or a reply that started did not end in time.
        at_limit: bool,
    },
    /// The configuration arrived but could not be understood.
    Config(ConfigError),
    /// The port went away.
    Disconnected(String),
}

impl fmt::Display for TransportError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TransportError::NoHeadset { others: None } => write!(f, "no headset dongle found"),
            TransportError::NoHeadset {
                others: Some(others),
            } if others.is_empty() => {
                write!(f, "no headset dongle found, and no other serial port")
            }
            TransportError::NoHeadset {
                others: Some(others),
            } => write!(
                f,
                "no headset dongle found; other serial ports: {}",
                others.join(", ")
            ),
            TransportError::SeveralHeadsets(ports) => {
                let paths: Vec<_> = ports.iter().map(|p| p.path.as_str()).collect();
                write!(f, "several headset dongles found: {}", paths.join(", "))
            }
            TransportError::Open(reason) => write!(f, "could not open the port: {reason}"),
            TransportError::NoData => write!(
                f,
                "nothing received from the headset: is it switched on and in range?"
            ),
            TransportError::NoConfiguration {
                requests,
                at_limit: false,
            } => write!(
                f,
                "the headset is streaming but did not answer {requests} configuration requests"
            ),
            TransportError::NoConfiguration {
                requests,
                at_limit: true,
            } => write!(
                f,
                "the headset is streaming but no complete configuration arrived within the \
                 {} s limit ({requests} configuration requests sent)",
                CONNECT_LIMIT.as_secs()
            ),
            TransportError::Config(e) => write!(f, "bad configuration: {e}"),
            TransportError::Disconnected(reason) => write!(f, "disconnected: {reason}"),
        }
    }
}

impl std::error::Error for TransportError {}

impl From<ConfigError> for TransportError {
    fn from(e: ConfigError) -> Self {
        TransportError::Config(e)
    }
}

/// Identifies a raw sink for [`Headset::remove_raw_sink`].
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct SinkId(u64);

/// Receives every stream byte after the configuration reply, with the sample
/// index just after those bytes and the host time they were read (seconds
/// since the Unix epoch).
pub type RawSink = Box<dyn FnMut(&[u8], u64, f64) + Send>;

/// Every byte this library may write to a headset. `0x13` (which rewrites
/// the headset's stored configuration) is deliberately absent: there is no
/// code path that can send it.
#[derive(Clone, Copy, Debug)]
enum Command {
    ImpedanceOn,
    ImpedanceOff,
    SendConfiguration,
}

impl Command {
    fn byte(self) -> u8 {
        match self {
            Command::ImpedanceOn => 0x11,
            Command::ImpedanceOff => 0x12,
            Command::SendConfiguration => 0x14,
        }
    }
}

/// The only write path to the port.
///
/// The byte is handed to the operating system, which sends it; this does
/// not wait until it has left. Waiting (`flush`, which is `tcdrain`) has no
/// time limit: it blocks for as long as the other side does not take the
/// byte, for instance while hardware flow control holds the output. With a
/// pseudo-terminal whose device side was busy writing a backlog it blocked
/// `Headset::open` until the process was killed (40 s and more). The write
/// itself is bounded by the port's read/write timeout.
fn send(port: &mut dyn SerialPort, command: Command) -> std::io::Result<()> {
    port.write_all(&[command.byte()])
}

/// Whether `port` is the macOS `/dev/tty.*` twin of a `/dev/cu.*` port in
/// `ports`. macOS lists each device twice; the cu ("call-out") node is the
/// one that opens without waiting for a carrier, so only that one is used.
fn is_tty_twin(port: &SerialPortInfo, ports: &[SerialPortInfo]) -> bool {
    port.port_name
        .strip_prefix("/dev/tty.")
        .is_some_and(|name| {
            let callout = format!("/dev/cu.{name}");
            ports.iter().any(|p| p.port_name == callout)
        })
}

/// The ports in `ports` that report themselves as a Cognionics headset
/// dongle, each once.
pub fn headsets_among(ports: &[SerialPortInfo]) -> Vec<PortInfo> {
    let mut found = Vec::new();
    for port in ports {
        let SerialPortType::UsbPort(usb) = &port.port_type else {
            continue;
        };
        let product = usb.product.clone().unwrap_or_default();
        if usb.vid != FTDI_VID
            || usb.pid != FTDI_PID
            || !(product.contains("Cognionics") || product.contains("Quick"))
            || is_tty_twin(port, ports)
        {
            continue;
        }
        found.push(PortInfo {
            path: port.port_name.clone(),
            description: product,
            serial_number: usb.serial_number.clone(),
        });
    }
    found
}

/// Dongles that report themselves as a Cognionics headset. Lists ports
/// only; nothing is opened.
pub fn find_headsets() -> Vec<PortInfo> {
    headsets_among(&serialport::available_ports().unwrap_or_default())
}

/// The headset to use when none was named: exactly one must be plugged in.
pub fn select_headset(mut found: Vec<PortInfo>) -> Result<PortInfo, TransportError> {
    match found.len() {
        0 => Err(TransportError::NoHeadset { others: None }),
        1 => Ok(found.remove(0)),
        _ => Err(TransportError::SeveralHeadsets(found)),
    }
}

/// The headset among `ports` (as listed by the operating system) to use
/// when none was named. Like [`select_headset`], but a
/// [`TransportError::NoHeadset`] names the other ports in `ports`.
pub fn choose_headset(ports: &[SerialPortInfo]) -> Result<PortInfo, TransportError> {
    let found = headsets_among(ports);
    if found.is_empty() {
        let others = ports
            .iter()
            .filter(|p| !is_tty_twin(p, ports))
            .map(|p| p.port_name.clone())
            .collect();
        return Err(TransportError::NoHeadset {
            others: Some(others),
        });
    }
    select_headset(found)
}

/// Whether `path` names a pseudo-terminal: macOS `/dev/ttys<N>` or Linux
/// `/dev/pts/<N>`. Only the name is examined.
pub fn is_pseudo_terminal(path: &Path) -> bool {
    let Some(path) = path.to_str() else {
        return false;
    };
    let number = |rest: &str| !rest.is_empty() && rest.bytes().all(|b| b.is_ascii_digit());
    path.strip_prefix("/dev/ttys").is_some_and(number)
        || path.strip_prefix("/dev/pts/").is_some_and(number)
}

/// Whether opening `path` may ignore port settings the OS rejects: only when
/// the path, with symlinks resolved, is positively a pseudo-terminal. Every
/// other path, including anything that cannot be resolved, is treated as a
/// real serial device and gets the strict settings.
pub fn relaxes_port_settings(path: &str) -> bool {
    fs::canonicalize(path).is_ok_and(|resolved| is_pseudo_terminal(&resolved))
}

/// Open `path` at 3,000,000 baud with hardware flow control.
fn open_port(path: &str) -> Result<Box<dyn SerialPort>, TransportError> {
    let builder = serialport::new(path, BAUD_RATE)
        .flow_control(FlowControl::Hardware)
        .timeout(READ_TIMEOUT);
    let port = match builder.clone().open() {
        Ok(port) => port,
        // On a real serial device (the dongle) failing to set the baud rate
        // is an error. Only a path that resolves to a pseudo-terminal (used
        // for testing) is let off: macOS sets non-standard rates with an
        // ioctl that ptys reject (ENOTTY), and serialport applies it inside
        // open(), so open with baud 0, which serialport takes as "leave the
        // rate alone".
        Err(first) if relaxes_port_settings(path) => builder
            .baud_rate(0)
            .open()
            .map_err(|_| TransportError::Open(first.to_string()))?,
        Err(e) => return Err(TransportError::Open(e.to_string())),
    };
    // The flow control is deliberately not read back: the macOS FTDI driver
    // reports `None` after accepting RTS/CTS, so a read-back check refuses
    // the real dongle, which streams without loss with the setting requested.
    // A pseudo-terminal cannot reproduce that driver behaviour, so no test
    // here covers it; a hardware check verifies it on the dongle.
    Ok(port)
}

fn now_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Read one buffer; a timeout is an empty read.
fn read_some(port: &mut dyn SerialPort, buf: &mut [u8]) -> Result<usize, String> {
    match port.read(buf) {
        Ok(0) => Err("the port closed".to_string()),
        Ok(n) => Ok(n),
        Err(e) if matches!(e.kind(), ErrorKind::TimedOut | ErrorKind::Interrupted) => Ok(0),
        Err(e) => Err(e.to_string()),
    }
}

/// Position of the first start marker in `bytes`.
fn find_marker(bytes: &[u8]) -> Option<usize> {
    bytes.windows(MARKER.len()).position(|w| w == MARKER)
}

/// Whether `block` (which starts with a start marker) holds the end word at
/// a whole number of words after the marker. Only the words from offset
/// `*searched` on are looked at; `*searched` is moved past them.
fn has_end_word(block: &[u8], searched: &mut usize) -> bool {
    while *searched + 4 <= block.len() {
        let at = *searched;
        *searched += 4;
        if u32::from_be_bytes([block[at], block[at + 1], block[at + 2], block[at + 3]]) == END_WORD
        {
            return true;
        }
    }
    false
}

/// Ask for the configuration and read until it has arrived.
/// Returns the configuration, the stream bytes that followed it, and the
/// host time they were read.
///
/// `0x14` is sent again if no start marker follows it within
/// [`RESEND_AFTER`], up to [`MAX_REQUESTS`] times in all; it is never sent
/// while a block is arriving. This function writes nothing else.
///
/// If no byte at all has been received (since the port was opened) when
/// [`RESEND_AFTER`] has passed after the first request, the result is
/// [`TransportError::NoData`] at once, after about 1.8 s and one request. A
/// headset that streams but does not answer gets
/// [`TransportError::NoConfiguration`] after [`MAX_REQUESTS`] requests, about
/// 6.3 s.
///
/// Whatever arrives, the result comes by `deadline` (the same errors), and
/// the work per read is proportional to the bytes read: a stream that never
/// stops cannot hold this up, nor can one full of false start markers.
fn read_configuration(
    port: &mut dyn SerialPort,
    deadline: Instant,
) -> Result<(DeviceConfig, Vec<u8>, f64), TransportError> {
    let mut buf = [0u8; 8192];
    let settle = Instant::now() + DISCARD_FOR;
    let mut any_data = false;
    while Instant::now() < settle.min(deadline) {
        let n = read_some(port, &mut buf).map_err(TransportError::Disconnected)?;
        any_data |= n > 0;
    }
    let mut requests = 0;
    // Whether `deadline` ended the wait (see `TransportError::NoConfiguration`).
    let mut at_limit = false;
    // Until a marker is seen: the last bytes received, so a marker split
    // across reads is found. After: the block and what follows, from its marker.
    let mut received: Vec<u8> = Vec::new();
    'request: loop {
        if Instant::now() >= deadline {
            at_limit = true;
            break;
        }
        send(port, Command::SendConfiguration)
            .map_err(|e| TransportError::Disconnected(e.to_string()))?;
        requests += 1;
        let sent = Instant::now();
        // Keep the overlap: a marker may straddle the moment of the resend.
        let mut marker_seen = false;
        // After a marker: the bytes of `received` already searched for the
        // end word, so each read costs only its own bytes.
        let mut searched = MARKER.len();
        loop {
            let n = read_some(port, &mut buf).map_err(TransportError::Disconnected)?;
            let arrival = now_seconds();
            if n > 0 {
                any_data = true;
                received.extend_from_slice(&buf[..n]);
                if !marker_seen {
                    if let Some(at) = find_marker(&received) {
                        marker_seen = true;
                        received.drain(..at);
                    } else if received.len() > MARKER.len() - 1 {
                        let cut = received.len() - (MARKER.len() - 1);
                        received.drain(..cut);
                    }
                }
                if marker_seen && has_end_word(&received, &mut searched) {
                    if let Some(block) = extract_words(&received) {
                        let config = DeviceConfig::from_words(&block.words)?;
                        return Ok((config, received.split_off(block.end), arrival));
                    }
                }
            }
            if Instant::now() >= deadline {
                at_limit = true;
                break 'request;
            }
            let waited = sent.elapsed();
            if marker_seen {
                if waited >= CONFIG_TIMEOUT {
                    break 'request;
                }
            } else if waited >= RESEND_AFTER {
                // A headset that has sent nothing at all, not even stream
                // packets, is not there to answer another request.
                if !any_data || requests >= MAX_REQUESTS {
                    break 'request;
                }
                continue 'request;
            }
        }
    }
    if any_data {
        Err(TransportError::NoConfiguration { requests, at_limit })
    } else {
        Err(TransportError::NoData)
    }
}

/// Lock a mutex, carrying on if a panic elsewhere poisoned it: every value
/// guarded here stays consistent between statements.
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

fn panic_message(payload: &(dyn Any + Send)) -> String {
    if let Some(s) = payload.downcast_ref::<&str>() {
        s.to_string()
    } else if let Some(s) = payload.downcast_ref::<String>() {
        s.clone()
    } else {
        "unknown panic".to_string()
    }
}

/// Raw sinks. While the reader thread calls them they are moved out of the
/// lock, so a sink may add or remove sinks (or drop the [`Headset`]) from
/// its own callback.
#[derive(Default)]
struct Sinks {
    /// Sinks not being called right now.
    idle: Vec<(SinkId, RawSink)>,
    /// Sinks removed while they were out being called.
    removed: Vec<SinkId>,
}

/// State shared by the [`Headset`] and its reader thread.
struct Shared {
    subscribers: Mutex<Vec<SyncSender<StreamEvent>>>,
    sinks: Mutex<Sinks>,
    next_sink: AtomicU64,
    sample_index: AtomicU64,
    disconnected: Mutex<Option<String>>,
    stop: AtomicBool,
    /// The reader has finished. Read and written only while holding
    /// `subscribers`, so a subscription is either broadcast to and closed by
    /// the reader or made after it and told at once.
    ended: AtomicBool,
}

impl Shared {
    /// Send to every subscriber that has room; drop those that hung up.
    fn publish(&self, event: &StreamEvent) {
        lock(&self.subscribers).retain(|tx| match tx.try_send(event.clone()) {
            Ok(()) | Err(TrySendError::Full(_)) => true,
            Err(TrySendError::Disconnected(_)) => false,
        });
    }

    /// Decode `bytes`, then feed raw sinks and subscribers.
    /// Returns the panic message if a raw sink panicked.
    fn handle(
        &self,
        chunker: &mut Chunker,
        bytes: &[u8],
        arrival: f64,
        events: &mut Vec<StreamEvent>,
    ) -> Result<(), String> {
        events.clear();
        chunker.push(bytes, arrival, events);
        let mut index = self.sample_index.load(Ordering::SeqCst);
        for event in events.iter() {
            index = match event {
                StreamEvent::Chunk(c) => c.index + c.samples as u64,
                StreamEvent::Lost { index, count } => index + u64::from(*count),
                StreamEvent::Disconnected(_) => index,
            };
        }
        self.sample_index.store(index, Ordering::SeqCst);
        let panicked = self.call_sinks(bytes, index, arrival);
        for event in events.iter() {
            self.publish(event);
        }
        match panicked {
            Some(message) => Err(format!("a raw sink panicked: {message}")),
            None => Ok(()),
        }
    }

    /// Call every raw sink without holding the lock. A sink that panics is
    /// dropped and its message returned.
    fn call_sinks(&self, bytes: &[u8], index: u64, arrival: f64) -> Option<String> {
        let running = std::mem::take(&mut lock(&self.sinks).idle);
        let mut kept = Vec::with_capacity(running.len());
        let mut panicked = None;
        for (id, mut sink) in running {
            if lock(&self.sinks).removed.contains(&id) {
                continue;
            }
            match panic::catch_unwind(AssertUnwindSafe(|| sink(bytes, index, arrival))) {
                Ok(()) => kept.push((id, sink)),
                Err(payload) => {
                    panicked.get_or_insert_with(|| panic_message(payload.as_ref()));
                }
            }
        }
        let dropped;
        {
            let mut sinks = lock(&self.sinks);
            let removed = std::mem::take(&mut sinks.removed);
            let (gone, mut still): (Vec<_>, Vec<_>) =
                kept.into_iter().partition(|(id, _)| removed.contains(id));
            dropped = gone;
            // Sinks added during the callbacks come after the older ones.
            still.append(&mut sinks.idle);
            sinks.idle = still;
        }
        // Dropped outside the lock: a sink may own things that use it.
        drop(dropped);
        panicked
    }
}

/// The reader thread. However it ends, subscribers are told: a failure
/// (including a panic) publishes [`StreamEvent::Disconnected`], and every
/// channel is closed.
fn run_reader(
    shared: &Shared,
    port: Box<dyn SerialPort>,
    chunker: Chunker,
    leftover: Vec<u8>,
    arrival: f64,
) {
    let outcome = panic::catch_unwind(AssertUnwindSafe(|| {
        read_loop(shared, port, chunker, leftover, arrival)
    }));
    let failure = match outcome {
        Ok(Ok(())) => None,
        Ok(Err(reason)) => Some(reason),
        Err(payload) => Some(format!(
            "the reader thread panicked: {}",
            panic_message(payload.as_ref())
        )),
    };
    if let Some(reason) = &failure {
        *lock(&shared.disconnected) = Some(reason.clone());
    }
    // One lock for the final broadcast, the close and the `ended` flag, so a
    // concurrent `subscribe` lands either before (and gets both) or after.
    let mut subscribers = lock(&shared.subscribers);
    if let Some(reason) = failure {
        let event = StreamEvent::Disconnected(reason);
        for tx in subscribers.iter() {
            let _ = tx.try_send(event.clone());
        }
    }
    // Dropping the senders ends every subscriber's channel.
    subscribers.clear();
    shared.ended.store(true, Ordering::SeqCst);
}

/// Read until stopped (`Ok`) or until the port fails or a sink panics.
/// The port closes when this returns.
fn read_loop(
    shared: &Shared,
    mut port: Box<dyn SerialPort>,
    mut chunker: Chunker,
    leftover: Vec<u8>,
    arrival: f64,
) -> Result<(), String> {
    let mut events = Vec::new();
    shared.handle(&mut chunker, &leftover, arrival, &mut events)?;
    let mut buf = [0u8; 8192];
    while !shared.stop.load(Ordering::SeqCst) {
        let n = read_some(port.as_mut(), &mut buf)?;
        if n > 0 {
            let arrival = now_seconds();
            shared.handle(&mut chunker, &buf[..n], arrival, &mut events)?;
        }
    }
    Ok(())
}

/// An open connection to a headset.
pub struct Headset {
    port_name: String,
    config: DeviceConfig,
    writer: Mutex<Box<dyn SerialPort>>,
    shared: Arc<Shared>,
    reader: Option<JoinHandle<()>>,
}

impl Headset {
    /// Open the headset at `path`, or the only one plugged in if `None`.
    ///
    /// Reads the headset's configuration (sending `0x14`) before returning.
    /// Returns or fails within [`CONNECT_LIMIT`] of being called; listing and
    /// opening the port count within it.
    pub fn open(path: Option<&str>) -> Result<Headset, TransportError> {
        let deadline = Instant::now() + CONNECT_LIMIT;
        let port_name = match path {
            Some(path) => path.to_string(),
            None => {
                let ports = serialport::available_ports().map_err(|e| {
                    TransportError::Open(format!("could not list the serial ports: {e}"))
                })?;
                choose_headset(&ports)?.path
            }
        };
        let mut port = open_port(&port_name)?;
        let (config, leftover, arrival) = match read_configuration(port.as_mut(), deadline) {
            Ok(found) => found,
            Err(error) => {
                // A request the device never took may still be queued for it;
                // closing a port can wait for its output to be sent. Discard
                // it (this sends nothing) so the port closes at once. A
                // failure here changes nothing: the port is being given up.
                let _ = port.clear(ClearBuffer::Output);
                return Err(error);
            }
        };
        let writer = port
            .try_clone()
            .map_err(|e| TransportError::Open(e.to_string()))?;
        let shared = Arc::new(Shared {
            subscribers: Mutex::new(Vec::new()),
            sinks: Mutex::new(Sinks::default()),
            next_sink: AtomicU64::new(0),
            sample_index: AtomicU64::new(0),
            disconnected: Mutex::new(None),
            stop: AtomicBool::new(false),
            ended: AtomicBool::new(false),
        });
        let chunker = Chunker::new(config.layout, config.sample_rate);
        let worker = Arc::clone(&shared);
        let reader = thread::Builder::new()
            .name("open-cognionics reader".to_string())
            .spawn(move || run_reader(&worker, port, chunker, leftover, arrival))
            .map_err(|e| TransportError::Open(e.to_string()))?;
        Ok(Headset {
            port_name,
            config,
            writer: Mutex::new(writer),
            shared,
            reader: Some(reader),
        })
    }

    /// The configuration the headset reported when opened.
    pub fn config(&self) -> &DeviceConfig {
        &self.config
    }

    /// The device path this connection uses.
    pub fn port(&self) -> &str {
        &self.port_name
    }

    /// A new stream of events holding up to `capacity` undelivered events
    /// (at least 1). A subscriber that falls behind misses events (it sees a
    /// gap in chunk indices) but is never removed; it is dropped from the
    /// list only when its receiver is dropped. When the port goes away the
    /// channel receives [`StreamEvent::Disconnected`] if it has room, and is
    /// closed in any case. Subscribing after the port went away gives a
    /// channel holding only that [`StreamEvent::Disconnected`], already closed.
    pub fn subscribe(&self, capacity: usize) -> Receiver<StreamEvent> {
        let (tx, rx) = sync_channel(capacity.max(1));
        let mut subscribers = lock(&self.shared.subscribers);
        if self.shared.ended.load(Ordering::SeqCst) {
            if let Some(reason) = lock(&self.shared.disconnected).clone() {
                let _ = tx.try_send(StreamEvent::Disconnected(reason));
            }
            // `tx` is dropped here, which closes the channel.
        } else {
            subscribers.push(tx);
        }
        rx
    }

    /// Turn the headset's impedance check on or off.
    pub fn set_impedance_check(&self, on: bool) -> Result<(), TransportError> {
        if let Some(reason) = lock(&self.shared.disconnected).clone() {
            return Err(TransportError::Disconnected(reason));
        }
        let command = if on {
            Command::ImpedanceOn
        } else {
            Command::ImpedanceOff
        };
        let mut writer = lock(&self.writer);
        send(writer.as_mut(), command).map_err(|e| TransportError::Disconnected(e.to_string()))
    }

    /// Call `sink` with every stream byte read from now on, on the reader
    /// thread. A sink may add or remove sinks, or drop the [`Headset`], from
    /// its own callback. If a sink panics the stream ends: subscribers get
    /// [`StreamEvent::Disconnected`] and the port is closed.
    pub fn add_raw_sink(&self, sink: RawSink) -> SinkId {
        let id = SinkId(self.shared.next_sink.fetch_add(1, Ordering::SeqCst));
        lock(&self.shared.sinks).idle.push((id, sink));
        id
    }

    /// Stop calling the sink. After this returns the sink is not called
    /// again, apart from a call the reader thread had already begun.
    pub fn remove_raw_sink(&self, id: SinkId) {
        let removed = {
            let mut sinks = lock(&self.shared.sinks);
            match sinks.idle.iter().position(|(i, _)| *i == id) {
                Some(at) => Some(sinks.idle.remove(at)),
                None => {
                    // Out being called right now; dropped when it comes back.
                    sinks.removed.push(id);
                    None
                }
            }
        };
        drop(removed);
    }

    /// Index of the next sample: whole packets (and lost ones) since the
    /// configuration reply.
    pub fn sample_index(&self) -> u64 {
        self.shared.sample_index.load(Ordering::SeqCst)
    }

    /// Why the stream ended, if the port failed or a raw sink panicked;
    /// `None` while it is running.
    pub fn disconnected(&self) -> Option<String> {
        lock(&self.shared.disconnected).clone()
    }

    /// Stop the reader thread and close the port.
    pub fn close(self) {}
}

impl Drop for Headset {
    fn drop(&mut self) {
        self.shared.stop.store(true, Ordering::SeqCst);
        if let Some(reader) = self.reader.take() {
            // Dropped by a raw sink, on the reader thread itself: joining
            // would deadlock. The thread sees `stop` once the sink returns.
            if reader.thread().id() == thread::current().id() {
                return;
            }
            // The reader catches its own panics, so join cannot fail.
            let _ = reader.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn commands_are_exactly_the_three_permitted_bytes() {
        // Exhaustive: a new variant must be added here to compile.
        let all = [
            Command::ImpedanceOn,
            Command::ImpedanceOff,
            Command::SendConfiguration,
        ];
        for command in all {
            match command {
                Command::ImpedanceOn | Command::ImpedanceOff | Command::SendConfiguration => {}
            }
        }
        let bytes: Vec<u8> = all.iter().map(|c| c.byte()).collect();
        assert_eq!(bytes, [0x11, 0x12, 0x14]);
    }
}
