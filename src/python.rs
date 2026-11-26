//! Python bindings: the `open_cognionics._core` extension module.
//!
//! Built by maturin with the `python` feature. The public Python names are
//! re-exported by `python/open_cognionics/__init__.py`.
//!
//! Locking rule: a [`Mutex`] taken while the thread is detached from the
//! interpreter (`Python::detach`) is always released before re-attaching, so
//! a thread holding the interpreter never waits for a lock whose holder is
//! waiting for the interpreter.

use std::path::PathBuf;
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use numpy::ndarray::{Array1, Array2};
use numpy::{IntoPyArray, PyArray1, PyArray2};
use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyOSError, PyTimeoutError, PyValueError};
use pyo3::prelude::*;

use crate::chunk::{Chunk, StreamEvent};
use crate::config::DeviceConfig;
use crate::record::{Recording, SessionMeta};
use crate::replay::{Marker, Replay, ReplayError};
use crate::scale::EXT_VOLTS_PER_COUNT;
use crate::transport::{self, Headset, PortInfo, SinkId, TransportError};

/// Longest a `stream()` iterator waits before checking for Ctrl-C.
const POLL: Duration = Duration::from_millis(100);

create_exception!(
    open_cognionics,
    Error,
    PyException,
    "Base class of the errors raised by open_cognionics."
);
create_exception!(
    open_cognionics,
    NoHeadset,
    Error,
    "No headset dongle is plugged in."
);
create_exception!(
    open_cognionics,
    SeveralHeadsets,
    Error,
    "More than one headset dongle is plugged in; the message lists their ports."
);
create_exception!(
    open_cognionics,
    OpenError,
    Error,
    "The port could not be opened or set up."
);
create_exception!(
    open_cognionics,
    NoData,
    Error,
    "Nothing at all was received from the headset."
);
create_exception!(
    open_cognionics,
    NoConfiguration,
    Error,
    "The headset streams but did not answer the configuration requests."
);
create_exception!(
    open_cognionics,
    ConfigError,
    Error,
    "The headset's configuration arrived but could not be understood."
);
create_exception!(
    open_cognionics,
    Disconnected,
    Error,
    "The connection to the headset was lost."
);

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

fn now_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn transport_error(e: TransportError) -> PyErr {
    let message = e.to_string();
    match e {
        TransportError::NoHeadset { .. } => NoHeadset::new_err(message),
        TransportError::SeveralHeadsets(_) => SeveralHeadsets::new_err(message),
        TransportError::Open(_) => OpenError::new_err(message),
        TransportError::NoData => NoData::new_err(message),
        TransportError::NoConfiguration { .. } => NoConfiguration::new_err(message),
        TransportError::Config(_) => ConfigError::new_err(message),
        TransportError::Disconnected(_) => Disconnected::new_err(message),
    }
}

fn replay_error(e: ReplayError) -> PyErr {
    match e {
        ReplayError::Io(e) => PyErr::from(e),
        other => PyValueError::new_err(other.to_string()),
    }
}

fn closed_error() -> PyErr {
    PyValueError::new_err("the headset is closed")
}

/// What the headset reports about itself.
#[pyclass(frozen, name = "Config", module = "open_cognionics", from_py_object)]
#[derive(Clone)]
pub struct PyConfig {
    inner: DeviceConfig,
}

#[pymethods]
impl PyConfig {
    /// Electrode labels of the EEG channels, in the order of `Chunk.data` columns.
    #[getter]
    fn labels(&self) -> Vec<String> {
        self.inner.labels.clone()
    }

    /// EEG labels followed by `ACC_X`, `ACC_Y`, `ACC_Z`.
    #[getter]
    fn channel_names(&self) -> Vec<String> {
        self.inner.channel_names()
    }

    /// Samples per second.
    #[getter]
    fn sample_rate(&self) -> f64 {
        self.inner.sample_rate
    }

    /// Amplifier gain (not the gain index).
    #[getter]
    fn gain(&self) -> u32 {
        self.inner.gain
    }

    /// Number of EEG channels.
    #[getter]
    fn n_eeg(&self) -> usize {
        self.inner.layout.eeg
    }

    /// Number of extension (ExG) channels.
    #[getter]
    fn n_ext(&self) -> usize {
        self.inner.layout.ext
    }

    /// Number of accelerometer channels.
    #[getter]
    fn n_acc(&self) -> usize {
        self.inner.layout.acc
    }

    /// Volts per ADC count on the EEG channels at this gain.
    #[getter]
    fn eeg_volts_per_count(&self) -> f64 {
        self.inner.eeg_volts_per_count()
    }

