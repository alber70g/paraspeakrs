from df.enhance import enhance, init_df, load_audio, save_audio
import torch

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"🚀 Using device: {device}")

model, df_state, _ = init_df()
model = model.to(device)

audio, _ = load_audio("test-audio/albert-gertjan-snippet-18-20.wav", sr=df_state.sr())
enhanced = enhance(model, df_state, audio)

import os
os.makedirs("deefilter_out", exist_ok=True)
save_audio("deefilter_out/albert-gertjan-snippet-18-20.wav", enhanced, df_state.sr())

print("✅ Done!")
