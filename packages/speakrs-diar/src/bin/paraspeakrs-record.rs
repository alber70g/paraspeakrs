//! paraspeakrs-record: record the microphone and the system audio as two mono WAV
//! tracks on one clock, for `paraspeakrs record` to merge and transcribe.
//!
//! The Linux and Windows counterpart of the Swift app in packages/recorder-macos.
//! System audio is the default output's monitor source on Linux (PulseAudio, or
//! PipeWire through pipewire-pulse) and WASAPI loopback of the default output
//! device on Windows.
//!
//! Both tracks are placed on one wall clock that starts with the recording. A
//! track more than 50 ms behind it is padded with silence; one more than 50 ms
//! ahead drops frames. Loopback delivers nothing while nothing plays, so without
//! this the system track would come out short and everything after a silence
//! would sit at the wrong time.
//!
//! The JSON on stdout is versioned (`version: 1`); `record.py` checks it.

use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, OnceLock, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{BufferSize, Device, Host, SampleFormat, SupportedBufferSize};
use serde::Serialize;

const CONTRACT_VERSION: u32 = 1;
const TOLERANCE: Duration = Duration::from_millis(50);
/// Requested callback size. PulseAudio otherwise picks record fragments of up to
/// two seconds, which is coarse for a clock that corrects at 50 ms.
const BUFFER: Duration = Duration::from_millis(20);
const SILENCE_WARNING_AFTER: Duration = Duration::from_secs(30);
/// Peak below which a buffer counts as silence, about -80 dBFS.
const SILENCE_PEAK: f32 = 1e-4;
/// How often the WAV headers are rewritten, so a crash leaves playable files.
const FLUSH_EVERY: Duration = Duration::from_secs(2);

const USAGE: &str = "\
paraspeakrs-record - record the microphone and the system audio as two tracks

USAGE:
    paraspeakrs-record --mic-out <mic.wav> --system-out <system.wav> [OPTIONS]
    paraspeakrs-record --list-devices

OPTIONS:
    --mic <NAME>          Input device whose name contains NAME (default: the default input)
    --system <NAME>       Device to record system audio from (default: the default output's
                          monitor on Linux, loopback of the default output on Windows)
    --seconds <N>         Stop after N seconds
    --stop-on-stdin       Also stop on a line or end of input on stdin
    -h, --help            Print this help

Stops on Ctrl+C. Prints one JSON line describing both tracks when done.
";

#[derive(Serialize)]
struct Output {
    version: u32,
    seconds: f64,
    mic: TrackReport,
    system: TrackReport,
}

#[derive(Serialize, Clone)]
struct TrackReport {
    path: PathBuf,
    device: String,
    sample_rate: u32,
    channels: u16,
    received_seconds: f64,
    padded_seconds: f64,
    dropped_seconds: f64,
    /// Whether any buffer was louder than silence.
    heard: bool,
}

struct Args {
    mic_out: PathBuf,
    system_out: PathBuf,
    mic: Option<String>,
    system: Option<String>,
    seconds: Option<f64>,
    stop_on_stdin: bool,
}

