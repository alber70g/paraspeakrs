//! Sidecar binary: run speakrs diarization over one mono 16 kHz WAV and print JSON
//! on stdout for the Python pipeline to consume. Errors go to stderr with a
//! non-zero exit.
//!
//! The JSON contract is versioned (`version: 1`); bump CONTRACT_VERSION and the
//! matching check in `diarization.py` together if the shape ever changes.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use serde::Serialize;
use speakrs::inference::ExecutionMode;
use speakrs::pipeline::{DiarizationResult, OwnedDiarizationPipeline};

const CONTRACT_VERSION: u32 = 1;
const EXPECTED_SAMPLE_RATE: u32 = 16_000;

const USAGE: &str = "\
speakrs-diar - speaker diarization sidecar

USAGE:
    speakrs-diar [OPTIONS] <audio.wav>

ARGS:
    <audio.wav>            Mono 16 kHz 16-bit PCM WAV

OPTIONS:
    --mode <MODE>          Execution mode (default: cpu)
    --models-dir <DIR>     Load models from DIR instead of downloading them
    --file-id <ID>         Identifier echoed back in the JSON (default: file stem)
    -h, --help             Print this help
";

#[derive(Serialize)]
struct Output {
    version: u32,
    file_id: String,
    embedding_dim: usize,
    segments: Vec<SegmentOut>,
    centroids: BTreeMap<String, Vec<f32>>,
}

#[derive(Serialize)]
struct SegmentOut {
    speaker: String,
    start: f64,
    end: f64,
}

struct Args {
    audio: PathBuf,
    mode: ExecutionMode,
    models_dir: Option<PathBuf>,
    file_id: Option<String>,
}