    /// Whether the headset starts with its impedance check on.
    #[getter]
    fn impedance_at_power_on(&self) -> bool {
        self.inner.impedance_at_power_on
    }

    /// Whether the headset sends the delta ("high-speed") stream format.
    #[getter]
    fn compressed(&self) -> bool {
        self.inner.compressed
    }

    fn __repr__(&self) -> String {
        format!(
            "Config(n_eeg={}, n_ext={}, n_acc={}, sample_rate={}, gain={})",
            self.inner.layout.eeg,
            self.inner.layout.ext,
            self.inner.layout.acc,
            self.inner.sample_rate,
            self.inner.gain
        )
    }
}

/// A serial port that looks like a headset dongle.
#[pyclass(frozen, name = "PortInfo", module = "open_cognionics")]
pub struct PyPortInfo {
    inner: PortInfo,
}

#[pymethods]
impl PyPortInfo {
    #[new]
    #[pyo3(signature = (path, description, serial_number = None))]
    fn new(path: String, description: String, serial_number: Option<String>) -> Self {
        PyPortInfo {
            inner: PortInfo {
                path,
                description,
                serial_number,
            },
        }
    }

    /// Device path to pass to `connect`.
    #[getter]
    fn path(&self) -> String {
        self.inner.path.clone()
    }

    /// The USB product string.
    #[getter]
    fn description(&self) -> String {
        self.inner.description.clone()
    }

    /// The USB serial number, if the dongle reports one.
    #[getter]
    fn serial_number(&self) -> Option<String> {
        self.inner.serial_number.clone()
    }

    fn __repr__(&self) -> String {
        format!(
            "PortInfo(path={:?}, description={:?}, serial_number={:?})",
            self.inner.path, self.inner.description, self.inner.serial_number
        )
    }
}

/// A marker saved with a recording.
#[pyclass(frozen, name = "Marker", module = "open_cognionics")]
pub struct PyMarker {
    inner: Marker,
}

#[pymethods]
impl PyMarker {
    /// The text given to `Recording.mark`.
    #[getter]
    fn label(&self) -> String {
        self.inner.label.clone()
    }

    /// Samples recorded when `mark` was called: the index of the next
    /// recorded sample (0 is the first sample of the recording, the first
    /// whole packet in `raw.bin`; lost samples count). It can be early by the
    /// link latency plus up to one read of samples; `Recorded.sample_at` of
    /// `unix_time` uses the saved clock instead.
    #[getter]
    fn sample(&self) -> u64 {
        self.inner.sample
    }

    /// Host time (seconds since the Unix epoch) when the marker was placed.
    #[getter]
    fn unix_time(&self) -> f64 {
        self.inner.unix_time
    }

    fn __repr__(&self) -> String {
        format!(
            "Marker(label={:?}, sample={}, unix_time={})",
            self.inner.label, self.inner.sample, self.inner.unix_time
        )
    }
}

/// A run of consecutive samples.
#[pyclass(frozen, name = "Chunk", module = "open_cognionics")]
pub struct PyChunk {
    index: u64,
    samples: usize,
    time: f64,
    lost: u64,
    battery_volts: f64,
    impedance_on: bool,
    data: Py<PyArray2<f64>>,
    ext: Py<PyArray2<f64>>,
    acc: Py<PyArray2<i32>>,
    triggers: Py<PyArray1<u16>>,
}

impl PyChunk {
    fn new(py: Python<'_>, chunk: Chunk, lost: u64, config: &DeviceConfig) -> PyResult<PyChunk> {
        let columns = split_columns(&chunk, config).map_err(PyValueError::new_err)?;
        Ok(PyChunk {
            index: chunk.index,
            samples: chunk.samples,
            time: chunk.time,
            lost,
            battery_volts: chunk.battery_volts,
            impedance_on: chunk.impedance_on,
            data: columns.eeg.into_pyarray(py).unbind(),
            ext: columns.ext.into_pyarray(py).unbind(),
            acc: columns.acc.into_pyarray(py).unbind(),
            triggers: Array1::from_vec(chunk.triggers).into_pyarray(py).unbind(),
        })
    }
}

/// A chunk's samples split by channel kind and scaled.
struct ChunkColumns {
    eeg: Array2<f64>,
    ext: Array2<f64>,
    acc: Array2<i32>,
}