enum Command {
    Help,
    ListDevices,
    Record(Args),
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("paraspeakrs-record: {message}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), String> {
    match parse_args(std::env::args().skip(1))? {
        Command::Help => {
            print!("{USAGE}");
            Ok(())
        }
        Command::ListDevices => list_devices(&audio_host()?),
        Command::Record(args) => record(args),
    }
}

fn parse_args(mut argv: impl Iterator<Item = String>) -> Result<Command, String> {
    let mut mic_out = None;
    let mut system_out = None;
    let mut mic = None;
    let mut system = None;
    let mut seconds = None;
    let mut stop_on_stdin = false;
    while let Some(arg) = argv.next() {
        let mut value = |flag: &str| argv.next().ok_or_else(|| format!("{flag} requires a value"));
        match arg.as_str() {
            "-h" | "--help" => return Ok(Command::Help),
            "--list-devices" => return Ok(Command::ListDevices),
            "--mic-out" => mic_out = Some(PathBuf::from(value("--mic-out")?)),
            "--system-out" => system_out = Some(PathBuf::from(value("--system-out")?)),
            "--mic" => mic = Some(value("--mic")?),
            "--system" => system = Some(value("--system")?),
            "--seconds" => {
                let raw = value("--seconds")?;
                let parsed: f64 = raw.parse().map_err(|_| format!("--seconds: not a number: {raw:?}"))?;
                if parsed.is_nan() || parsed <= 0.0 {
                    return Err("--seconds must be positive".to_string());
                }
                seconds = Some(parsed);
            }
            "--stop-on-stdin" => stop_on_stdin = true,
            other => return Err(format!("unknown argument {other:?}; see --help")),
        }
    }
    Ok(Command::Record(Args {
        mic_out: mic_out.ok_or("missing --mic-out")?,
        system_out: system_out.ok_or("missing --system-out")?,
        mic,
        system,
        seconds,
        stop_on_stdin,
    }))
}

// ----- devices ------------------------------------------------------------------

/// PulseAudio on Linux, because it is the host that lists monitor sources; it is
/// the PipeWire desktop's PulseAudio server too. The platform default elsewhere.
fn audio_host() -> Result<Host, String> {
    #[cfg(target_os = "linux")]
    {
        cpal::host_from_id(cpal::HostId::PulseAudio).map_err(|err| {
            format!(
                "no PulseAudio server ({err}). System audio is recorded through PulseAudio, \
                 or PipeWire with pipewire-pulse, which most desktops run by default"
            )
        })
    }
    #[cfg(not(target_os = "linux"))]
    {
        Ok(cpal::default_host())
    }
}

fn name_of(device: &Device) -> String {
    device
        .description()
        .map(|description| description.name().to_string())
        .unwrap_or_else(|_| "unknown device".to_string())
}

/// The id is what a user can pass to --mic or --system to pick a device exactly.
fn id_of(device: &Device) -> String {
    device.id().map(|id| id.id().to_string()).unwrap_or_default()
}

fn matches(device: &Device, wanted: &str) -> bool {
    let wanted = wanted.to_lowercase();
    name_of(device).to_lowercase().contains(&wanted) || id_of(device).to_lowercase() == wanted
}

fn list_devices(host: &Host) -> Result<(), String> {
    let inputs = host.input_devices().map_err(|err| format!("cannot list input devices: {err}"))?;
    println!("Inputs (--mic, and --system on Linux):");
    for device in inputs {
        println!("  {}  [{}]", name_of(&device), id_of(&device));
    }
    if cfg!(target_os = "windows") {
        let outputs = host.output_devices().map_err(|err| format!("cannot list output devices: {err}"))?;
        println!("Outputs (--system, recorded by loopback):");
        for device in outputs {
            println!("  {}  [{}]", name_of(&device), id_of(&device));
        }
    }
    Ok(())
}

fn find_input(host: &Host, wanted: &str) -> Result<Device, String> {
    host.input_devices()
        .map_err(|err| format!("cannot list input devices: {err}"))?
        .find(|device| matches(device, wanted))
        .ok_or_else(|| format!("no input device matches {wanted:?}; see --list-devices"))
}

fn mic_device(host: &Host, wanted: Option<&str>) -> Result<Device, String> {
    match wanted {
        Some(wanted) => find_input(host, wanted),
        None => host.default_input_device().ok_or_else(|| "no default input device".to_string()),
    }
}

/// On Linux the default sink's monitor, which PulseAudio and pipewire-pulse
/// both name `<sink>.monitor`.
#[cfg(target_os = "linux")]
fn system_device(host: &Host, wanted: Option<&str>) -> Result<Device, String> {
    if let Some(wanted) = wanted {
        return find_input(host, wanted);
    }
    let sink = host.default_output_device().ok_or("no default output device")?;
    let monitor = format!("{}.monitor", id_of(&sink));
    host.input_devices()
        .map_err(|err| format!("cannot list input devices: {err}"))?
        .find(|device| id_of(device) == monitor)
        .ok_or_else(|| format!("the default output has no monitor source {monitor:?}; see --list-devices"))
}

/// On Windows an output device: cpal records it with WASAPI loopback when an
/// input stream is built on it.
#[cfg(not(target_os = "linux"))]
fn system_device(host: &Host, wanted: Option<&str>) -> Result<Device, String> {
    match wanted {
        Some(wanted) => host
            .output_devices()
            .map_err(|err| format!("cannot list output devices: {err}"))?
            .find(|device| matches(device, wanted))
            .ok_or_else(|| format!("no output device matches {wanted:?}; see --list-devices")),
        None => host.default_output_device().ok_or_else(|| "no default output device".to_string()),
    }
}

// ----- the shared clock ---------------------------------------------------------

/// Where a buffer goes on the shared clock: how much silence to write before it,
/// and how many of its leading frames to drop.
#[derive(Debug, PartialEq, Eq)]
struct Placement {
    pad: u64,
    skip: usize,
}

/// Keeps one track on the recording's clock.
struct Aligner {
    rate: f64,
    tolerance: u64,
    written: u64,
}

impl Aligner {
    fn new(sample_rate: u32) -> Self {
        Self {
            rate: f64::from(sample_rate),
            tolerance: (TOLERANCE.as_secs_f64() * f64::from(sample_rate)).round() as u64,
            written: 0,
        }
    }

