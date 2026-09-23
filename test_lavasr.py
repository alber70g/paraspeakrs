import torch
import torchaudio
import soundfile as sf
from LavaSR.model import LavaEnhance2

device = "cuda" if torch.cuda.is_available() else "cpu"
model = LavaEnhance2("YatharthS/LavaSR", device=device)

input_file = "test-audio/albert-gertjan-snippet-18-20.wav"
base_name = "albert-gertjan-snippet-18-20"

wav, sr = torchaudio.load(input_file)
if wav.shape[0] > 1:
    wav = wav.mean(dim=0, keepdim=True)
if sr != 16000:
    wav = torchaudio.functional.resample(wav, sr, 16000)
wav = wav.to(device)

variants = [
    (True,  True,  "enhance_denoise"),
    (True,  False, "enhance_only"),
    (False, True,  "denoise_only"),
    (False, False, "bwe_only"),
]

for enhance_flag, denoise_flag, suffix in variants:
    result = model.enhance(wav, enhance=enhance_flag, denoise=denoise_flag)
    out_path = f"out_lavasr/{base_name}_{suffix}.wav"
    out = result.detach().cpu().reshape(1, -1)
    sf.write(out_path, out.squeeze(0).numpy(), 48000)
    print(f"Done! Written {out_path}")
