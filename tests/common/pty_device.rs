//! A test harness that plays recorded headset bytes through a real OS
//! pseudo-terminal, so the library opens and reads an actual serial device
//! node. It is not a stand-in for any part of the library: everything the
//! library does to the port (open, termios, reads, writes, close) happens
//! for real.
//!
//! The device side plays `stream` in a loop, one whole packet at a time, at
//! `bytes_per_second`. Every byte the library writes is recorded. When the
//! library writes `0x14` the configuration reply is inserted between two
//! packets, as the headset does.
//!
//! The only byte of the recording this harness alters is the packet counter
//! (byte 1 of each packet, modulo 128). It is rewritten so counters stay
//! continuous when the stream loops and across the inserted configuration
//! reply. All other bytes are played exactly as recorded.
//!
//! Like a serial link, the two directions are independent: the bytes the
//! library writes are read (and requests noticed) even while the stream waits
//! for room in the pty. Stream bytes that are due but not yet played stand
//! for bytes the headset has sent and the computer holds: like the computer's
//! serial buffer, they are at most [`BACKLOG_SECONDS`] of stream (a headset
//! never waits for its reader, so when the port is left unread for longer the
//! oldest packets are lost, their counter values skipped as when real packets
//! are lost), and a configuration reply comes after them, never ahead of them.

use std::os::fd::{AsFd, OwnedFd};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use nix::errno::Errno;
use nix::fcntl::{fcntl, FcntlArg, FdFlag, OFlag};
use nix::pty::openpty;
use nix::sys::termios::{cfmakeraw, tcgetattr, tcsetattr, SetArg};

/// Bytes per packet in the recorded stream (20 EEG + 3 ACC channels).
pub const PACKET_LEN: usize = 75;
/// The byte that asks the headset for its configuration.
const SEND_CONFIGURATION: u8 = 0x14;
/// Packets written per pacing step at most, so requests are noticed quickly.
const MAX_BATCH: usize = 20;
/// Seconds of stream held for a reader that is behind; older packets are
/// lost. A real computer's serial buffer is bounded too (its size for the
/// dongle on macOS is not known); five seconds is long enough that a reader
/// stopped for a couple of seconds (a loaded machine, or a test that stops
/// the reading process for 2 s) loses nothing, and short enough that a port
/// left unread for minutes does not hold minutes of old data.
pub const BACKLOG_SECONDS: f64 = 5.0;

struct Shared {
    master: Mutex<Option<OwnedFd>>,
    written: Mutex<Vec<u8>>,
    config_requests: AtomicUsize,
    played: Mutex<Played>,
    stop: AtomicBool,
    /// Configuration requests still to be ignored (not answered).
    ignore_requests: AtomicUsize,
    /// Play no stream bytes at all (a headset that is switched off).
    silent: bool,
    /// Never read what the library writes, like a dongle whose flow control
    /// holds the computer's output: no request is answered.
    deaf: bool,
    /// Stream packets lost because nobody read the port for longer than
    /// [`BACKLOG_SECONDS`].
    dropped: AtomicUsize,
    /// How late the stream was, in seconds, when the last configuration
    /// reply was played (see [`PtyDevice::lag_at_config`]).
    lag_at_config: Mutex<Option<f64>>,
}

#[derive(Default)]
struct Played {
    bytes: Vec<u8>,
    /// Offset in `bytes` just after the last configuration reply.
    after_config: Option<usize>,
}

impl Shared {
    /// Move every byte the library has written into `written`.
    fn drain(&self) {
        let master = self.master.lock().unwrap();
        let Some(fd) = master.as_ref() else { return };
        let mut written = self.written.lock().unwrap();
        let mut buf = [0u8; 256];
        loop {
            match nix::unistd::read(fd.as_fd(), &mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    let requests = buf[..n].iter().filter(|&&b| b == SEND_CONFIGURATION);
                    self.config_requests
                        .fetch_add(requests.count(), Ordering::SeqCst);
                    written.extend_from_slice(&buf[..n]);
                }
                // EAGAIN: nothing more queued. EIO: the slave side is not open.
                Err(_) => break,
            }
        }
    }

    /// Write all of `bytes` to the master, waiting while the pty is full.
    /// Returns false if the device was stopped first.
    fn play(&self, bytes: &[u8], is_config: bool) -> bool {
        let mut off = 0;
        while off < bytes.len() {
            if self.stop.load(Ordering::SeqCst) {
                return false;
            }
            let result = {
                let master = self.master.lock().unwrap();
                match master.as_ref() {
                    Some(fd) => nix::unistd::write(fd, &bytes[off..]),
                    None => return false,
                }
            };
            match result {
                Ok(n) => {
                    // Recorded as written: a stop can leave a packet partly played.
                    let mut played = self.played.lock().unwrap();
                    played.bytes.extend_from_slice(&bytes[off..off + n]);
                    off += n;
                }
                Err(Errno::EAGAIN) => {
                    // The other direction keeps flowing while this one waits,
                    // as on a serial link: what the library writes is taken
                    // now and answered after this write.
                    if !self.deaf {
                        self.drain();
                    }
                    thread::sleep(Duration::from_millis(1))
                }
                Err(e) => panic!("pty master write failed: {e}"),
            }
        }
        let mut played = self.played.lock().unwrap();
        if is_config {
            played.after_config = Some(played.bytes.len());
        }
        true
    }
}