/// Split `chunk` into EEG volts, EXT volts and ACC counts. Fails if the
/// chunk's shape does not match `config`'s layout.
fn split_columns(chunk: &Chunk, config: &DeviceConfig) -> Result<ChunkColumns, String> {
    let layout = config.layout;
    let (eeg, ext, acc) = (layout.eeg, layout.ext, layout.acc);
    let n = chunk.samples;
    if chunk.channels != layout.channels()
        || chunk.counts.len() != n * chunk.channels
        || chunk.triggers.len() != n
    {
        return Err(format!(
            "chunk of {n} samples x {} channels ({} values, {} triggers) does not match \
             the layout of {} EEG + {} EXT + {} ACC channels",
            chunk.channels,
            chunk.counts.len(),
            chunk.triggers.len(),
            eeg,
            ext,
            acc
        ));
    }
    let volts = config.eeg_volts_per_count();
    let mut eeg_data = Vec::with_capacity(n * eeg);
    let mut ext_data = Vec::with_capacity(n * ext);
    let mut acc_data = Vec::with_capacity(n * acc);
    for row in chunk.counts.chunks_exact(chunk.channels.max(1)) {
        eeg_data.extend(row[..eeg].iter().map(|&c| f64::from(c) * volts));
        ext_data.extend(
            row[eeg..eeg + ext]
                .iter()
                .map(|&c| f64::from(c) * EXT_VOLTS_PER_COUNT),
        );
        acc_data.extend_from_slice(&row[eeg + ext..]);
    }
    let shape_error = |e: numpy::ndarray::ShapeError| e.to_string();
    Ok(ChunkColumns {
        eeg: Array2::from_shape_vec((n, eeg), eeg_data).map_err(shape_error)?,
        ext: Array2::from_shape_vec((n, ext), ext_data).map_err(shape_error)?,
        acc: Array2::from_shape_vec((n, acc), acc_data).map_err(shape_error)?,
    })
}

#[pymethods]
impl PyChunk {
    /// Index of the first sample since the stream started (lost samples count).
    #[getter]
    fn index(&self) -> u64 {
        self.index
    }

    /// Number of samples (rows).
    #[getter]
    fn samples(&self) -> usize {
        self.samples
    }

    /// Host time of the first sample, seconds since the Unix epoch for a live
    /// headset. Sample `k` is at `time + k / sample_rate`. Live, it is host
    /// clock time fitted to the arrival times of the bytes; the constant part
    /// of the link latency is not removed. From `replay()` it is the session
    /// start plus `n / sample_rate`.
    #[getter]
    fn time(&self) -> f64 {
        self.time
    }

    /// Number of samples lost immediately before this chunk.
    #[getter]
    fn lost(&self) -> u64 {
        self.lost
    }

    /// Battery voltage, from the last sample.
    #[getter]
    fn battery_volts(&self) -> f64 {
        self.battery_volts
    }

    /// Whether the impedance check was on at the last sample.
    #[getter]
    fn impedance_on(&self) -> bool {
        self.impedance_on
    }

    /// EEG in volts, float64, shape (samples, n_eeg), columns in `Config.labels` order.
    #[getter]
    fn data<'py>(&self, py: Python<'py>) -> Bound<'py, PyArray2<f64>> {
        self.data.bind(py).clone()
    }

    /// Extension (ExG) channels in volts, float64, shape (samples, n_ext).
    #[getter]
    fn ext<'py>(&self, py: Python<'py>) -> Bound<'py, PyArray2<f64>> {
        self.ext.bind(py).clone()
    }

    /// Accelerometer, raw counts, int32, shape (samples, n_acc).
    #[getter]
    fn acc<'py>(&self, py: Python<'py>) -> Bound<'py, PyArray2<i32>> {
        self.acc.bind(py).clone()
    }

    /// Trigger value of every sample, uint16, shape (samples,).
    #[getter]
    fn triggers<'py>(&self, py: Python<'py>) -> Bound<'py, PyArray1<u16>> {
        self.triggers.bind(py).clone()
    }

    fn __repr__(&self) -> String {
        format!(
            "Chunk(index={}, samples={}, lost={}, time={})",
            self.index, self.samples, self.lost, self.time
        )
    }
}

/// An open connection to a headset. Use `connect` to make one.
#[pyclass(frozen, name = "Headset", module = "open_cognionics")]
pub struct PyHeadset {
    inner: Mutex<Option<Headset>>,
    config: DeviceConfig,
    port: String,
    /// The sample index when the headset was closed.
    closed_index: Mutex<u64>,
    /// Why the link had dropped when the headset was closed, if it had.
    closed_reason: Mutex<Option<String>>,
}

/// `None`, infinity and durations too long to represent mean no limit;
/// negative and NaN values are rejected.
fn parse_timeout(timeout: Option<f64>) -> PyResult<Option<Duration>> {
    match timeout {
        None => Ok(None),
        Some(t) if t.is_nan() || t < 0.0 => Err(PyValueError::new_err(format!(
            "timeout must be None or a number of seconds >= 0, not {t}"
        ))),
        Some(t) => Ok(Duration::try_from_secs_f64(t).ok()),
    }
}