    fn frame_at(&self, offset: Duration) -> u64 {
        (offset.as_secs_f64() * self.rate).round() as u64
    }

    /// `frames` frames whose first one was captured `start` after the recording began.
    fn place(&mut self, start: Duration, frames: usize) -> Placement {
        let start = self.frame_at(start);
        let mut placement = Placement { pad: 0, skip: 0 };
        if start > self.written + self.tolerance {
            placement.pad = start - self.written;
        } else if self.written > start + self.tolerance {
            placement.skip = usize::try_from(self.written - start).unwrap_or(usize::MAX).min(frames);
        }
        self.written += placement.pad + (frames - placement.skip) as u64;
        placement
    }

    /// Silence that brings the track up to `end`, so both tracks stop together.
    fn tail(&mut self, end: Duration) -> u64 {
        let pad = self.frame_at(end).saturating_sub(self.written);
        self.written += pad;
        pad
    }
}

// ----- capture ------------------------------------------------------------------

/// The recording's clock. It starts when both streams are open and about to play,
/// so the time spent opening devices is neither counted by --seconds nor written
/// as leading silence; `stop` is where both tracks end.
#[derive(Clone, Default)]
struct Clock {
    origin: Arc<OnceLock<Instant>>,
    stop: Arc<OnceLock<Duration>>,
}

impl Clock {
    fn start(&self) {
        let _ = self.origin.set(Instant::now());
    }

    /// A stream that delivers before `start` (no host should) starts the clock.
    fn origin(&self) -> Instant {
        *self.origin.get_or_init(Instant::now)
    }

    fn elapsed(&self) -> Duration {
        self.origin().elapsed()
    }