fn main() -> ExitCode {
    match run() {
        Ok(json) => {
            println!("{json}");
            ExitCode::SUCCESS
        }
        Err(message) => {
            eprintln!("speakrs-diar: {message}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<String, String> {
    let args = match parse_args()? {
        Some(args) => args,
        None => {
            print!("{USAGE}");
            return Ok(String::new());
        }
    };

    let audio = load_wav(&args.audio)?;
    let file_id = args.file_id.clone().unwrap_or_else(|| {
        args.audio
            .file_stem()
            .map(|stem| stem.to_string_lossy().into_owned())
            .unwrap_or_else(|| "audio".to_string())
    });

    let mut pipeline = match &args.models_dir {
        Some(dir) => OwnedDiarizationPipeline::from_dir(dir.clone(), args.mode),
        None => OwnedDiarizationPipeline::from_pretrained(args.mode),
    }
    .map_err(|err| format!("failed to load models: {err}"))?;

    let result = pipeline
        .run_with_file_id(&audio, &file_id)
        .map_err(|err| format!("diarization failed: {err}"))?;

    let segments: Vec<SegmentOut> = result
        .segments
        .iter()
        .map(|segment| SegmentOut {
            speaker: segment.speaker.clone(),
            start: segment.start,
            end: segment.end,
        })
        .collect();

    let (embedding_dim, centroids) = speaker_centroids(&result);
    warn_on_label_mismatch(&segments, &centroids);

    let output = Output {
        version: CONTRACT_VERSION,
        file_id,
        embedding_dim,
        segments,
        centroids,
    };
    serde_json::to_string(&output).map_err(|err| format!("failed to serialize output: {err}"))
}

fn parse_args() -> Result<Option<Args>, String> {
    let mut audio: Option<PathBuf> = None;
    let mut mode_name = "cpu".to_string();
    let mut models_dir: Option<PathBuf> = None;
    let mut file_id: Option<String> = None;

    let mut argv = std::env::args().skip(1);
    while let Some(arg) = argv.next() {
        match arg.as_str() {
            "-h" | "--help" => return Ok(None),
            "--mode" => mode_name = next_value(&mut argv, "--mode")?,
            "--models-dir" => models_dir = Some(PathBuf::from(next_value(&mut argv, "--models-dir")?)),
            "--file-id" => file_id = Some(next_value(&mut argv, "--file-id")?),
            other if other.starts_with('-') => return Err(format!("unknown option {other:?}")),
            other => {
                if audio.is_some() {
                    return Err("expected exactly one audio path".to_string());
                }
                audio = Some(PathBuf::from(other));
            }
        }
    }

    let audio = audio.ok_or_else(|| "missing <audio.wav> argument".to_string())?;
    Ok(Some(Args {
        audio,
        mode: parse_mode(&mode_name)?,
        models_dir,
        file_id,
    }))
}

fn next_value(argv: &mut impl Iterator<Item = String>, flag: &str) -> Result<String, String> {
    argv.next().ok_or_else(|| format!("{flag} requires a value"))
}

fn parse_mode(name: &str) -> Result<ExecutionMode, String> {
    match name {
        "cpu" => Ok(ExecutionMode::Cpu),
        #[cfg(feature = "coreml")]
        "coreml" => Ok(ExecutionMode::CoreMl),
        #[cfg(feature = "coreml")]
        "coreml-fast" => Ok(ExecutionMode::CoreMlFast),
        #[cfg(feature = "cuda")]
        "cuda" => Ok(ExecutionMode::Cuda),
        #[cfg(feature = "cuda")]
        "cuda-fast" => Ok(ExecutionMode::CudaFast),
        other => Err(format!(
            "unsupported --mode {other:?}; this binary supports: {}",
            supported_modes().join(", ")
        )),
    }
}

fn supported_modes() -> Vec<&'static str> {
    let mut modes = vec!["cpu"];
    if cfg!(feature = "coreml") {
        modes.extend(["coreml", "coreml-fast"]);
    }
    if cfg!(feature = "cuda") {
        modes.extend(["cuda", "cuda-fast"]);
    }
    modes
}

fn load_wav(path: &Path) -> Result<Vec<f32>, String> {
    let mut reader =
        hound::WavReader::open(path).map_err(|err| format!("cannot read {}: {err}", path.display()))?;
    let spec = reader.spec();

    if spec.channels != 1 {
        return Err(format!(
            "expected mono audio, got {} channels ({})",
            spec.channels,
            path.display()
        ));
    }
    if spec.sample_rate != EXPECTED_SAMPLE_RATE {
        return Err(format!(
            "expected {EXPECTED_SAMPLE_RATE} Hz, got {} Hz ({})",
            spec.sample_rate,
            path.display()
        ));
    }
    if spec.sample_format != hound::SampleFormat::Int || spec.bits_per_sample != 16 {
        return Err(format!(
            "expected 16-bit PCM, got {:?}/{} bits ({})",
            spec.sample_format,
            spec.bits_per_sample,
            path.display()
        ));
    }

    reader
        .samples::<i16>()
        .map(|sample| {
            sample
                .map(|value| f32::from(value) / 32768.0)
                .map_err(|err| format!("corrupt sample in {}: {err}", path.display()))
        })
        .collect()
}

/// speakrs exposes per-chunk embeddings and their cluster assignments, but no
/// ready-made per-speaker centroid. Average every chunk-speaker embedding that
/// landed in the same global cluster, then L2-normalize — the speaker cache
/// compares with cosine similarity, so magnitude is irrelevant.
///
/// `hard_clusters` is (chunks, speakers) with -1 for unassigned pairs;
/// `embeddings` is (chunks, speakers, dim) and holds non-finite values for
/// chunk-speaker pairs that never spoke, so those are skipped too.
fn speaker_centroids(result: &DiarizationResult) -> (usize, BTreeMap<String, Vec<f32>>) {
    let embeddings = &result.embeddings;
    let clusters = &result.hard_clusters;
    let (chunks, speakers, dim) = embeddings.dim();

    let mut sums: BTreeMap<i32, (Vec<f64>, usize)> = BTreeMap::new();
    for chunk in 0..chunks {
        for speaker in 0..speakers {
            let cluster = clusters[[chunk, speaker]];
            if cluster < 0 {
                continue;
            }
            if (0..dim).any(|d| !embeddings[[chunk, speaker, d]].is_finite()) {
                continue;
            }
            let entry = sums.entry(cluster).or_insert_with(|| (vec![0.0; dim], 0));
            for d in 0..dim {
                entry.0[d] += f64::from(embeddings[[chunk, speaker, d]]);
            }
            entry.1 += 1;
        }
    }

    let centroids = sums
        .into_iter()
        .filter_map(|(cluster, (sum, count))| {
            if count == 0 {
                return None;
            }
            let mean: Vec<f64> = sum.iter().map(|value| value / count as f64).collect();
            let norm = mean.iter().map(|value| value * value).sum::<f64>().sqrt();
            if norm == 0.0 {
                return None;
            }
            let normalized = mean.iter().map(|value| (value / norm) as f32).collect();
            Some((format!("SPEAKER_{cluster:02}"), normalized))
        })
        .collect();

    (dim, centroids)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cpu_mode_is_always_available() {
        assert_eq!(parse_mode("cpu").unwrap(), ExecutionMode::Cpu);
    }

    #[test]
    fn unknown_mode_lists_what_this_build_supports() {
        let err = parse_mode("tpu").unwrap_err();
        assert!(err.contains("tpu"), "{err}");
        assert!(err.contains("cpu"), "{err}");
    }

    #[test]
    fn rejects_audio_that_is_not_mono_16k() {
        // The Python side always feeds us ffmpeg-normalized mono 16 kHz PCM; anything
        // else means the caller skipped normalization and the timings would be wrong.
        let dir = std::env::temp_dir().join("speakrs-diar-tests");
        std::fs::create_dir_all(&dir).unwrap();

        let stereo = dir.join("stereo.wav");
        write_wav(&stereo, 2, 16_000);
        assert!(load_wav(&stereo).unwrap_err().contains("mono"));

        let resampled = dir.join("44k.wav");
        write_wav(&resampled, 1, 44_100);
        assert!(load_wav(&resampled).unwrap_err().contains("16000 Hz"));
    }

    #[test]
    fn reads_mono_16k_samples_as_normalized_floats() {
        let dir = std::env::temp_dir().join("speakrs-diar-tests");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("mono.wav");
        write_wav(&path, 1, 16_000);

        let samples = load_wav(&path).unwrap();

        assert_eq!(samples.len(), 4);
        assert!(samples.iter().all(|value| (-1.0..=1.0).contains(value)));
    }

    fn write_wav(path: &Path, channels: u16, sample_rate: u32) {
        let spec = hound::WavSpec {
            channels,
            sample_rate,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut writer = hound::WavWriter::create(path, spec).unwrap();
        for value in [0i16, 16_384, -16_384, 32_767] {
            writer.write_sample(value).unwrap();
        }
        writer.finalize().unwrap();
    }
}

/// The centroid labels are rebuilt from cluster ids while the segment labels come
/// out of speakrs itself. They should agree; if they ever stop agreeing, the
/// speaker cache would silently learn the wrong voice, so say so loudly.
fn warn_on_label_mismatch(segments: &[SegmentOut], centroids: &BTreeMap<String, Vec<f32>>) {
    let missing: Vec<&str> = segments
        .iter()
        .map(|segment| segment.speaker.as_str())
        .filter(|speaker| !centroids.contains_key(*speaker))
        .collect();
    if !missing.is_empty() {
        let mut unique: Vec<&str> = missing;
        unique.sort_unstable();
        unique.dedup();
        eprintln!(
            "speakrs-diar: warning: no centroid for speaker(s) {} - cluster ids and segment labels disagree",
            unique.join(", ")
        );
    }
}