impl PyHeadset {
    /// Why the link dropped, whether or not the headset has been closed
    /// since; `None` if it did not drop.
    fn link_lost(&self) -> Option<String> {
        match lock(&self.inner).as_ref() {
            Some(headset) => headset.disconnected(),
            None => lock(&self.closed_reason).clone(),
        }
    }

    fn with_open<T>(&self, f: impl FnOnce(&Headset) -> T) -> PyResult<T> {
        match lock(&self.inner).as_ref() {
            Some(headset) => Ok(f(headset)),
            None => Err(closed_error()),
        }
    }
}

#[pymethods]
impl PyHeadset {
    /// The configuration the headset reported when opened.
    #[getter]
    fn config(&self) -> PyConfig {
        PyConfig {
            inner: self.config.clone(),
        }
    }

    /// The device path of this connection.
    #[getter]
    fn port(&self) -> String {
        self.port.clone()
    }

    /// Index of the next sample: whole packets (and lost ones) since the
    /// configuration reply. After `close`, the index at closing.
    #[getter]
    fn sample_index(&self) -> u64 {
        match lock(&self.inner).as_ref() {
            Some(headset) => headset.sample_index(),
            None => *lock(&self.closed_index),
        }
    }

    /// Turn the headset's impedance check on (writes `0x11`) or off (`0x12`).
    /// Raises `Disconnected` if the link has dropped.
    fn impedance_check(&self, py: Python<'_>, on: bool) -> PyResult<()> {
        py.detach(|| self.with_open(|h| h.set_impedance_check(on)))?
            .map_err(transport_error)
    }

    /// Iterate over chunks as they arrive.
    ///
    /// `queue` is how many undelivered events are held; when the consumer
    /// falls behind further, chunks are dropped and the next chunk's index
    /// jumps (recordings are not affected). With `min_samples` above 1,
    /// consecutive chunks are joined until at least that many samples are
    /// held; chunks are never joined across lost samples. Each wait for data
    /// lasts at most 100 ms between checks for Ctrl-C. With `timeout`, raises
    /// `TimeoutError` if no chunk arrives within that many seconds. Raises
    /// `Disconnected` when the link drops; ends when the headset is closed.
    #[pyo3(signature = (queue = 256, timeout = None, min_samples = 1))]
    fn stream(
        slf: &Bound<'_, Self>,
        queue: usize,
        timeout: Option<f64>,
        min_samples: usize,
    ) -> PyResult<PyStream> {
        let timeout = parse_timeout(timeout)?;
        let this = slf.get();
        let rx = this.with_open(|h| h.subscribe(queue))?;
        Ok(PyStream {
            state: Mutex::new(StreamState {
                rx,
                buffer: None,
                pending_lost: 0,
                disconnected: None,
                closed: false,
                finished: false,
            }),
            headset: slf.clone().unbind(),
            config: this.config.clone(),
            timeout,
            min_samples: min_samples.max(1),
        })
    }

    /// Start recording into `directory` (created if needed). Raises
    /// `FileExistsError`, changing nothing, if the directory already holds
    /// any of the session files.
    ///
    /// The recording is written on the reader thread from the raw bytes, so
    /// it does not depend on anyone iterating `stream()` and keeps every byte
    /// received. Use it as a context manager or call `close()`.
    #[pyo3(signature = (directory, participant, participant_uuid = None))]
    fn record(
        slf: &Bound<'_, Self>,
        directory: PathBuf,
        participant: String,
        participant_uuid: Option<String>,
    ) -> PyResult<PyRecording> {
        let this = slf.get();
        let start_unix = now_seconds();
        let meta = SessionMeta {
            participant,
            participant_uuid,
            start_unix,
            config: this.config.clone(),
            port: this.port.clone(),
            software_version: env!("CARGO_PKG_VERSION").to_string(),
        };
        let guard = lock(&this.inner);
        let headset = guard.as_ref().ok_or_else(closed_error)?;
        if let Some(reason) = headset.disconnected() {
            return Err(Disconnected::new_err(format!("disconnected: {reason}")));
        }
        // Refuses (FileExistsError) a directory that already holds a recording.
        let recording = Recording::start(&directory, meta)?;
        let state = Arc::new(Mutex::new(Some(recording)));
        let sink_state = Arc::clone(&state);
        let sink = headset.add_raw_sink(Box::new(move |bytes, _index, arrival| {
            if let Some(rec) = lock(&sink_state).as_mut() {
                rec.take(bytes, arrival);
            }
        }));
        drop(guard);
        Ok(PyRecording {
            state,
            sink,
            headset: slf.clone().unbind(),
            directory,
        })
    }