/// A pseudo-terminal that behaves like the headset's serial port.
pub struct PtyDevice {
    path: String,
    shared: Arc<Shared>,
    slave: Mutex<Option<OwnedFd>>,
    thread: Mutex<Option<JoinHandle<()>>>,
}

impl PtyDevice {
    /// Start playing `stream` (whole packets) at `bytes_per_second`.
    pub fn start(
        stream: Vec<u8>,
        config_reply: Option<Vec<u8>>,
        bytes_per_second: usize,
    ) -> PtyDevice {
        Self::start_with(stream, config_reply, bytes_per_second, 0, false, false)
    }

    /// Like `start`, but never reads what the library writes (see `deaf`).
    pub fn start_deaf(
        stream: Vec<u8>,
        config_reply: Option<Vec<u8>>,
        bytes_per_second: usize,
    ) -> PtyDevice {
        Self::start_with(stream, config_reply, bytes_per_second, 0, false, true)
    }

    /// Like `start`, but the first `ignore_requests` configuration requests
    /// get no reply.
    pub fn start_ignoring(
        stream: Vec<u8>,
        config_reply: Option<Vec<u8>>,
        bytes_per_second: usize,
        ignore_requests: usize,
    ) -> PtyDevice {
        Self::start_with(
            stream,
            config_reply,
            bytes_per_second,
            ignore_requests,
            false,
            false,
        )
    }

    /// A device that never sends a byte, whatever is written to it.
    pub fn silent() -> PtyDevice {
        Self::start_with(vec![0; PACKET_LEN], None, 1, 0, true, false)
    }

    fn start_with(
        stream: Vec<u8>,
        config_reply: Option<Vec<u8>>,
        bytes_per_second: usize,
        ignore_requests: usize,
        silent: bool,
        deaf: bool,
    ) -> PtyDevice {
        assert!(!stream.is_empty() && stream.len().is_multiple_of(PACKET_LEN));
        let pty = openpty(None, None).expect("openpty");
        // Raw mode before anyone opens the slave: a fresh pty echoes input
        // and translates bytes, which would corrupt both directions.
        let mut termios = tcgetattr(&pty.slave).expect("tcgetattr");
        cfmakeraw(&mut termios);
        tcsetattr(&pty.slave, SetArg::TCSANOW, &termios).expect("tcsetattr");
        fcntl(&pty.master, FcntlArg::F_SETFL(OFlag::O_NONBLOCK)).expect("O_NONBLOCK");
        // A child process (the server under test) must not inherit either end:
        // an inherited master would keep the device open after `hang_up`.
        for fd in [&pty.master, &pty.slave] {
            fcntl(fd, FcntlArg::F_SETFD(FdFlag::FD_CLOEXEC)).expect("FD_CLOEXEC");
        }
        let path = nix::unistd::ttyname(&pty.slave)
            .expect("ttyname")
            .to_str()
            .unwrap()
            .to_string();

        let shared = Arc::new(Shared {
            master: Mutex::new(Some(pty.master)),
            written: Mutex::new(Vec::new()),
            config_requests: AtomicUsize::new(0),
            played: Mutex::new(Played::default()),
            stop: AtomicBool::new(false),
            ignore_requests: AtomicUsize::new(ignore_requests),
            silent,
            deaf,
            dropped: AtomicUsize::new(0),
            lag_at_config: Mutex::new(None),
        });
        let worker = Arc::clone(&shared);
        let thread = thread::spawn(move || run(&worker, &stream, config_reply, bytes_per_second));
        PtyDevice {
            path,
            shared,
            // Held open so the pty keeps its raw settings between openers.
            slave: Mutex::new(Some(pty.slave)),
            thread: Mutex::new(Some(thread)),
        }
    }

    /// The device node the library should open.
    pub fn path(&self) -> String {
        self.path.clone()
    }