    fn stop(&self) -> Duration {
        *self.stop.get_or_init(|| self.elapsed())
    }
}

/// One callback's audio, downmixed to mono, with the moment its first frame was
/// captured on the recording's clock.
struct Chunk {
    start: Duration,
    samples: Vec<f32>,
}

struct Track {
    // Capture runs while this lives.
    stream: cpal::Stream,
    writer: thread::JoinHandle<Result<TrackReport, String>>,
    sender: mpsc::Sender<Chunk>,
}

/// Opens the device and the track's file. The stream is paused until `play`.
fn open_track(
    label: &'static str,
    device: &Device,
    loopback: bool,
    path: &Path,
    clock: &Clock,
) -> Result<Track, String> {
    let supported = if loopback {
        device.default_output_config()
    } else {
        device.default_input_config()
    }
    .map_err(|err| format!("{label}: no usable format on {}: {err}", name_of(device)))?;
    let sample_rate = supported.sample_rate();
    let channels = supported.channels();
    let format = supported.sample_format();
    let mut config = supported.config();
    let wanted = (BUFFER.as_secs_f64() * f64::from(sample_rate)) as u32;
    if let SupportedBufferSize::Range { min, max } = supported.buffer_size()
        && (*min..=*max).contains(&wanted)
    {
        config.buffer_size = BufferSize::Fixed(wanted);
    }

    let (sender, receiver) = mpsc::channel::<Chunk>();
    let report = TrackReport {
        path: path.to_path_buf(),
        device: name_of(device),
        sample_rate,
        channels,
        received_seconds: 0.0,
        padded_seconds: 0.0,
        dropped_seconds: 0.0,
        heard: false,
    };
    let writer = spawn_writer(label, path, report, receiver, clock.clone())?;
    let stream = build_stream(device, config, format, channels, sender.clone(), clock.clone(), label)?;
    Ok(Track {
        stream,
        writer,
        sender,
    })
}

fn build_stream(
    device: &Device,
    config: cpal::StreamConfig,
    format: SampleFormat,
    channels: u16,
    sender: mpsc::Sender<Chunk>,
    clock: Clock,
    label: &'static str,
) -> Result<cpal::Stream, String> {
    macro_rules! typed {
        ($t:ty) => {
            device.build_input_stream::<$t, _, _>(
                config,
                move |data: &[$t], info: &cpal::InputCallbackInfo| {
                    let samples = downmix(data, channels, |sample| cpal::Sample::to_sample::<f32>(sample));
                    let _ = sender.send(Chunk {
                        start: capture_offset(info, clock.origin()),
                        samples,
                    });
                },
                move |err| eprintln!("paraspeakrs-record: {label}: {err}"),
                None,
            )
        };
    }
    let stream = match format {
        SampleFormat::F32 => typed!(f32),
        SampleFormat::F64 => typed!(f64),
        SampleFormat::I16 => typed!(i16),
        SampleFormat::I32 => typed!(i32),
        SampleFormat::U16 => typed!(u16),
        SampleFormat::U8 => typed!(u8),
        SampleFormat::I8 => typed!(i8),
        other => return Err(format!("{label}: sample format {other} is not supported")),
    };
    stream.map_err(|err| format!("{label}: cannot open {}: {err}", name_of(device)))
}

/// When the buffer's first frame was captured, on the recording's clock.
///
/// The host reports capture and callback times on a clock of its own; their
/// difference is how long ago the buffer was captured, which carries over to
/// `Instant` whatever that clock is.
fn capture_offset(info: &cpal::InputCallbackInfo, started: Instant) -> Duration {
    let timestamp = info.timestamp();
    let age = timestamp.callback.duration_since(timestamp.capture);
    Instant::now()
        .checked_sub(age)
        .unwrap_or(started)
        .saturating_duration_since(started)
}

fn downmix<T: Copy>(data: &[T], channels: u16, to_f32: impl Fn(T) -> f32) -> Vec<f32> {
    let channels = usize::from(channels.max(1));
    data.chunks(channels)
        .map(|frame| frame.iter().map(|&sample| to_f32(sample)).sum::<f32>() / frame.len() as f32)
        .collect()
}

fn spawn_writer(
    label: &'static str,
    path: &Path,
    mut report: TrackReport,
    receiver: mpsc::Receiver<Chunk>,
    clock: Clock,
) -> Result<thread::JoinHandle<Result<TrackReport, String>>, String> {
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: report.sample_rate,
        bits_per_sample: 32,
        sample_format: hound::SampleFormat::Float,
    };
    let mut wav =
        hound::WavWriter::create(path, spec).map_err(|err| format!("cannot write {}: {err}", path.display()))?;
    Ok(thread::spawn(move || {
        let rate = f64::from(report.sample_rate);
        let mut aligner = Aligner::new(report.sample_rate);
        let mut warned = false;
        let mut last_flush = Instant::now();
        let write = |wav: &mut hound::WavWriter<_>, samples: &[f32], silence: u64| -> Result<(), String> {
            for _ in 0..silence {
                wav.write_sample(0.0f32).map_err(|err| err.to_string())?;
            }
            for &sample in samples {
                wav.write_sample(sample).map_err(|err| err.to_string())?;
            }
            Ok(())
        };
        // Ends when the stream and the main thread have both dropped their senders.
        for chunk in receiver {
            let placement = aligner.place(chunk.start, chunk.samples.len());
            write(&mut wav, &chunk.samples[placement.skip..], placement.pad)?;
            report.received_seconds += chunk.samples.len() as f64 / rate;
            report.padded_seconds += placement.pad as f64 / rate;
            report.dropped_seconds += placement.skip as f64 / rate;
            if !report.heard && chunk.samples.iter().any(|sample| sample.abs() > SILENCE_PEAK) {
                report.heard = true;
            }
            if !report.heard && !warned && clock.elapsed() > SILENCE_WARNING_AFTER {
                warned = true;
                eprintln!(
                    "paraspeakrs-record: warning: no {label} audio after {} s (nothing playing, or the wrong device?)",
                    SILENCE_WARNING_AFTER.as_secs()
                );
            }
            if last_flush.elapsed() > FLUSH_EVERY {
                wav.flush().map_err(|err| err.to_string())?;
                last_flush = Instant::now();
            }
        }
        let tail = aligner.tail(clock.stop());
        write(&mut wav, &[], tail)?;
        report.padded_seconds += tail as f64 / rate;
        wav.finalize().map_err(|err| err.to_string())?;
        Ok(report)
    }))
}