    /// Stop the reader thread and close the port. Calling it again does nothing.
    fn close(&self, py: Python<'_>) {
        let headset = {
            let mut guard = lock(&self.inner);
            if let Some(h) = guard.as_ref() {
                *lock(&self.closed_index) = h.sample_index();
                *lock(&self.closed_reason) = h.disconnected();
            }
            guard.take()
        };
        py.detach(move || drop(headset));
    }

    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    #[pyo3(signature = (*_args))]
    fn __exit__(&self, py: Python<'_>, _args: &Bound<'_, pyo3::types::PyTuple>) -> bool {
        self.close(py);
        false
    }

    fn __repr__(&self) -> String {
        let state = if lock(&self.inner).is_some() {
            "open"
        } else {
            "closed"
        };
        format!("Headset(port={:?}, {state})", self.port)
    }
}

struct StreamState {
    rx: Receiver<StreamEvent>,
    /// Samples held back while joining chunks, and the loss before them.
    buffer: Option<(Chunk, u64)>,
    /// Samples lost since the last chunk.
    pending_lost: u64,
    disconnected: Option<String>,
    /// The channel has closed.
    closed: bool,
    /// Nothing more will be returned.
    finished: bool,
}

enum Step {
    Ready(Chunk, u64),
    Wait,
    End,
    Disconnected(String),
    TimedOut,
}

impl StreamState {
    /// Wait until `until` at most for something to return.
    fn step(
        &mut self,
        until: Instant,
        deadline: Option<Instant>,
        min_samples: usize,
        why_closed: impl Fn() -> Option<String>,
    ) -> Step {
        loop {
            if self.finished {
                return Step::End;
            }
            if self
                .buffer
                .as_ref()
                .is_some_and(|(c, _)| c.samples >= min_samples)
            {
                return self.release();
            }
            if self.closed || self.disconnected.is_some() {
                if self.buffer.is_some() {
                    return self.release();
                }
                self.finished = true;
                return match self.disconnected.take() {
                    Some(reason) => Step::Disconnected(reason),
                    None => Step::End,
                };
            }
            let now = Instant::now();
            if deadline.is_some_and(|d| now >= d) {
                return match self.buffer {
                    Some(_) => self.release(),
                    None => Step::TimedOut,
                };
            }
            if now >= until {
                return Step::Wait;
            }
            let wait_until = deadline.map_or(until, |d| d.min(until));
            match self.rx.recv_timeout(wait_until - now) {
                Ok(StreamEvent::Chunk(chunk)) => {
                    let joins = self.pending_lost == 0
                        && self
                            .buffer
                            .as_ref()
                            .is_some_and(|(b, _)| b.index + b.samples as u64 == chunk.index);
                    if joins {
                        if let Some((b, _)) = self.buffer.as_mut() {
                            join(b, chunk);
                        }
                    } else {
                        let lost = std::mem::take(&mut self.pending_lost);
                        let previous = self.buffer.replace((chunk, lost));
                        if let Some((c, l)) = previous {
                            return Step::Ready(c, l);
                        }
                    }
                }
                Ok(StreamEvent::Lost { count, .. }) => {
                    self.pending_lost += u64::from(count);
                    if self.buffer.is_some() {
                        return self.release();
                    }
                }
                Ok(StreamEvent::Disconnected(reason)) => self.disconnected = Some(reason),
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => {
                    self.closed = true;
                    if self.disconnected.is_none() {
                        self.disconnected = why_closed();
                    }
                }
            }
        }
    }

    fn release(&mut self) -> Step {
        match self.buffer.take() {
            Some((c, l)) => Step::Ready(c, l),
            None => Step::Wait,
        }
    }
}

/// Append `next` (which starts where `chunk` ends) to `chunk`.
fn join(chunk: &mut Chunk, next: Chunk) {
    chunk.samples += next.samples;
    chunk.counts.extend_from_slice(&next.counts);
    chunk.triggers.extend_from_slice(&next.triggers);
    chunk.battery_volts = next.battery_volts;
    chunk.impedance_on = next.impedance_on;
}

/// Iterator returned by `Headset.stream()`.
#[pyclass(frozen, name = "Stream", module = "open_cognionics")]
pub struct PyStream {
    state: Mutex<StreamState>,
    headset: Py<PyHeadset>,
    config: DeviceConfig,
    timeout: Option<Duration>,
    min_samples: usize,
}