    /// Every byte the library has written to the port so far.
    pub fn written(&self) -> Vec<u8> {
        self.shared.drain();
        self.shared.written.lock().unwrap().clone()
    }

    /// Every byte played so far.
    pub fn played(&self) -> Vec<u8> {
        self.shared.played.lock().unwrap().bytes.clone()
    }

    /// Bytes played after the end of the last configuration reply.
    pub fn played_after_config(&self) -> Option<Vec<u8>> {
        let played = self.shared.played.lock().unwrap();
        played.after_config.map(|at| played.bytes[at..].to_vec())
    }

    /// How late, in seconds, the stream was when the last configuration reply
    /// was played: the age of the oldest packet then due and not yet played
    /// (0 when none was). The packets after the reply are no older than this.
    pub fn lag_at_config(&self) -> Option<f64> {
        *self.shared.lag_at_config.lock().unwrap()
    }

    /// Stream packets lost because nobody read the port for longer than
    /// [`BACKLOG_SECONDS`].
    pub fn dropped_packets(&self) -> usize {
        self.shared.dropped.load(Ordering::SeqCst)
    }

    /// Stop playing and close the device side, as when the dongle is unplugged.
    pub fn hang_up(&self) {
        self.shared.stop.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.lock().unwrap().take() {
            thread.join().expect("pty device thread panicked");
        }
        self.shared.drain();
        self.shared.master.lock().unwrap().take();
        self.slave.lock().unwrap().take();
    }
}

impl Drop for PtyDevice {
    fn drop(&mut self) {
        self.hang_up();
    }
}

fn run(shared: &Shared, stream: &[u8], config_reply: Option<Vec<u8>>, bytes_per_second: usize) {
    let packets = stream.len() / PACKET_LEN;
    let mut next = 0usize; // next packet of `stream` to play
    let mut counter = 0u8;
    let start = Instant::now();
    let mut paced = 0usize; // stream bytes played since `start`
                            // A request waiting for its answer: the stream bytes due when it arrived.
                            // They left the headset before the request reached it (the computer holds
                            // them), so the reply comes after them, as on the real link.
    let mut answer_after: Option<usize> = None;
    while !shared.stop.load(Ordering::SeqCst) {
        if !shared.deaf {
            shared.drain();
        }
        if !shared.deaf
            && answer_after.is_none()
            && shared.config_requests.load(Ordering::SeqCst) > 0
        {
            shared.config_requests.fetch_sub(1, Ordering::SeqCst);
            answer_after = Some(if shared.silent {
                0
            } else {
                (start.elapsed().as_secs_f64() * bytes_per_second as f64) as usize
            });
        }
        if answer_after.is_some_and(|after| shared.silent || paced > after) {
            answer_after = None;
            let ignored = shared
                .ignore_requests
                .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
                .is_ok();
            if let (false, false, Some(reply)) = (ignored, shared.silent, &config_reply) {
                let due = (start.elapsed().as_secs_f64() * bytes_per_second as f64) as usize;
                let lag = due.saturating_sub(paced) as f64 / bytes_per_second as f64;
                if !shared.play(reply, true) {
                    return;
                }
                *shared.lag_at_config.lock().unwrap() = Some(lag);
            }
        }
        if shared.silent {
            thread::sleep(Duration::from_millis(1));
            continue;
        }
        let due = (start.elapsed().as_secs_f64() * bytes_per_second as f64) as usize;
        // Paced against the start time: after a stall (a starved thread or a
        // full pty) the device catches up, MAX_BATCH packets per step, so the
        // bytes played match the elapsed time. Only the last BACKLOG_SECONDS
        // are caught up; older packets are lost.
        let limit = (BACKLOG_SECONDS * bytes_per_second as f64) as usize;
        let owed = due.saturating_sub(paced);
        if owed > limit {
            let lost = (owed - limit) / PACKET_LEN;
            shared.dropped.fetch_add(lost, Ordering::SeqCst);
            counter = ((counter as usize + lost) % 128) as u8;
            next = (next + lost) % packets;
            paced += lost * PACKET_LEN;
        }
        let mut batch = 0;
        while paced <= due && batch < MAX_BATCH {
            let at = next * PACKET_LEN;
            let mut packet = stream[at..at + PACKET_LEN].to_vec();
            packet[1] = counter;
            if !shared.play(&packet, false) {
                return;
            }
            counter = (counter + 1) % 128;
            next = (next + 1) % packets;
            paced += PACKET_LEN;
            batch += 1;
        }
        thread::sleep(Duration::from_millis(1));
    }
}