// ----- the recording ------------------------------------------------------------

fn record(args: Args) -> Result<(), String> {
    let host = audio_host()?;
    let mic = mic_device(&host, args.mic.as_deref())?;
    let system = system_device(&host, args.system.as_deref())?;

    let stop = Arc::new(AtomicBool::new(false));
    {
        let stop = stop.clone();
        ctrlc::set_handler(move || stop.store(true, Ordering::SeqCst))
            .map_err(|err| format!("cannot catch Ctrl+C: {err}"))?;
    }
    if args.stop_on_stdin {
        let stop = stop.clone();
        thread::spawn(move || {
            let mut line = String::new();
            let _ = std::io::stdin().lock().read_line(&mut line);
            stop.store(true, Ordering::SeqCst);
        });
    }

    let clock = Clock::default();
    let mic_track = open_track("microphone", &mic, false, &args.mic_out, &clock)?;
    let system_track = open_track("system", &system, cfg!(not(target_os = "linux")), &args.system_out, &clock)?;
    clock.start();
    for (track, device) in [(&mic_track, &mic), (&system_track, &system)] {
        track
            .stream
            .play()
            .map_err(|err| format!("cannot start {}: {err}", name_of(device)))?;
    }
    eprintln!(
        "paraspeakrs-record: recording microphone: {} | system audio: {}",
        name_of(&mic),
        name_of(&system)
    );

    let deadline = args.seconds.map(Duration::from_secs_f64);
    while !stop.load(Ordering::SeqCst) && deadline.is_none_or(|limit| clock.elapsed() < limit) {
        thread::sleep(Duration::from_millis(10));
    }
    let seconds = clock.stop().as_secs_f64();

    let mut reports = Vec::new();
    for track in [mic_track, system_track] {
        // Stop the stream first, then let the writer drain and pad to the end.
        drop(track.stream);
        drop(track.sender);
        reports.push(track.writer.join().map_err(|_| "a track writer panicked".to_string())??);
    }
    let system_report = reports.pop().expect("two tracks");
    let mic_report = reports.pop().expect("two tracks");
    let output = Output {
        version: CONTRACT_VERSION,
        seconds,
        mic: mic_report,
        system: system_report,
    };
    let json = serde_json::to_string(&output).map_err(|err| format!("cannot serialize the report: {err}"))?;
    // writeln!, not println!: a caller that has gone away must not turn a saved
    // recording into a panic.
    writeln!(std::io::stdout(), "{json}").map_err(|err| {
        format!(
            "recording saved to {} and {}, but the report could not be printed: {err}",
            args.mic_out.display(),
            args.system_out.display()
        )
    })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ms(value: u64) -> Duration {
        Duration::from_millis(value)
    }

    #[test]
    fn buffers_on_time_are_written_as_they_come() {
        let mut aligner = Aligner::new(1000);
        assert_eq!(aligner.place(ms(0), 20), Placement { pad: 0, skip: 0 });
        assert_eq!(aligner.place(ms(25), 20), Placement { pad: 0, skip: 0 });
        assert_eq!(aligner.written, 40);
    }

    #[test]
    fn a_gap_longer_than_the_tolerance_is_filled_with_silence() {
        // Loopback delivers nothing while nothing plays.
        let mut aligner = Aligner::new(1000);
        aligner.place(ms(0), 100);
        let placement = aligner.place(ms(3000), 20);
        assert_eq!(placement, Placement { pad: 2900, skip: 0 });
        assert_eq!(aligner.written, 3020);
    }

    #[test]
    fn a_track_running_ahead_drops_its_overlap() {
        let mut aligner = Aligner::new(1000);
        aligner.place(ms(0), 200);
        // Claims to start at 100 ms although 200 ms are already written.
        let placement = aligner.place(ms(100), 150);
        assert_eq!(placement, Placement { pad: 0, skip: 100 });
        assert_eq!(aligner.written, 250);
    }

    #[test]
    fn small_jitter_is_left_alone() {
        let mut aligner = Aligner::new(1000);
        aligner.place(ms(0), 100);
        assert_eq!(aligner.place(ms(140), 10), Placement { pad: 0, skip: 0 });
        assert_eq!(aligner.place(ms(70), 10), Placement { pad: 0, skip: 0 });
    }

    #[test]
    fn the_tail_pads_both_tracks_to_the_same_end() {
        let mut silent = Aligner::new(1000);
        let mut busy = Aligner::new(1000);
        busy.place(ms(0), 5000);
        assert_eq!(silent.tail(ms(5000)), 5000);
        assert_eq!(busy.tail(ms(5000)), 0);
        assert_eq!(silent.written, busy.written);
    }

    #[test]
    fn the_clock_ignores_device_setup_and_stops_once() {
        let clock = Clock::default();
        thread::sleep(ms(40)); // opening devices: not part of the recording
        clock.start();
        assert!(clock.elapsed() < ms(30), "{:?}", clock.elapsed());
        let stop = clock.stop();
        thread::sleep(ms(20));
        // Both writers pad to the same moment, however late each one gets there.
        assert_eq!(clock.stop(), stop);
    }

    #[test]
    fn stereo_is_averaged_to_mono() {
        assert_eq!(downmix(&[1.0f32, 0.0, 0.5, 0.5], 2, |sample| sample), vec![0.5, 0.5]);
        assert_eq!(downmix(&[0.25f32, -0.25], 1, |sample| sample), vec![0.25, -0.25]);
    }

    #[test]
    fn the_output_paths_are_required() {
        let args = |list: &[&str]| parse_args(list.iter().map(|arg| arg.to_string()));
        assert!(matches!(args(&["--mic-out", "a.wav"]), Err(message) if message.contains("--system-out")));
        assert!(matches!(
            args(&["--mic-out", "a.wav", "--system-out", "b.wav", "--seconds", "0"]),
            Err(message) if message.contains("positive")
        ));
        assert!(matches!(args(&["--list-devices"]), Ok(Command::ListDevices)));
    }
}