#[pymethods]
impl PyStream {
    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Option<PyChunk>> {
        let deadline = self.timeout.and_then(|t| Instant::now().checked_add(t));
        loop {
            let step = py.detach(|| {
                // A closed channel without a Disconnected event (the queue
                // was full) still reports a dropped link, even after close().
                let why_closed = || self.headset.get().link_lost();
                lock(&self.state).step(
                    Instant::now() + POLL,
                    deadline,
                    self.min_samples,
                    why_closed,
                )
            });
            match step {
                Step::Ready(chunk, lost) => {
                    return PyChunk::new(py, chunk, lost, &self.config).map(Some)
                }
                Step::Wait => py.check_signals()?,
                Step::End => return Ok(None),
                Step::Disconnected(reason) => {
                    return Err(Disconnected::new_err(format!("disconnected: {reason}")))
                }
                Step::TimedOut => {
                    return Err(PyTimeoutError::new_err("no data from the headset in time"))
                }
            }
        }
    }
}

/// A recording in progress. Returned by `Headset.record()`.
#[pyclass(frozen, name = "Recording", module = "open_cognionics")]
pub struct PyRecording {
    state: Arc<Mutex<Option<Recording>>>,
    sink: SinkId,
    headset: Py<PyHeadset>,
    directory: PathBuf,
}

impl PyRecording {
    /// Stop receiving bytes and complete the files. `None` if already done.
    fn finish(&self) -> Option<std::io::Result<()>> {
        let mut disconnected = None;
        if let Some(h) = lock(&self.headset.get().inner).as_ref() {
            h.remove_raw_sink(self.sink);
            disconnected = h.disconnected();
        }
        let mut rec = lock(&self.state).take()?;
        if let Some(reason) = disconnected {
            // Annotated in the BDF+ file at the last recorded sample.
            rec.disconnected(&reason);
        }
        Some(rec.finish())
    }
}

#[pymethods]
impl PyRecording {
    /// The session directory.
    #[getter]
    fn directory(&self) -> PathBuf {
        self.directory.clone()
    }

    /// Place a marker, stamped with the host time of the call. Its `sample`
    /// is the number of samples recorded when it was placed (the index of
    /// the next sample to be recorded). Returns that sample.
    fn mark(&self, py: Python<'_>, label: &str) -> PyResult<u64> {
        let unix_time = now_seconds();
        // The reader thread holds this lock while it writes the files, so
        // wait for it with the interpreter released.
        let placed = py.detach(|| {
            lock(&self.state)
                .as_mut()
                .map(|rec| rec.mark(label, unix_time))
        });
        placed.ok_or_else(|| PyValueError::new_err("the recording is closed"))
    }

    /// Stop recording and complete the files; raises `OSError` if any write
    /// failed. Calling it again does nothing.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        match py.detach(|| self.finish()) {
            Some(Err(e)) => Err(PyOSError::new_err(format!(
                "recording in {}: {e}",
                self.directory.display()
            ))),
            _ => Ok(()),
        }
    }

    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    #[pyo3(signature = (*_args))]
    fn __exit__(&self, py: Python<'_>, _args: &Bound<'_, pyo3::types::PyTuple>) -> PyResult<bool> {
        self.close(py)?;
        Ok(false)
    }
}

impl Drop for PyRecording {
    fn drop(&mut self) {
        // Not closed explicitly: complete the files anyway. An error here
        // cannot be raised; `close()` is the way to see it.
        let _ = self.finish();
    }
}

/// A saved recording, iterated as chunks. Returned by `replay()`.
#[pyclass(frozen, name = "Replay", module = "open_cognionics")]
pub struct PyReplay {
    inner: Mutex<Replay>,
    config: DeviceConfig,
    markers: Vec<Marker>,
    clock_error: Option<String>,
    lost_at_end: Mutex<u64>,
}

#[pymethods]
impl PyReplay {
    /// The configuration used to decode the bytes.
    #[getter]
    fn config(&self) -> PyConfig {
        PyConfig {
            inner: self.config.clone(),
        }
    }

    /// The markers from `markers.jsonl` (empty for a bare `raw.bin`).
    #[getter]
    fn markers(&self) -> Vec<PyMarker> {
        self.markers
            .iter()
            .map(|m| PyMarker { inner: m.clone() })
            .collect()
    }

    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    /// What was left out of `clock.jsonl` (for example `line 3: cut short`),
    /// or `None` when the file was whole or absent. The chunks do not use
    /// the clock.
    #[getter]
    fn clock_error(&self) -> Option<String> {
        self.clock_error.clone()
    }

    /// Samples lost after the last chunk, which no chunk can carry in its
    /// `lost`. 0 until iteration has ended. The sum of `samples + lost` over
    /// all chunks plus this equals the rows of `read_recording`.
    #[getter]
    fn lost_at_end(&self) -> u64 {
        *lock(&self.lost_at_end)
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Option<PyChunk>> {
        let next = py.detach(|| {
            let mut replay = lock(&self.inner);
            let mut lost = 0u64;
            loop {
                match replay.next() {
                    Some(StreamEvent::Chunk(c)) => return Ok(Some((c, lost))),
                    Some(StreamEvent::Lost { count, .. }) => lost += u64::from(count),
                    Some(StreamEvent::Disconnected(_)) => {}
                    None => {
                        *lock(&self.lost_at_end) += lost;
                        return match replay.error() {
                            Some(e) => Err(std::io::Error::new(e.kind(), e.to_string())),
                            None => Ok(None),
                        };
                    }
                }
            }
        })?;
        next.map(|(c, lost)| PyChunk::new(py, c, lost, &self.config))
            .transpose()
    }
}

/// Dongles that report themselves as a Cognionics headset. Lists ports only;
/// nothing is opened.
#[pyfunction]
fn find_headsets(py: Python<'_>) -> Vec<PyPortInfo> {
    py.detach(transport::find_headsets)
        .into_iter()
        .map(|inner| PyPortInfo { inner })
        .collect()
}

/// The headset to use when none was named: `ports` must hold exactly one.
/// Raises `NoHeadset` for none and `SeveralHeadsets` for more than one.
#[pyfunction]
fn select_headset(ports: Vec<Bound<'_, PyPortInfo>>) -> PyResult<PyPortInfo> {
    let ports = ports.iter().map(|p| p.get().inner.clone()).collect();
    transport::select_headset(ports)
        .map(|inner| PyPortInfo { inner })
        .map_err(transport_error)
}

/// Open the headset at `port`, or the only one plugged in if `None`.
///
/// Sends the configuration request (`0x14`) and waits for the reply before
/// returning. Returns or raises within 8 s whatever the device sends
/// (`transport::CONNECT_LIMIT`); the call cannot be interrupted.
#[pyfunction]
#[pyo3(signature = (port = None))]
fn connect(py: Python<'_>, port: Option<String>) -> PyResult<PyHeadset> {
    let headset = py
        .detach(|| Headset::open(port.as_deref()))
        .map_err(transport_error)?;
    Ok(PyHeadset {
        config: headset.config().clone(),
        port: headset.port().to_string(),
        inner: Mutex::new(Some(headset)),
        closed_index: Mutex::new(0),
        closed_reason: Mutex::new(None),
    })
}

/// Read a session directory (or a bare `raw.bin`, which needs `config`) as
/// the chunks a live headset produces. With `realtime`, chunks are released
/// no faster than they were recorded.
#[pyfunction]
#[pyo3(signature = (path, realtime = false, config = None))]
fn replay(path: PathBuf, realtime: bool, config: Option<PyConfig>) -> PyResult<PyReplay> {
    let replay = Replay::open(&path, config.map(|c| c.inner), realtime).map_err(replay_error)?;
    Ok(PyReplay {
        config: replay.config().clone(),
        markers: replay.markers().to_vec(),
        clock_error: replay.clock_error().map(str::to_owned),
        inner: Mutex::new(replay),
        lost_at_end: Mutex::new(0),
    })
}

/// Columns of a whole recording, with NaN rows for lost samples.
type Columns<'py> = (
    PyConfig,
    Vec<PyMarker>,
    Vec<(u64, f64)>,
    Option<String>,
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<f64>>,
);

/// Decode a whole recording: (config, markers, clock points as (sample,
/// unix_time), what was left out of clock.jsonl, eeg volts, ext volts, acc
/// counts).
#[pyfunction]
#[pyo3(signature = (path, config = None))]
fn _read_recording(
    py: Python<'_>,
    path: PathBuf,
    config: Option<PyConfig>,
) -> PyResult<Columns<'_>> {
    let mut replay = Replay::open(&path, config.map(|c| c.inner), false).map_err(replay_error)?;
    let config = replay.config().clone();
    let markers = replay.markers().to_vec();
    let clock = replay
        .clock()
        .iter()
        .map(|p| (p.sample, p.unix_time))
        .collect();
    let clock_error = replay.clock_error().map(str::to_owned);
    let layout = config.layout;
    let (eeg, ext, acc) = (layout.eeg, layout.ext, layout.acc);
    let result = py.detach(|| {
        let (mut d, mut x, mut a) = (Vec::new(), Vec::new(), Vec::new());
        // Counted, not derived from one column's length: a layout may have
        // no EEG (or no EXT, or no ACC) channels.
        let mut rows = 0usize;
        for event in replay.by_ref() {
            match event {
                StreamEvent::Chunk(c) => {
                    let columns = split_columns(&c, &config).map_err(PyValueError::new_err)?;
                    d.extend(columns.eeg.iter());
                    x.extend(columns.ext.iter());
                    a.extend(columns.acc.iter().map(|&v| f64::from(v)));
                    rows += c.samples;
                }
                StreamEvent::Lost { count, .. } => {
                    let n = count as usize;
                    d.extend(std::iter::repeat_n(f64::NAN, n * eeg));
                    x.extend(std::iter::repeat_n(f64::NAN, n * ext));
                    a.extend(std::iter::repeat_n(f64::NAN, n * acc));
                    rows += n;
                }
                StreamEvent::Disconnected(_) => {}
            }
        }
        match replay.error() {
            Some(e) => Err(PyErr::from(std::io::Error::new(e.kind(), e.to_string()))),
            None => Ok((d, x, a, rows)),
        }
    });
    let (d, x, a, rows) = result?;
    let matrix = |v: Vec<f64>, cols: usize| -> PyResult<Bound<'_, PyArray2<f64>>> {
        Array2::from_shape_vec((rows, cols), v)
            .map(|m| m.into_pyarray(py))
            .map_err(|e| PyValueError::new_err(e.to_string()))
    };
    Ok((
        PyConfig { inner: config },
        markers
            .into_iter()
            .map(|inner| PyMarker { inner })
            .collect(),
        clock,
        clock_error,
        matrix(d, eeg)?,
        matrix(x, ext)?,
        matrix(a, acc)?,
    ))
}

/// Compiled part of open_cognionics; use the `open_cognionics` package.
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("Error", py.get_type::<Error>())?;
    m.add("NoHeadset", py.get_type::<NoHeadset>())?;
    m.add("SeveralHeadsets", py.get_type::<SeveralHeadsets>())?;
    m.add("OpenError", py.get_type::<OpenError>())?;
    m.add("NoData", py.get_type::<NoData>())?;
    m.add("NoConfiguration", py.get_type::<NoConfiguration>())?;
    m.add("ConfigError", py.get_type::<ConfigError>())?;
    m.add("Disconnected", py.get_type::<Disconnected>())?;
    m.add_class::<PyConfig>()?;
    m.add_class::<PyPortInfo>()?;
    m.add_class::<PyMarker>()?;
    m.add_class::<PyChunk>()?;
    m.add_class::<PyHeadset>()?;
    m.add_class::<PyStream>()?;
    m.add_class::<PyRecording>()?;
    m.add_class::<PyReplay>()?;
    m.add_function(wrap_pyfunction!(find_headsets, m)?)?;
    m.add_function(wrap_pyfunction!(select_headset, m)?)?;
    m.add_function(wrap_pyfunction!(connect, m)?)?;
    m.add_function(wrap_pyfunction!(replay, m)?)?;
    m.add_function(wrap_pyfunction!(_read_recording, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::legacy::Layout;

    fn config() -> DeviceConfig {
        DeviceConfig {
            layout: Layout {
                eeg: 2,
                ext: 1,
                acc: 1,
            },
            sample_rate: 500.0,
            gain: 3,
            labels: vec!["F7".into(), "Fp1".into()],
            impedance_at_power_on: false,
            compressed: false,
        }
    }

    fn chunk(channels: usize, samples: usize) -> Chunk {
        Chunk {
            index: 0,
            samples,
            channels,
            counts: (0..(channels * samples) as i32).collect(),
            time: 0.0,
            battery_volts: 4.0,
            impedance_on: false,
            triggers: vec![0; samples],
        }
    }

    #[test]
    fn columns_are_split_and_scaled() {
        let c = split_columns(&chunk(4, 2), &config()).expect("matching layout");
        let volts = config().eeg_volts_per_count();
        assert_eq!(c.eeg.shape(), [2, 2]);
        assert_eq!(c.eeg[[1, 0]], 4.0 * volts);
        assert_eq!(c.ext[[1, 0]], 6.0 * EXT_VOLTS_PER_COUNT);
        assert_eq!(c.acc[[1, 0]], 7);
    }

    #[test]
    fn a_chunk_that_does_not_match_the_layout_is_an_error() {
        assert!(split_columns(&chunk(3, 2), &config()).is_err());
        let mut short = chunk(4, 2);
        short.counts.pop();
        assert!(split_columns(&short, &config()).is_err());
        let mut triggers = chunk(4, 2);
        triggers.triggers.pop();
        assert!(split_columns(&triggers, &config()).is_err());
    }
}
